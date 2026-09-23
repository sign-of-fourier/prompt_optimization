"""External steps: the transport, the frozen-enrichment contract, and the validation that is supposed to stop you
wiring up a step that teaches nothing (EXTERNAL-STEPS.md §8).

A real HTTP server on a real socket, not a stubbed client: the retry, the 404-means-missing rule and the schema
check are the parts most likely to be wrong, and none of them are exercised by faking the transport away.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from app import steps as X
from app import step_validation as SV
from app.models import EvaluateSpec, ModuleSpec, ProjectSpec, ScorerSpec, StepSpec, ValidationReport

KEY = "test-key"
RECORDS = {
    "C1": {"plan": "enterprise", "open_tickets": 3, "score": 10},
    "C2": {"plan": "free", "open_tickets": 0, "score": 20},
    "C3": {"plan": "pro", "open_tickets": 1, "score": 30},
}
CALLS: list[dict] = []


class Lookup(BaseModel):
    customer_id: str


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/records")
    async def records(b: Lookup, authorization: str | None = Header(default=None)):
        CALLS.append({"customer_id": b.customer_id})
        if (authorization or "") != f"Bearer {KEY}":
            raise HTTPException(401, "bad key")
        if b.customer_id == "BOOM":
            raise HTTPException(500, "kaboom")
        if b.customer_id == "WRONG":
            return {"plan": "enterprise"}                    # missing a declared field
        if b.customer_id == "BADENUM":
            return {"plan": "platinum", "open_tickets": 1, "score": 1}
        rec = RECORDS.get(b.customer_id)
        if rec is None:
            raise HTTPException(404, "no record")
        return rec

    return app


@pytest.fixture(scope="module")
def server():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cfg = uvicorn.Config(_app(), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(200):
        if srv.started:
            break
        import time; time.sleep(0.02)
    d = Path(tempfile.mkdtemp())
    (d / "rec.json").write_text(json.dumps({
        "step": 1, "id": "rec", "version": "1.0.0", "name": "records",
        "transport": {"kind": "http", "method": "POST", "url": f"http://127.0.0.1:{port}/records", "timeout_s": 3, "retries": 1},
        "auth": {"kind": "bearer"}, "missing": {"status": 404, "means": "no record"},
        "inputs": [{"name": "customer_id", "type": "string", "required": True}],
        "outputs": [{"name": "plan", "type": "string", "enum": ["free", "pro", "enterprise"]},
                    {"name": "open_tickets", "type": "number"}, {"name": "score", "type": "number"}],
        "cacheable": True, "cache_key": ["customer_id"], "price_usd_per_call": 0.001,
    }))
    os.environ["STUDIO_STEPS"] = str(d)
    yield
    srv.should_exit = True
    os.environ.pop("STUDIO_STEPS", None)


def spec(**over) -> ProjectSpec:
    base = dict(
        name="triage",
        modules=[ModuleSpec(id="route", template="Queue for: {message}. Plan {rec_plan}, open {rec_open_tickets}.",
                            description="routes a ticket", schema_fields=[])],
        steps=[StepSpec(id="rec", manifest="rec@1.0.0", inputs={"customer_id": "customer_id"}, credential_id="cred1")],
        evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match")], label_column="queue"),
    )
    base.update(over)
    return ProjectSpec(**base)


# ---- transport -------------------------------------------------------------------------------

def test_call_success_missing_and_failures(server):
    m = X.load_manifest("rec@1.0.0")
    assert m is not None and X.load_manifest("rec@9.9.9") is None   # a version that moved is not silently accepted

    out, met = asyncio.run(X.call(m, {"customer_id": "C1"}, secret=KEY))
    assert out == {"plan": "enterprise", "open_tickets": 3, "score": 10}
    assert met["usd"] == 0.001 and met["latency_s"] >= 0 and met["missing"] == 0.0

    # "no record" is a declared answer, not a failure: production is full of unknown ids
    out, met = asyncio.run(X.call(m, {"customer_id": "NOPE"}, secret=KEY))
    assert out is None and met["missing"] == 1.0

    for cid, fragment in [("BOOM", "500"), ("WRONG", "missing the declared field"), ("BADENUM", "not one of")]:
        with pytest.raises(X.StepError) as e:
            asyncio.run(X.call(m, {"customer_id": cid}, secret=KEY))
        assert fragment in str(e.value)

    with pytest.raises(X.StepError) as e:
        asyncio.run(X.call(m, {"customer_id": "C1"}, secret="wrong"))
    assert "credential" in str(e.value)


# ---- enrichment ------------------------------------------------------------------------------

def test_enrich_freezes_caches_and_survives_a_failure(server):
    sp = spec()
    rows = [{"message": "m", "customer_id": c} for c in ["C1", "C2", "C1", "C1", "NOPE", "BOOM"]]
    CALLS.clear()
    out, rep = asyncio.run(X.enrich(sp, sp.steps[0], rows, secret=KEY))

    # six rows, four distinct ids: the declared cache key means four calls, not six
    assert len(CALLS) == 4 and rep["calls"] == 4 and rep["cached"] == 2
    assert rep["missing"] == 1 and rep["failed"] == 1 and rep["rows"] == 6
    assert rep["coverage"] == pytest.approx(4 / 6)
    assert rep["usd"] == pytest.approx(0.003)   # three calls answered; the failed one is not counted (a real
                                               # provider might still charge for it - the manifest price is ours, not theirs)

    assert out[0]["rec_plan"] == "enterprise" and out[0]["rec_open_tickets"] == 3
    assert out[4]["rec_plan"] is None                       # unknown id: present, empty, not an error
    assert "rec__error" in out[5] and "500" in out[5]["rec__error"]   # one bad row, not a dead run
    assert all("rec__captured_at" in r for r in out)
    assert rows[0] == {"message": "m", "customer_id": "C1"}  # the source rows are never mutated


# ---- what a caller must send -----------------------------------------------------------------

def test_required_inputs_excludes_what_the_step_provides(server):
    from app.serving import required_inputs
    need = required_inputs(spec())
    assert "message" in need and "customer_id" in need       # the step's input is the caller's job
    assert "rec_plan" not in need and "rec_open_tickets" not in need   # fetched, not sent


# ---- tier 0: static ---------------------------------------------------------------------------

def test_static_checks(server):
    cols = ["message", "customer_id", "queue"]

    rep = ValidationReport(); SV.check_steps(spec(), cols, rep)
    assert not [i for i in rep.issues if i.level == "error"]

    bad = spec(); bad.steps[0].inputs = {}
    rep = ValidationReport(); SV.check_steps(bad, cols, rep)
    assert any("not mapped" in i.message for i in rep.issues if i.level == "error")

    bad = spec(); bad.steps[0].credential_id = None
    rep = ValidationReport(); SV.check_steps(bad, cols, rep)
    assert any("credential" in i.message for i in rep.issues if i.level == "error")

    rep = ValidationReport(); SV.check_steps(spec(), cols + ["rec_plan"], rep)   # a column we did not put there
    assert any("collides" in i.message for i in rep.issues if i.level == "error")

    # nothing in the prompt reads the step's output: you are paying for data nobody looks at
    unused = spec(modules=[ModuleSpec(id="route", template="Queue for: {message}.", description="routes")])
    rep = ValidationReport(); SV.check_steps(unused, cols, rep)
    assert any("nobody reads" in i.message for i in rep.issues if i.level == "warn")

    # evaluation reads the frozen snapshot, so the dataset has to carry it
    rep = ValidationReport(); SV.check_frozen(spec(), cols, rep)
    assert any("has not been enriched" in i.message for i in rep.issues if i.level == "error")
    rep = ValidationReport(); SV.check_frozen(spec(), cols + ["rec_plan", "rec_open_tickets", "rec_score"], rep)
    assert not rep.issues


# ---- tier 2: statistics -----------------------------------------------------------------------

def _rows(n=120):
    """A field that matters (plan), one that is constant, one that is noise, one that is the label in disguise."""
    import random
    rnd = random.Random(7)
    out = []
    for i in range(n):
        plan = rnd.choice(["free", "pro", "enterprise"])
        q = "escalation" if plan == "enterprise" else rnd.choice(["bug", "billing"])
        out.append({"queue": q, "rec_plan": plan, "rec_open_tickets": 1, "rec_score": rnd.randint(0, 999),
                    "rec_leak": q})
    return out


def test_signal_degeneracy_and_leakage(server):
    sp = spec()
    sp.steps[0].manifest = "rec@1.0.0"
    rows = _rows()
    # widen the manifest's declared outputs for this check by naming them on the spec side
    m = X.load_manifest("rec@1.0.0")
    m.outputs.append(X.StepField(name="leak", type="string"))
    import unittest.mock as mock
    with mock.patch.object(X, "load_manifest", return_value=m), mock.patch.object(SV, "load_manifest", return_value=m):
        rep = ValidationReport()
        SV.check_signal(sp, rows, "queue", rep)
        msgs = {i.where + ":" + i.message for i in rep.issues}
        assert any("rec_plan carries signal" in m_ for m_ in msgs)
        assert any("rec_open_tickets is the same on every row" in m_ for m_ in msgs)
        assert any("rec_leak predicts the label" in m_ and "the answer" in m_ for m_ in msgs)
        assert [i.level for i in rep.issues if "rec_leak" in i.message] == ["error"]

        rep = ValidationReport()
        SV.check_coverage(sp, [{**r, "rec_plan": None} if i < 80 else r for i, r in enumerate(rows)], rep)
        assert any("only 33% of rows have a record" in i.message for i in rep.issues if i.level == "error")


def test_a_field_that_only_matters_in_combination_is_not_condemned(server):
    """The trap this check nearly walked into: the label depends on plan AND open_tickets together, so each field
    alone looks like noise. A marginal test would tell the user to delete the thing the task depends on."""
    import random
    import unittest.mock as mock
    rnd = random.Random(3)
    rows = []
    for _ in range(160):
        plan = rnd.choice(["free", "pro", "enterprise"])
        open_t = rnd.choice([0, 3])
        joint = plan == "enterprise" and open_t == 3
        rows.append({"queue": "escalation" if joint else rnd.choice(["bug", "billing", "shipping", "other"]),
                     "rec_plan": plan, "rec_open_tickets": open_t, "rec_score": rnd.randint(0, 5)})
    m = X.load_manifest("rec@1.0.0")
    with mock.patch.object(SV, "load_manifest", return_value=m):
        rep = ValidationReport()
        SV.check_signal(spec(), rows, "queue", rep)
    alone = [i for i in rep.issues if "on its own" in i.message]
    assert alone and all(i.level == "info" for i in alone)          # never a warning on the strength of a marginal test
    assert any("may still matter combined with the input text" in i.message for i in alone)
    together = [i for i in rep.issues if "taken together" in i.message]
    assert len(together) == 1 and together[0].level == "info"       # the interaction is found
    # `info` here means the permutation test called it significant; the margin is modest because the statistic is
    # cross-validated - a rule fitted and scored on the same rows would report a much larger and much less real gap
    assert together[0].data["accuracy"] > together[0].data["baseline"]


def test_a_step_that_carries_nothing_is_called_out(server):
    """The other side of it: fields that are noise alone and noise together get one clear warning, not five."""
    import random
    import unittest.mock as mock
    rnd = random.Random(5)
    rows = [{"queue": rnd.choice(["bug", "billing", "shipping", "other"]),
             "rec_plan": rnd.choice(["free", "pro"]), "rec_open_tickets": rnd.choice([0, 1]),
             "rec_score": rnd.choice([1, 2])} for _ in range(160)]
    m = X.load_manifest("rec@1.0.0")
    with mock.patch.object(SV, "load_manifest", return_value=m):
        rep = ValidationReport()
        SV.check_signal(spec(), rows, "queue", rep)
    warns = [i for i in rep.issues if i.level == "warn"]
    assert len(warns) == 1 and "alone or in combination" in warns[0].message and "pilot" in warns[0].message


def test_a_noisy_field_does_not_look_useful_just_for_having_many_values(server):
    """The reason the statistic is cross-validated: a value spread over many small groups (25 region codes here) is memorisable, so a rule
    fitted and scored on the same rows calls it informative. Not wide enough to be caught as "unique on every row" -
    that is a different check - just noisy enough to be flattering if nothing is held out."""
    import random
    import unittest.mock as mock
    rnd = random.Random(11)
    rows = [{"queue": "escalation" if plan == "enterprise" else rnd.choice(["bug", "billing", "shipping"]),
             "rec_plan": plan, "rec_open_tickets": rnd.choice([0, 1]), "rec_score": f"r{rnd.randint(0, 24)}"}
            for plan in (rnd.choice(["free", "pro", "enterprise"]) for _ in range(200))]
    m = X.load_manifest("rec@1.0.0")
    with mock.patch.object(SV, "load_manifest", return_value=m):
        rep = ValidationReport()
        SV.check_signal(spec(), rows, "queue", rep)
    # the same statistic, fitted and scored on the same rows, calls this grouping 80% accurate; held out it is 46%
    groups = [(r["rec_plan"], r["rec_open_tickets"], r["rec_score"]) for r in rows]
    labels = [r["queue"] for r in rows]
    assert SV._majority_accuracy(list(zip(groups, labels)), folds=1) > SV._majority_accuracy(list(zip(groups, labels))) + 0.2
    redundant = [i for i in rep.issues if i.data.get("redundant")]
    assert redundant and "rec_score" in redundant[0].data["redundant"]     # noise, however many values it has
    kept = [i.message for i in rep.issues if "cannot replace it" in i.message]
    assert any("rec_plan" in k for k in kept)                              # and the field that decides the label stays
