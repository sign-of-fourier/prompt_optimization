"""Versions (PLAN.md, Piece 1): the immutable thing everything downstream points at.

A version is a fully pinned snapshot - spec + prompt templates + concrete model ids + the dataset it was scored on
+ the score it earned - created by promoting a run's node or the current canvas. It is never edited: `store.py`
has no update statement, and re-fetching a version returns the same prompts that earned its score.

What is *not* pinned yet, and must be by the time it matters: external-step ids and versions (Piece 4 - a manifest
id per external node goes in `spec` the same way a model id does), and the dataset's content hash rather than its
id (a dataset is mutable today: its rows file can be replaced under the same id).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import ProjectSpec

# The behavioural part of a spec: two versions with the same fingerprint execute identically. Deliberately excludes
# name, layout and the optimizer block - those describe how the version was produced, not what it does at serve time.
BEHAVIOUR = ("modules", "edges", "entry", "max_steps", "eval_model", "evaluate")


def pin(spec: ProjectSpec, templates: dict[str, str] | None = None) -> ProjectSpec:
    """A copy of `spec` with `templates` (module id -> template, e.g. a run node's prompts) applied and every model
    reference made concrete, so nothing about the version depends on a default that may change later."""
    s = spec.model_copy(deep=True)
    for m in s.modules:
        if templates and m.id in templates:
            m.template = templates[m.id]
        m.model = m.model or s.eval_model
    for sc in s.evaluate.scorers:
        if sc.judge_model is None and sc.type.startswith("llm_judge"):
            sc.judge_model = s.eval_model
    s.optimizer.critic_model = s.optimizer.critic_model or s.optimizer.reflect_model
    s.layout = {}  # canvas positions are not part of what runs
    return s


def fingerprint(spec: ProjectSpec) -> str:
    d = spec.model_dump(mode="json")
    return hashlib.sha256(json.dumps({k: d[k] for k in BEHAVIOUR}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def from_run(run: dict, node: dict | None) -> dict[str, Any]:
    """The score side of a version published from a run node: what it earned, on which rows, and where it came from.
    `node` is a `runs.node_summary` dict; None means the run's own best."""
    summary = run.get("summary") or {}
    n = node or summary.get("best") or {}
    full = summary.get("best") or {}
    return {
        "templates": {k: v for k, v in (n.get("modules") or {}).items()},
        "source": {"kind": "run_node", "run_id": run["id"], "node_id": n.get("id"), "op": (n.get("origin") or {}).get("op")},
        "dataset_id": run["dataset_id"],
        "score": n.get("score"),
        "metrics": n.get("metrics") or {},
        "n_rows": n.get("n"),
        # the hold-out numbers belong to the run's best node only; a different node was never scored on those rows
        "holdout": summary.get("holdout") if (n.get("id") and n.get("id") == full.get("id")) else None,
    }
