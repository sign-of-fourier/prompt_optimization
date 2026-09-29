"""Time split and the hold-out's per-class report (app/splits.py)."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from httpx import ASGITransport, AsyncClient

from bpto import Dataset

from app import splits as SP
from app.datasets import to_dataset
from app.models import OptimizerSpec, ValidationReport
from app.validation import check_split
from test_api import SPEC, app


def test_parse_time():
    assert SP.parse_time("2026-09-29") == SP.parse_time("2026-09-29T00:00:00Z") == SP.parse_time(1790640000)
    assert SP.parse_time(1790640000000) == 1790640000.0            # milliseconds
    assert SP.parse_time("1790640000") == 1790640000.0
    assert SP.parse_time("2026-09-29T14:03:00+02:00") < SP.parse_time("2026-09-29T14:03:00Z")
    assert SP.parse_time("next tuesday") is None and SP.parse_time(None) is None and SP.parse_time(True) is None


ROWS = [{"id": f"t{i:02d}", "created": f"2026-09-{(i % 28) + 1:02d}", "msg": f"m{i}", "queue": "old" if i < 16 else "new"}
        for i in range(20)]


def test_time_split_holds_out_the_latest_rows():
    ds = to_dataset(ROWS, {"msg": "msg"}, "queue")
    train, held = SP.split(ds, 0.2, seed=0, mode="time", time_column="created")
    assert sorted(e.id for e in held.examples) == ["t16", "t17", "t18", "t19"]   # the four most recent dates
    assert SP.split(ds, 0.2, seed=0, mode="time", time_column="created")[1].examples == held.examples   # deterministic
    rnd = SP.split(ds, 0.2, seed=0)[1]
    assert len(rnd) == 4 and sorted(e.id for e in rnd.examples) != ["t16", "t17", "t18", "t19"]
    undated = ROWS + [{"id": "tx", "created": "someday", "msg": "m", "queue": "new"}]
    _, held2 = SP.split(to_dataset(undated, {"msg": "msg"}, "queue"), 0.2, 0, "time", "created")
    assert "tx" not in {e.id for e in held2.examples}                         # no date: treated as oldest, trained on
    assert SP.split(ds, 0.0, 0, "time", "created")[1].examples == []


def test_split_validation():
    spec = lambda **o: NS(optimizer=OptimizerSpec(**o))
    rep = ValidationReport(); check_split(spec(split="time"), ROWS, rep)
    assert not rep.ok and "date column" in rep.issues[0].message
    rep = ValidationReport(); check_split(spec(split="time", time_column="nope"), ROWS, rep)
    assert not rep.ok
    rep = ValidationReport(); check_split(spec(split="time", time_column="msg"), ROWS, rep)
    assert not rep.ok and "readable date" in rep.issues[0].message
    rep = ValidationReport(); check_split(spec(split="time", time_column="created"), ROWS + [{"created": "?"}], rep)
    assert rep.ok and any("go to training" in i.message for i in rep.issues)
    assert any("most recent 4 rows" in i.message and "2026-09-17 to 2026-09-20" in i.message for i in rep.issues)


def test_per_class_report():
    held = Dataset.from_records([{"id": str(i), "inputs": {}, "answer": a} for i, a in enumerate(["billing"] * 4 + ["refund"] * 2)])
    res = [NS(example_id=str(i), parsed={"queue": g}, output="", error=None)
           for i, g in enumerate(["billing", "billing", "refund", "billing", "billing", "refund"])]
    pc = SP.per_class(held, res, "queue")
    assert pc["classes"] == [{"label": "billing", "n": 4, "right": 3, "recall": 0.75}, {"label": "refund", "n": 2, "right": 1, "recall": 0.5}]
    assert pc["balanced_recall"] == pytest.approx(0.625)
    assert pc["confusions"] == [{"label": "billing", "answered": "refund", "n": 1}, {"label": "refund", "answered": "billing", "n": 1}]
    free = Dataset.from_records([{"id": str(i), "inputs": {}, "answer": f"free text {i}"} for i in range(60)])
    assert SP.per_class(free, [], None) is None                                  # not classes: no report


async def _time_run_flow():
    rows = [{"created": f"2026-08-{d:02d}", "context": f"Fact {d}.", "question": f"q{d}", "answer": "late" if d > 16 else ("a" if d % 2 else "b")}
            for d in range(1, 21)]
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/auth/signup", json={"email": "split@b.co", "password": "password1"})
            spec = {**SPEC, "optimizer": {**SPEC["optimizer"], "holdout_frac": 0.2, "split": "time", "time_column": "created"}}
            pid = (await c.post("/projects", json=spec)).json()["id"]
            did = (await c.post(f"/projects/{pid}/datasets", files={"file": ("d.jsonl", "\n".join(json.dumps(r) for r in rows).encode())})).json()["id"]
            await c.put(f"/datasets/{did}/mapping", json={"input_map": {"context": "context", "question": "question"}, "label_column": "answer"})
            v = (await c.post(f"/projects/{pid}/validate", json={"dataset_id": did})).json()
            assert v["ok"] and any("most recent 4 rows (2026-08-17 to 2026-08-20)" in i["message"] for i in v["report"]["issues"])
            rid = (await c.post(f"/projects/{pid}/runs", json={"dataset_id": did, "mock": True})).json()["id"]
            for _ in range(300):
                await asyncio.sleep(0.05)
                st = (await c.get(f"/runs/{rid}")).json()
                if st["live"]["state"] in ("done", "failed", "stopped"):
                    break
            assert st["live"]["state"] == "done", st
            ho = st["summary"]["holdout"]
            # the hold-out is exactly the four latest rows, all labelled "late"; the report is per class, for both prompts
            assert ho["split"] == "time" and [c["label"] for c in ho["per_class"]["best"]["classes"]] == ["late"]
            assert ho["per_class"]["best"]["classes"][0]["n"] == 4 and ho["per_class"]["root"] is not None


def test_time_split_run():
    asyncio.run(_time_run_flow())
