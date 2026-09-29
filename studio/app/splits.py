"""Hold-out splits and the hold-out's per-class report.

A random hold-out leaks the future when rows have dates: routing policy, products and phrasing drift, and a model
scored on rows interleaved with its training rows looks better than it will on next month's traffic. `split="time"`
holds out the most recent share of rows by a date column instead - the only honest split for tickets, leads, anything
with a timeline. The per-class report says which labels the hold-out gets wrong and what it confuses them with:
accuracy hides the long tail, which is where misroutes cost the most.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from bpto import Dataset

from . import scorers as S


def parse_time(v: Any) -> float | None:
    """Epoch seconds (or milliseconds), or an ISO-8601 date/datetime ("2026-09-29", "2026-09-29T14:03:00Z").
    None when it is neither: the validator reports those rows rather than guessing where they belong."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e11 else 1.0)
    s = str(v).strip()
    try:
        return parse_time(float(s))
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _time_of(ex, column: str) -> float | None:
    v = (ex.meta or {}).get(column)
    if v is None:
        v = (ex.inputs or {}).get(column)
    return parse_time(v)


def split(full: Dataset, holdout_frac: float, seed: int, mode: str = "random", time_column: str | None = None) -> tuple[Dataset, Dataset]:
    """(train, hold-out). `time`: the most recent `holdout_frac` of rows by `time_column` (ties broken by id, so the
    split is the same every time); rows without a readable time go to training, oldest-first, and the validator has
    already said so."""
    if holdout_frac <= 0:
        return full, Dataset([])
    if mode != "time" or not time_column:
        return full.split(1 - holdout_frac, seed=seed)
    ex = sorted(full.examples, key=lambda e: (_time_of(e, time_column) is not None, _time_of(e, time_column) or 0.0, str(e.id)))
    n = int(len(ex) * (1 - holdout_frac))
    return Dataset(ex[:n]), Dataset(ex[n:])


def describe(rows: list[dict[str, Any]], holdout_frac: float, time_column: str) -> dict[str, Any]:
    """For the validator: how many rows have a readable time, and the date ranges training and hold-out will cover."""
    ts = sorted(t for t in (parse_time(r.get(time_column)) for r in rows) if t is not None)
    out: dict[str, Any] = {"rows": len(rows), "dated": len(ts), "undated": len(rows) - len(ts)}
    if ts:
        n = int(len(ts) * (1 - holdout_frac))
        day = lambda t: datetime.fromtimestamp(t, timezone.utc).date().isoformat()
        out.update({"train_from": day(ts[0]), "train_to": day(ts[max(0, n - 1)]),
                    "holdout_from": day(ts[min(n, len(ts) - 1)]), "holdout_to": day(ts[-1]), "holdout_rows": len(ts) - n})
    return out


def per_class(held: Dataset, results: list, field: str | None, normalize: bool = True, top: int = 8) -> dict[str, Any] | None:
    """Per-label recall on the hold-out and the most frequent confusions (label -> what was answered instead).
    `results` are the hold-out ExampleResults of one node. None when the labels are not classes (too many distinct)."""
    norm = S.normalize if normalize else (lambda x: str(x if x is not None else ""))
    truth = {str(e.id): norm(e.answer) for e in held.examples}
    if not truth or len(set(truth.values())) > max(20, len(truth) // 3):
        return None
    got: dict[str, str] = {}
    for r in results:
        p = r.parsed
        if isinstance(p, dict) and field:
            p = p.get(field)
        elif isinstance(p, dict) and len(p) == 1:
            p = next(iter(p.values()))
        got[str(r.example_id)] = "(error)" if r.error else norm(p if p is not None else r.output)
    n, hit, conf = Counter(), Counter(), Counter()
    for eid, t in truth.items():
        g = got.get(eid, "(missing)")
        n[t] += 1
        if g == t:
            hit[t] += 1
        else:
            conf[(t, g)] += 1
    classes = [{"label": c, "n": n[c], "right": hit[c], "recall": hit[c] / n[c]} for c in sorted(n, key=lambda c: (-n[c], c))]
    return {"classes": classes, "balanced_recall": sum(c["recall"] for c in classes) / len(classes),
            "confusions": [{"label": a, "answered": b, "n": k} for (a, b), k in conf.most_common(top)]}
