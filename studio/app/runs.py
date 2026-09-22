"""Background runs: one asyncio task per run in this process; artifacts under data/runs/<id>/
(tree.json checkpoint, events.jsonl, cache.jsonl, status.json). Resume = load the checkpoint and run again."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import traceback
from pathlib import Path
from typing import Any

from bpto import Budget, Dataset, EventLog, Tree, evaluate, run
from bpto.search import Stop

from . import db
from .clients import Access, make_client, prices
from .compile import build_schedule, build_task
from .datasets import to_dataset
from .models import ProjectSpec

log = logging.getLogger("studio.runs")
RUNS_DIR = db.DATA_DIR / "runs"


def node_summary(n) -> dict[str, Any]:
    ev = n.evaluation
    return {"id": n.id, "parent_id": n.parent_id, "depth": n.depth, "state": str(n.state), "origin": n.origin.model_dump(),
            "score": n.score, "metrics": ev.metrics if ev else None, "metrics_std": ev.metrics_std if ev else None,
            "n": ev.n if ev else None, "full": bool(ev and ev.n >= 0 and n.evaluation and len(ev.dataset_ids) > 0) if ev else False,
            "modules": {k: v.template for k, v in n.prompt.modules.items()}, "created_at": n.created_at.isoformat()}


class RunManager:
    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}
        self.trees: dict[str, Tree] = {}

    def start(self, con, run_id: str, spec: ProjectSpec, rows: list[dict], input_map: dict[str, str], label: str | None,
              mock: bool = False, pilot: dict | None = None, access: Access | None = None, on_call=None) -> None:
        self.tasks[run_id] = asyncio.create_task(self._run(con, run_id, spec, rows, input_map, label, mock, pilot, access, on_call))

    def stop(self, run_id: str) -> bool:
        t = self.tasks.get(run_id)
        if t and not t.done():
            t.cancel()
            return True
        return False

    async def _run(self, con, run_id, spec, rows, input_map, label, mock, pilot, access, on_call=None):
        d = RUNS_DIR / run_id
        d.mkdir(parents=True, exist_ok=True)
        o = spec.optimizer
        status: dict[str, Any] = {"state": "running", "round": 0, "step": "", "best": None, "history": [], "usage": {}, "spent_usd": 0.0}
        def flush():  # atomic: the UI polls this file while the run writes it
            tmp = d / "status.json.tmp"
            tmp.write_text(json.dumps(status, default=str))
            tmp.replace(d / "status.json")
        try:
            full = to_dataset(rows, input_map, label)
            train, held = full.split(1 - o.holdout_frac, seed=o.seed) if o.holdout_frac > 0 else (full, Dataset([]))
            if o.eval_rows:
                train = train.sample(o.eval_rows, o.seed)
            budget = Budget(max_usd=o.max_usd, max_calls=o.max_calls, prices=prices())
            client = make_client(spec.eval_model, cache_path=d / "cache.jsonl", budget=budget, access=access, on_call=on_call, purpose="run",
                                 mock=__import__("app.mock", fromlist=["mock_client"]).mock_client() if mock else None)
            task = build_task(spec, train, client)
            embedder = None
            if o.engine == "bo":
                if mock:
                    from bpto.bo import HashEmbedder
                    embedder = HashEmbedder(dim=64)
                else:
                    from bpto.bo import BedrockEmbedder
                    embedder = BedrockEmbedder(region=__import__("os").environ.get("AWS_REGION", "us-east-1"))
            schedule, stop = build_schedule(spec, task, embedder=embedder)
            ckpt = d / "tree.json"
            tree = Tree.load(ckpt, task) if ckpt.exists() else Tree(task)
            self.trees[run_id] = tree
            EventLog(d / "events.jsonl", tree)
            status.update({"train_rows": len(train), "holdout_rows": len(held), "nondeterminism_band": (pilot or {}).get("nondeterminism_band"),
                           "tier": access.tier if access else None, "max_concurrency": client._sem._value})
            flush()

            train_ids = {ex.id for ex in train}

            def best_full(t):
                """Top score among nodes evaluated on the whole training set. `Tree.best()` also ranks children scored on the
                check batch only, and five rows can outscore forty; the star in the tree marks those, the headline must not."""
                full = [n for n in t.evaluated_nodes() if n.evaluation.feasible and set(n.evaluation.dataset_ids) >= train_ids]
                return max(full, key=lambda n: n.score) if full else None

            def on_step(t, r, st):
                best = best_full(t)
                status.update({"round": r, "step": st.name, "usage": client.usage.model_dump(), "spent_usd": budget.spent_usd,
                               "best": node_summary(best) if best else None, "nodes": len(t)})
                proposed = sum(1 for n in t if n.origin.op == "reflect")
                accepted = sum(1 for n in t if n.origin.op == "reflect" and n.evaluation and set(n.evaluation.dataset_ids) >= train_ids)
                status["accepted"], status["proposed"] = accepted, proposed
                status["history"].append({"round": r, "step": st.name, "best": best.score if best else None, "calls": client.usage.calls,
                                          "usd": budget.spent_usd, "nodes": len(t)})
                flush()

            res = await run(tree, schedule, stop=stop, checkpoint=ckpt, on_step=on_step)
            best = best_full(tree)
            summary = {"stopped_because": res.stopped_because, "rounds": res.rounds, "seconds": res.seconds, "nodes": len(tree),
                       "best": node_summary(best) if best else None, "root_score": tree.root.score, "usage": client.usage.model_dump(),
                       "spent_usd": budget.spent_usd, "accepted": status.get("accepted"), "proposed": status.get("proposed")}
            if best is not None and len(held):
                held_best = await evaluate(dataset=held).score(tree, best)
                held_root = await evaluate(dataset=held).score(tree, tree.root)
                summary["holdout"] = {"best": held_best.metrics, "root": held_root.metrics, "best_score": held_best.score, "root_score": held_root.score,
                                      "se": {k: v / math.sqrt(max(1, held_best.n)) for k, v in held_best.metrics_std.items()}}
            # commit the row before the status file says "done": readers poll the file, then read the row
            con.execute("update runs set status='done', finished=?, summary=? where id=?", (db.now(), json.dumps(summary, default=str), run_id))
            con.commit()
            status.update({"state": "done", "summary": summary})
            flush()
        except asyncio.CancelledError:
            status.update({"state": "stopped"}); flush()
            con.execute("update runs set status='stopped', finished=? where id=?", (db.now(), run_id)); con.commit()
            raise
        except Exception as e:
            log.error("run %s failed: %s", run_id, traceback.format_exc())
            status.update({"state": "failed", "error": f"{type(e).__name__}: {e}"}); flush()
            con.execute("update runs set status='failed', finished=?, error=? where id=?", (db.now(), f"{type(e).__name__}: {e}", run_id)); con.commit()
        finally:
            self.tasks.pop(run_id, None)


def read_status(run_id: str) -> dict[str, Any]:
    p = RUNS_DIR / run_id / "status.json"
    if not p.exists():
        return {"state": "unknown"}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"state": "running"}


def read_tree(run_id: str) -> dict[str, Any]:
    p = RUNS_DIR / run_id / "tree.json"
    if not p.exists():
        return {"nodes": []}
    data = json.loads(p.read_text())
    nodes = []
    for n in data["nodes"]:
        ev = n.get("evaluation")
        nodes.append({"id": n["id"], "parent_id": n.get("parent_id"), "depth": n["depth"], "state": n["state"], "origin": n["origin"],
                      "score": ev["score"] if ev else None, "metrics": ev["metrics"] if ev else None, "metrics_std": ev["metrics_std"] if ev else None,
                      "n": ev["n"] if ev else None, "modules": {k: v["template"] for k, v in n["prompt"]["modules"].items()} if "modules" in n["prompt"] else {"prompt": n["prompt"]["template"]},
                      "created_at": n.get("created_at")})
    return {"root": data["root"], "nodes": nodes, "meta": data.get("meta", {})}


def read_node(run_id: str, node_id: str) -> dict[str, Any] | None:
    p = RUNS_DIR / run_id / "tree.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    by_id = {n["id"]: n for n in data["nodes"]}
    n = by_id.get(node_id)
    if not n:
        return None
    parent = by_id.get(n.get("parent_id") or "")
    return {"node": n, "parent_modules": {k: v["template"] for k, v in parent["prompt"]["modules"].items()} if parent and "modules" in parent["prompt"] else None}


def read_events(run_id: str, after: int = 0) -> list[dict]:
    p = RUNS_DIR / run_id / "events.jsonl"
    if not p.exists():
        return []
    with open(p) as f:
        lines = f.readlines()
    return [json.loads(l) for l in lines[after:] if l.strip()]
