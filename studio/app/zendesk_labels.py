"""Routing labels from a Zendesk export: what each ticket looked like when it arrived, and where people decided it
belonged.

Input is what Zendesk's API gives (tickets, ticket audits, users, organizations, groups), as JSON lines. The ticket
export is the ticket's CURRENT state: its tags, subject and group were written after the fact and name the answer. So
every input here comes from replaying the audits up to the creation audit, and nothing after it except the history
that was already known at that moment (the org's other open tickets).

The label is the group the ticket was solved in. How much to trust it is the tier:

  strong    a person moved it there within `hours` of the previous assignment: an explicit correction
  medium    the first assignment stood until solved (by a person or a trigger), or the correction came late
  weak      it went through three or more groups (contested); kept out of the dataset by default
  excluded  not a routing decision: deleted (spam), merged, still open at export, never routed, answered and solved at
            first contact (the first agent's group is where they sat, not where the ticket belonged), or moved into a
            catch-all group (a queue being emptied, not a ticket being corrected)

The report says how often the first assignment agreed with the label, split by people and triggers. That agreement
among people is the ceiling a triage model is compared against; it is measured against these labels, not against a
truth nobody has.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MERGED_TAG = "closed_by_merge"


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


@dataclass
class Move:
    t: float
    group: int | None
    author: int
    by: str                        # "rule" | "person"


@dataclass
class Replay:
    id: int
    created: float
    channel: str
    subject: str = ""
    message: str = ""
    fields: dict[str, Any] = field(default_factory=dict)   # custom fields as the customer set them
    tags: list[str] = field(default_factory=list)          # tags at creation
    moves: list[Move] = field(default_factory=list)
    solved: float | None = None
    first_agent_reply: float | None = None


def replay(ticket: dict, audits: list[dict], agents: set[int]) -> Replay:
    """The ticket as it arrived, and every group assignment after that. Trigger-fired events carry their own via."""
    audits = sorted(audits, key=lambda a: (a["created_at"], a["id"]))
    r = Replay(ticket["id"], ts(ticket["created_at"]), (ticket.get("via") or {}).get("channel", ""))
    for i, a in enumerate(audits):
        t = ts(a["created_at"])
        for e in a["events"]:
            f, v = e.get("field_name"), e.get("value")
            ev_rule = (e.get("via") or {}).get("channel") == "rule" or a["via"]["channel"] == "rule"
            if i == 0 and e["type"] == "Create":
                if f == "subject":
                    r.subject = v or ""
                elif f == "tags":
                    r.tags = list(v or [])
                elif f and f.isdigit():
                    r.fields[f] = v
            if i == 0 and e["type"] == "Comment" and not r.message:
                r.message = e.get("body", "")
            if f == "group_id":
                g = int(v) if v else None
                if not r.moves or r.moves[-1].group != g:
                    r.moves.append(Move(t, g, a["author_id"], "rule" if ev_rule else "person"))
            if f == "status" and v == "solved":
                r.solved = t
            if e["type"] == "Comment" and e.get("public") and a["author_id"] in agents and r.first_agent_reply is None and i > 0:
                r.first_agent_reply = t
    return r


def classify(r: Replay, status: str, tags: list[str], hours: float, catch_all: set[int]) -> tuple[str, str]:
    """(tier, reason)."""
    if status == "deleted":
        return "excluded", "deleted"
    if MERGED_TAG in tags:
        return "excluded", "merged"
    if status not in ("solved", "closed"):
        return "excluded", "open at export"
    moves = [m for m in r.moves if m.group is not None]
    if not moves:
        return "excluded", "never routed"
    if len(moves) == 1 and moves[0].by == "person" and r.solved is not None and r.solved - r.created <= 3600:
        return "excluded", "answered at first contact"
    if len(moves) > 1 and moves[-1].group in catch_all:
        return "excluded", "moved into a catch-all"
    if len({m.group for m in moves}) >= 3:
        return "weak", "contested"
    if len(moves) == 1:
        return "medium", f"first assignment stood ({moves[0].by})"
    last, prev = moves[-1], moves[-2]
    if last.by == "person" and last.t - prev.t <= hours * 3600:
        return "strong", "corrected"
    return "medium", "late correction" if last.by == "person" else "moved by a trigger"


def _renewal_days(org: dict | None, t: float) -> int | None:
    """Days from `t` to the org's next annual renewal. The export has only today's renewal date; an annual contract
    renews on the same day every year, so step back whole years."""
    f = (org or {}).get("organization_fields") or {}
    if f.get("contract_term") != "annual" or not f.get("renewal_date"):
        return None
    d = datetime.fromisoformat(f["renewal_date"]).replace(tzinfo=timezone.utc)
    while d.replace(year=d.year - 1).timestamp() > t:
        d = d.replace(year=d.year - 1)
    return int((d.timestamp() - t) // 86400)


def build(tickets: Iterable[dict], audits: Iterable[dict], users: Iterable[dict], orgs: Iterable[dict],
          groups: Iterable[dict], hours: float = 24, catch_all: Iterable[str] = ("General",),
          keep: Iterable[str] = ("strong", "medium")) -> tuple[list[dict], dict]:
    """(dataset rows, report). Rows carry the creation snapshot, the label (group name as of today; ids are stable
    across renames), its tier, and how the first assignment was made."""
    by_t: dict[int, list] = defaultdict(list)
    for a in audits:
        by_t[a["ticket_id"]].append(a)
    users = list(users)
    agents = {u["id"] for u in users if u.get("role") in ("agent", "admin")}
    name = {u["id"]: u["name"] for u in users}
    orgs = {o["id"]: o for o in orgs}
    gname = {g["id"]: g["name"] for g in groups}
    catch = {gid for gid, n in gname.items() if n in set(catch_all)}
    keep = set(keep)
    tickets = sorted(tickets, key=lambda t: t["created_at"])

    reps = {t["id"]: replay(t, by_t[t["id"]], agents) for t in tickets}
    # the org's open tickets at each creation: created earlier, not yet solved (known at that moment)
    org_open: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for t in tickets:
        r = reps[t["id"]]
        if t.get("organization_id") and t["status"] != "deleted" and MERGED_TAG not in t["tags"]:
            org_open[t["organization_id"]].append((r.created, r.solved or float("inf")))

    rows, reasons, tiers = [], Counter(), Counter()
    first_seen: dict[str, float] = {}
    for t in tickets:
        r = reps[t["id"]]
        tier, why = classify(r, t["status"], t["tags"], hours, catch)
        tiers[tier] += 1
        reasons[why] += 1
        final = t.get("group_id")
        label = gname.get(final, str(final)) if final else None
        if label and tier in ("strong", "medium"):
            first_seen[label] = min(first_seen.get(label, r.created), r.created)
        oid = t.get("organization_id")
        org = orgs.get(oid)
        first = next((m for m in r.moves if m.group is not None), None)
        rows.append({
            "id": str(t["id"]), "created_at": datetime.fromtimestamp(r.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": r.channel, "subject": r.subject, "message": r.message,
            **{f"field_{k}": v for k, v in sorted(r.fields.items())},
            "org_plan": ((org or {}).get("organization_fields") or {}).get("plan"),
            "org_open_tickets": sum(1 for c, s in org_open.get(oid, []) if c < r.created < s) if oid else 0,
            "days_to_renewal": _renewal_days(org, r.created),
            "label": label, "tier": tier, "tier_reason": why,
            "first_group": gname.get(first.group) if first else None, "first_by": first.by if first else None,
            "first_agent": name.get(first.author) if first and first.by == "person" else None,
        })

    kept = [x for x in rows if x["tier"] in keep]
    agree = defaultdict(lambda: [0, 0])
    per_agent = defaultdict(lambda: [0, 0])
    for x in kept:
        a = agree[x["first_by"]]
        a[0] += x["first_group"] == x["label"]
        a[1] += 1
        if x["first_agent"]:
            p = per_agent[x["first_agent"]]
            p[0] += x["first_group"] == x["label"]
            p[1] += 1
    months = defaultdict(Counter)
    for x in kept:
        months[x["created_at"][:7]][x["label"]] += 1
    report = {
        "tickets": len(rows), "kept": len(kept), "tiers": dict(tiers), "reasons": dict(reasons.most_common()),
        "first_assignment_agrees": {k: {"right": v[0], "n": v[1], "rate": v[0] / v[1]} for k, v in agree.items()},
        "per_agent": {k: {"right": v[0], "n": v[1], "rate": v[0] / v[1]} for k, v in sorted(per_agent.items(), key=lambda kv: -kv[1][1])},
        "labels_by_month": {m: dict(c) for m, c in sorted(months.items())},
        "label_first_seen": {g: day(t) for g, t in sorted(first_seen.items(), key=lambda kv: kv[1])},
        "settings": {"hours": hours, "catch_all": sorted(catch_all), "keep": sorted(keep)},
    }
    return rows, report


def load_export(folder: str | Path) -> dict[str, list[dict]]:
    p = Path(folder)
    rd = lambda n: [json.loads(line) for line in (p / f"{n}.jsonl").open() if line.strip()]
    return {"tickets": rd("tickets"), "audits": rd("ticket_audits"), "users": rd("users"),
            "orgs": rd("organizations"), "groups": rd("groups")}


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Routing labels from a Zendesk export")
    ap.add_argument("export"), ap.add_argument("out", help="dataset .jsonl (kept rows); the report goes next to it")
    ap.add_argument("--hours", type=float, default=24), ap.add_argument("--catch-all", nargs="*", default=["General"])
    ap.add_argument("--all", action="store_true", help="write every row, excluded ones too, with their tier")
    a = ap.parse_args(argv)
    ex = load_export(a.export)
    rows, rep = build(ex["tickets"], ex["audits"], ex["users"], ex["orgs"], ex["groups"], a.hours, a.catch_all)
    out = Path(a.out)
    keep = rows if a.all else [x for x in rows if x["tier"] in ("strong", "medium")]
    out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in keep))
    out.with_suffix(".report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: rep[k] for k in ("tickets", "kept", "tiers", "reasons", "first_assignment_agrees")}, indent=1))


if __name__ == "__main__":
    main()
