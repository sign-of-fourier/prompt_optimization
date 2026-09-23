"""Traces + outcomes -> a dataset (PLAN.md, Piece 3).

The contract: a trace plus an outcome **is** a label, and promoting a set of them produces a dataset identical in
shape to an uploaded one - same table, same mapping, same validation, same Run button. Nothing downstream of here
knows whether rows came from a CSV or from production.

Each promoted row keeps its provenance: `trace_id` (the record it came from), `captured_at` (when the outcome
arrived) and `version_id` (what produced the answer that was corrected). Those ride along as dataset metadata -
they are not placeholders, so they never reach a prompt, but they are what makes a label auditable later.
"""
from __future__ import annotations

from typing import Any


def rows_from_traces(labelled: list[dict], label_column: str) -> list[dict[str, Any]]:
    """`labelled` is `store.labelled_traces` output, ordered by outcome time. A trace corrected twice keeps the last
    word: the second correction is the agent changing their mind, not a second example."""
    by_trace: dict[str, dict[str, Any]] = {}
    for t in labelled:
        label = t["o_label"] if t["o_label"] is not None else t["o_value"]
        if label is None or label == "":
            continue  # an outcome with no label (a rating, a reopen) is a signal, not a training row
        by_trace[t["id"]] = {**(t["inputs"] or {}), label_column: label,
                             "trace_id": t["id"], "version_id": t["version_id"], "captured_at": t["o_created"],
                             "label_source": t["o_source"]}
    return list(by_trace.values())
