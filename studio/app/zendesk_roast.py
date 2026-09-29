"""The routing review ("roast report"): what a support team's Zendesk history says about its routing, before any model
is involved.

Input is the label builder's rows (app/zendesk_labels.py, every row, excluded ones too). Everything here is counted
from the history: how often the first assignment was right (by people, by triggers), what misroutes cost in hours in
the wrong group, which groups and triggers miss most, whether the misses cluster on facts the message does not carry
(the org's plan, its open tickets, its renewal date), and how routing changed month to month.

"Right" means "agrees with the label", and a misroute nobody fixed counts as right. So first-time-right figures are
upper bounds, and the page says so.

    python -m app.zendesk_roast labels.jsonl review.html --company Fernhollow
"""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

KEEP = ("strong", "medium")


def _routed(r: dict) -> list[list]:
    return [m for m in r.get("moves") or [] if m[1] is not None]


def hours_wrong(r: dict) -> float:
    """Hours between the first assignment and arriving, for the last time, in the group it was solved in."""
    ms = _routed(r)
    if not ms or ms[0][1] == r["label"]:
        return 0.0
    k = max(i for i, m in enumerate(ms) if m[1] != r["label"]) + 1
    return max(0.0, ms[k][0] - ms[0][0]) if k < len(ms) else 0.0


