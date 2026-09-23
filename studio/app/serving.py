"""Serving a published version (PLAN.md, Piece 2).

The contract this exists to hold: **one compile path for evaluation and production.** A served request builds the
task through the same `compile.build_task` and executes it with the same `bpto.run_program` the evaluator calls, so
"the prompt that was scored is the prompt that runs" is a property of the code, not a promise. When v1 splits
evaluation and production into separate pools, that split must not go through a second implementation of this.

v0 asterisks: synchronous and in-process (no queue, no worker pool, no reserved quota); no completion cache, so a
repeated request costs again; no per-version rate limit.
"""
from __future__ import annotations

import time
from typing import Any

from bpto import Budget, Dataset, Example, Prompt, run_program

from .clients import Access, make_client, prices
from .compile import build_task
from .models import ProjectSpec


def required_inputs(spec: ProjectSpec) -> list[str]:
    """The placeholders a caller must supply: every module's placeholders minus what an incoming edge fills before
    that module first runs. Same forward walk as `validation.check_mapping`, without a dataset in the picture."""
    from_edges: dict[str, set[str]] = {m.id: set() for m in spec.modules}
    seen: set[str] = set()
    todo = [spec.entry_id]
    while todo:
        x = todo.pop(0)
        if x in seen:
            continue
        seen.add(x)
        for e in spec.outgoing(x):
            if e.target not in seen:
                from_edges[e.target] |= from_edges[x] | set(e.mapping)
                todo.append(e.target)
    need: list[str] = []
    for m in spec.modules:
        try:
            phs = Prompt(template=m.template).placeholders
        except ValueError:
            continue
        for ph in phs:
            if ph not in from_edges.get(m.id, set()) and ph not in need:
                need.append(ph)
    return need


class MissingInputs(ValueError):
    def __init__(self, missing: list[str], required: list[str]):
        self.missing, self.required = missing, required
        super().__init__(f"missing inputs: {', '.join(missing)} (this version takes: {', '.join(required)})")


async def serve(spec: ProjectSpec, inputs: dict[str, Any], *, access: Access, mock=None, on_call=None) -> dict[str, Any]:
    """Run one request through the pinned program. Raises MissingInputs; model errors propagate to the caller so the
    route can record them on the trace."""
    required = required_inputs(spec)
    missing = [p for p in required if inputs.get(p) in (None, "")]
    if missing:
        raise MissingInputs(missing, required)
    # a request is bounded by the program's own step cap; anything beyond it is a loop, not a workload
    budget = Budget(max_calls=spec.max_steps + 2, prices=prices())
    client = make_client(spec.eval_model, budget=budget, access=access, on_call=on_call, purpose="serve", mock=mock)
    task = build_task(spec, Dataset([]), client)
    ex = Example(id="request", inputs={p: inputs.get(p) for p in required})
    t0 = time.perf_counter()
    r = await run_program(task, task.root, ex)
    return {"output": r.completion.text, "parsed": r.completion.parsed, "path": r.path, "metrics": dict(r.metrics),
            "input_tokens": client.usage.input_tokens, "output_tokens": client.usage.output_tokens,
            "usd": budget.spent_usd, "latency_s": round(time.perf_counter() - t0, 4)}