def _rate(xs: list[bool]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _conditions(rows: list[dict]):
    """Facts the message does not carry, as simple tests; pairs of them too."""
    atoms: list[tuple[str, Any]] = []
    for v in sorted({r.get("org_plan") for r in rows if r.get("org_plan")}):
        atoms.append((f"the org is on {v}", lambda r, v=v: r.get("org_plan") == v))
    for k in (1, 2, 3):
        atoms.append((f"the org already had {k}+ open ticket{'s' if k > 1 else ''}", lambda r, k=k: (r.get("org_open_tickets") or 0) >= k))
    for d in (30, 60, 90):
        atoms.append((f"the org's annual renewal was {d} days away or less",
                      lambda r, d=d: r.get("days_to_renewal") is not None and r["days_to_renewal"] <= d))
    for v in sorted({r.get("channel") for r in rows if r.get("channel")}):
        atoms.append((f"it came in by {v}", lambda r, v=v: r.get("channel") == v))
    yield from atoms
    for i, (n1, f1) in enumerate(atoms):
        for n2, f2 in atoms[i + 1:]:
            yield f"{n1} and {n2.replace('the org ', 'it ', 1) if n2.startswith('the org ') else n2}", (lambda r, f1=f1, f2=f2: f1(r) and f2(r))


def analyze(rows: list[dict], agent_min: int = 30) -> dict[str, Any]:
    kept = [r for r in rows if r["tier"] in KEEP]
    if not kept:
        return {"tickets": len(rows), "kept": 0}
    first_right = lambda r: r["first_group"] == r["label"]
    people = [r for r in kept if r["first_by"] == "person"]
    trig = [r for r in kept if r["first_by"] == "rule"]
    moved = [r for r in kept if not first_right(r)]
    hw = [hours_wrong(r) for r in moved]
    wait = [_routed(r)[0][0] for r in people if _routed(r)]
    solve_ok = [r["solved_h"] for r in kept if first_right(r) and r.get("solved_h") is not None]
    solve_bad = [r["solved_h"] for r in moved if r.get("solved_h") is not None]

    groups = []
    for g, rs in sorted(_by(kept, "label").items(), key=lambda kv: -len(kv[1])):
        misses = Counter(r["first_group"] for r in rs if not first_right(r))
        groups.append({"group": g, "n": len(rs), "first_right": _rate([first_right(r) for r in rs]),
                       "top_miss": misses.most_common(1)[0] if misses else None})
    pairs = defaultdict(list)
    for r in moved:
        pairs[(r["first_group"], r["label"])].append(hours_wrong(r))
    pairs = [{"from": a, "to": b, "n": len(h), "hours": sum(h), "median_h": median(h)} for (a, b), h in pairs.items()]
    pairs.sort(key=lambda p: -p["hours"])

    triggers = []
    for t, rs in sorted(_by(trig, "first_trigger").items(), key=lambda kv: -len(kv[1])):
        misses = Counter((r["first_group"], r["label"]) for r in rs if not first_right(r))
        triggers.append({"trigger": t or "(unnamed trigger)", "n": len(rs), "first_right": _rate([first_right(r) for r in rs]),
                         "top_miss": misses.most_common(1)[0] if misses else None})

    agents = sorted((_rate([first_right(r) for r in rs]), len(rs)) for a, rs in _by(people, "first_agent").items() if len(rs) >= agent_min)
    agents.reverse()

    months = []
    for m, rs in sorted(_by(kept, lambda r: r["created_at"][:7]).items()):
        months.append({"month": m, "n": len(rs), "people": _rate([first_right(r) for r in rs if r["first_by"] == "person"]),
                       "triggers": _rate([first_right(r) for r in rs if r["first_by"] == "rule"])})
    start = min(r["created_at"] for r in rows)
    first_seen = {}
    for r in sorted(kept, key=lambda r: r["created_at"]):
        first_seen.setdefault(r["label"], r["created_at"][:10])
    new_groups = {g: d for g, d in first_seen.items() if (_d(d) - _d(start[:10])).days > 21}

    patterns = []
    base = len(kept)
    for g in groups:
        if g["n"] < 15 or g["first_right"] > 0.6:
            continue
        rs = [r for r in kept if r["label"] == g["group"]]
        best = None
        for name, f in _conditions(kept):
            inside = sum(1 for r in rs if f(r)) / len(rs)
            overall = sum(1 for r in kept if f(r)) / base
            if inside >= 0.4 and inside - overall > (best[2] - best[3] if best else 0.25):
                best = (name, f, inside, overall)
        if best:
            hit = [r for r in rs if best[1](r)]
            patterns.append({"group": g["group"], "n": g["n"], "first_right": g["first_right"], "condition": best[0],
                             "share_in_group": best[2], "share_overall": best[3],
                             "first_right_when": _rate([first_right(r) for r in hit])})

    return {
        "tickets": len(rows), "kept": len(kept), "start": start[:10], "end": max(r["created_at"] for r in rows)[:10],
        "excluded": dict(Counter(r["tier_reason"] for r in rows if r["tier"] not in KEEP).most_common()),
        "first_right": {"all": _rate([first_right(r) for r in kept]), "people": _rate([first_right(r) for r in people]),
                        "triggers": _rate([first_right(r) for r in trig]), "people_n": len(people), "triggers_n": len(trig)},
        "moved": {"n": len(moved), "share": len(moved) / len(kept), "hours": sum(hw), "median_h": median(hw) if hw else None},
        "wait_median_h": median(wait) if wait else None,
        "solve_median_h": {"first_right": median(solve_ok) if solve_ok else None, "moved": median(solve_bad) if solve_bad else None},
        "groups": groups, "pairs": pairs[:8], "triggers": triggers, "agents": agents, "months": months,
        "new_groups": new_groups, "patterns": patterns,
    }


def _by(rows, key):
    out = defaultdict(list)
    for r in rows:
        out[key(r) if callable(key) else r.get(key)].append(r)
    return out


def _d(s: str):
    return datetime.fromisoformat(s[:10])


# ---- the page ----------------------------------------------------------------------------------------------------

def _pct(x, d=0):
    return "–" if x is None else f"{100 * x:.{d}f}%"


def _h(x):
    if x is None:
        return "–"
    return f"{x:.1f}\u00a0h" if x < 48 else f"{x / 24:.1f}\u00a0days"


def _date(s: str) -> str:
    return _d(s).strftime("%-d %b %Y")


def _month(s: str) -> str:
    return datetime.fromisoformat(s + "-01").strftime("%b")


CSS = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#111a33;--line:#26325a;--text:#e8ecf7;--muted:#9aa6c8;
 --accent:#5ee3c8;--warn:#ffb454;--bad:#ff7a90;--track:#1c2748;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){color-scheme:light;--bg:#f6f7fb;--panel:#ffffff;
 --line:#d9deec;--text:#141a2e;--muted:#5a6485;--accent:#0f8f7a;--warn:#b86e00;--bad:#c23454;--track:#e6e9f3}}
:root[data-theme="light"]{color-scheme:light;--bg:#f6f7fb;--panel:#ffffff;--line:#d9deec;--text:#141a2e;--muted:#5a6485;
 --accent:#0f8f7a;--warn:#b86e00;--bad:#c23454;--track:#e6e9f3}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:15.5px;line-height:1.55;margin:0}
.wrap{max-width:960px;margin:0 auto;padding-inline:20px;padding-block:36px 64px;display:grid;gap:40px}
header{display:grid;gap:10px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-size:clamp(26px,4.2vw,38px);line-height:1.15;margin:0;text-wrap:balance;letter-spacing:-.3px}
h2{font-size:20px;margin:0;text-wrap:balance}
p{margin:0;max-width:68ch}
.lead{color:var(--muted);font-size:17px}
section{display:grid;gap:14px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.stat{background:var(--panel);padding:16px 18px;display:grid;gap:4px;align-content:start}
.stat .v{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:28px;font-weight:600}
.stat .k{font-size:13px;color:var(--muted)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:12px;font-weight:600;color:var(--muted);letter-spacing:.04em;text-transform:uppercase;white-space:nowrap}
tr:last-child td{border-bottom:0}
td.r,th.r{text-align:right}
.bar{display:flex;align-items:center;gap:10px;min-width:170px}
.bar i{display:block;height:8px;border-radius:4px;background:var(--track);flex:1;position:relative;overflow:hidden}
.bar i b{position:absolute;inset:0 auto 0 0;border-radius:4px}
.ok b{background:var(--accent)}.mid b{background:var(--warn)}.low b{background:var(--bad)}
.muted{color:var(--muted)}
.arrow{color:var(--muted);padding-inline:4px}
.pattern{border-left:3px solid var(--warn);padding:10px 14px;background:var(--panel);border-radius:0 10px 10px 0;display:grid;gap:4px}
.pattern strong{font-weight:600}
.note{font-size:13.5px;color:var(--muted);max-width:75ch}
svg text{fill:var(--muted);font-family:var(--sans);font-size:11px}
.legend{display:flex;gap:18px;font-size:13px;color:var(--muted);flex-wrap:wrap}
.legend span::before{content:"";display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle;background:var(--c)}
.dots{display:grid;gap:6px}
.dots div{display:grid;grid-template-columns:70px 1fr 64px;gap:10px;align-items:center;font-size:13.5px}
ul.plain{margin:0;padding-left:18px;display:grid;gap:4px;max-width:75ch}
"""


def _bar(x):
    cls = "ok" if x >= 0.85 else "mid" if x >= 0.7 else "low"
    return f'<div class="bar {cls}"><i><b style="width:{100 * x:.1f}%"></b></i><span class="num">{_pct(x)}</span></div>'


def _chart(months):
    pts = [(m["month"], m["people"], m["triggers"]) for m in months]
    vals = [v for _, a, b in pts for v in (a, b) if v is not None]
    if len(pts) < 2 or not vals:
        return ""
    lo = max(0.0, min(0.5, (int(min(vals) * 10) / 10)))
    W, H, L, R, T, B = 640, 220, 44, 16, 14, 30
    x = lambda i: L + i * (W - L - R) / (len(pts) - 1)
    y = lambda v: T + (1 - (v - lo) / (1 - lo)) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="First-time-right by month">']
    k = lo
    while k <= 1.0001:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(k):.1f}" y2="{y(k):.1f}" stroke="var(--line)" stroke-width="1"/>'
                   f'<text x="{L - 8}" y="{y(k) + 4:.1f}" text-anchor="end">{round(k * 100)}%</text>')
        k += 0.1
    for i, (m, _, _) in enumerate(pts):
        out.append(f'<text x="{x(i):.1f}" y="{H - 8}" text-anchor="middle">{_month(m)}</text>')
    for idx, color in ((1, "var(--accent)"), (2, "var(--warn)")):
        seq = [(x(i), y(p[idx])) for i, p in enumerate(pts) if p[idx] is not None]
        if len(seq) > 1:
            out.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.2" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in seq) + '"/>')
        out += [f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.2" fill="{color}"/>' for a, b in seq]
    out.append("</svg>")
    return "".join(out)


def render(rep: dict, company: str = "Your team") -> str:
    e = html.escape
    fr, mv = rep["first_right"], rep["moved"]
    s = [f"<title>{e(company)} Routing Review</title><style>{CSS}</style><div class=\"wrap\">"]
    s.append(f'<header><div class="eyebrow">Routing review · {e(company)} support · {_date(rep["start"])} to {_date(rep["end"])}</div>'
             f'<h1>{_pct(fr["people"])} of tickets reach the right group on the first try. The rest spend {_h(mv["median_h"])} in the wrong queue, typically.</h1>'
             f'<p class="lead">Read from {rep["tickets"]:,} tickets in your Zendesk history, {rep["kept"]:,} of them routing decisions we could label. '
             'No model was involved in any number on this page.</p></header>')
    s.append('<div class="stats">'
             f'<div class="stat"><span class="v">{_pct(fr["people"])}</span><span class="k">right first time when a person routed it ({fr["people_n"]:,} tickets)</span></div>'
             f'<div class="stat"><span class="v">{_pct(fr["triggers"])}</span><span class="k">right first time when a trigger routed it ({fr["triggers_n"]:,} tickets)</span></div>'
             f'<div class="stat"><span class="v">{mv["n"]:,}</span><span class="k">tickets moved to another group ({_pct(mv["share"])})</span></div>'
             f'<div class="stat"><span class="v">{mv["hours"]:,.0f}\u00a0h</span><span class="k">spent in the wrong group, in total</span></div>'
             '</div>')
    sm = rep["solve_median_h"]
    s.append(f'<p>Tickets that waited for a person waited {_h(rep["wait_median_h"])} (median) for a first group. '
             f'Tickets routed right the first time were solved in {_h(sm["first_right"])} (median); tickets that were moved took {_h(sm["moved"])}.</p>')

    s.append('<section><h2>Where tickets go wrong</h2><p class="muted">For each group, how many of the tickets it solved were sent there first, and where the others went instead.</p>'
             '<div class="scroll"><table><thead><tr><th>Group</th><th class="r">Tickets</th><th>Right first time</th><th>Most often sent to instead</th></tr></thead><tbody>')
    for g in rep["groups"]:
        miss = f'{e(g["top_miss"][0])} <span class="muted num">×{g["top_miss"][1]}</span>' if g["top_miss"] else '<span class="muted">–</span>'
        s.append(f'<tr><td>{e(g["group"])}</td><td class="r num">{g["n"]:,}</td><td>{_bar(g["first_right"])}</td><td>{miss}</td></tr>')
    s.append("</tbody></table></div></section>")

    s.append('<section><h2>The costliest misroutes</h2><p class="muted">Ranked by total hours tickets sat in the wrong group before reaching the one that solved them.</p>'
             '<div class="scroll"><table><thead><tr><th>Sent to</th><th>Belonged in</th><th class="r">Tickets</th><th class="r">Hours in total</th><th class="r">Median wait</th></tr></thead><tbody>')
    for p in rep["pairs"]:
        s.append(f'<tr><td>{e(p["from"])}</td><td>{e(p["to"])}</td><td class="r num">{p["n"]}</td><td class="r num">{p["hours"]:,.0f}</td><td class="r num">{_h(p["median_h"])}</td></tr>')
    s.append("</tbody></table></div></section>")

    if rep["patterns"]:
        s.append('<section><h2>Misses that follow facts the message doesn\'t carry</h2>'
                 '<p class="muted">Where a group is rarely reached first time, we looked for a fact about the customer that most of its tickets share. '
                 'These are patterns in the history, worth checking with the team; they are not rules we know you have.</p>')
        for p in rep["patterns"]:
            s.append(f'<div class="pattern"><div><strong>{e(p["group"])}</strong>: {_pct(p["share_in_group"])} of its {p["n"]} tickets arrived when '
                     f'{e(p["condition"])}, against {_pct(p["share_overall"], 1)} of all tickets.</div>'
                     f'<div class="muted">Those tickets reached {e(p["group"])} first time {_pct(p["first_right_when"])} of the time. '
                     'A triage step can look these facts up; a person reading the message cannot see them.</div></div>')
        s.append("</section>")

    if rep["triggers"]:
        s.append('<section><h2>Triggers</h2><div class="scroll"><table><thead><tr><th>Trigger</th><th class="r">Tickets</th><th>Right first time</th><th>Most common miss</th></tr></thead><tbody>')
        for t in rep["triggers"]:
            miss = (f'{e(t["top_miss"][0][0])}<span class="arrow">→</span>{e(t["top_miss"][0][1])} <span class="muted num">×{t["top_miss"][1]}</span>'
                    if t["top_miss"] else '<span class="muted">–</span>')
            s.append(f'<tr><td>{e(t["trigger"])}</td><td class="r num">{t["n"]:,}</td><td>{_bar(t["first_right"])}</td><td>{miss}</td></tr>')
        s.append("</tbody></table></div></section>")

    s.append('<section><h2>Month by month</h2><div class="legend"><span style="--c:var(--accent)">routed by a person</span>'
             '<span style="--c:var(--warn)">routed by a trigger</span></div>' + _chart(rep["months"]))
    if rep["new_groups"]:
        s.append("<p>" + " ".join(f'{e(g)} first appears on {_date(d)}.' for g, d in sorted(rep["new_groups"].items(), key=lambda kv: kv[1]))
                 + " A model trained on the whole history has to know which groups existed when.</p>")
    s.append("</section>")

    if rep["agents"]:
        s.append(f'<section><h2>The triage team</h2><p class="muted">Right first time for each person who routed at least 30 tickets. Names are left out.</p><div class="dots">')
        for i, (rate, n) in enumerate(rep["agents"], 1):
            cls = "ok" if rate >= 0.85 else "mid" if rate >= 0.7 else "low"
            s.append(f'<div><span class="muted">Person {i}</span><span class="bar {cls}"><i><b style="width:{100 * rate:.1f}%"></b></i></span><span class="num">{_pct(rate)}</span></div>')
        s.append("</div></section>")

    ex = rep["excluded"]
    s.append('<section><h2>How these numbers were made</h2><ul class="plain">'
             '<li>Each ticket is read as it arrived: the customer\'s subject, message and form fields, replayed from the ticket history. Tags, notes and edited subjects added later are left out.</li>'
             '<li>Its label is the group that solved it. "Right first time" means the first group matched that label.</li>'
             '<li>A ticket that was misrouted and solved where it landed counts as right, so every first-time figure here is an upper bound.</li>'
             f'<li>Left out as not routing decisions: ' + ", ".join(f'{e(k)} {v}' for k, v in ex.items()) + ".</li></ul></section>")
    s.append('<section><h2>What happens next</h2><p>We optimize a triage prompt on these labels, holding out the most recent weeks to score it, and compare it with the '
             'first-time figures above. If it holds up, it runs in shadow for two weeks: it writes its pick to a hidden field and your team routes as usual. '
             'Nothing changes for customers until you have seen where it agrees with your team and where it does not.</p></section>')
    s.append("</div>")
    return "".join(s)


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="The routing review from label-builder rows")
    ap.add_argument("labels"), ap.add_argument("out"), ap.add_argument("--company", default="Your team")
    a = ap.parse_args(argv)
    rows = [json.loads(l) for l in Path(a.labels).open() if l.strip()]
    rep = analyze(rows)
    Path(a.out).write_text(render(rep, a.company))
    Path(a.out).with_suffix(".json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({k: rep[k] for k in ("tickets", "kept", "first_right", "moved", "patterns")}, indent=1, default=str))


if __name__ == "__main__":
    main()
