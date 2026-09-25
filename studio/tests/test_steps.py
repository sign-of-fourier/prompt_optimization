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
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
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
        steps=[StepSpec(id="rec", manifest="rec@1.0.0", inputs={"customer_id": "customer_id"}, credential="step:cred1")],
        evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match")], label_column="queue"),
    )
    base.update(over)
    return ProjectSpec(**base)


# ---- transport -------------------------------------------------------------------------------

def test_call_success_missing_and_failures(server):
    m = X.load_manifest("rec@1.0.0")
    assert m is not None and X.load_manifest("rec@9.9.9") is None   # a version that moved is not silently accepted

    out, met = asyncio.run(X.call(m, {"customer_id": "C1"}, token=KEY))
    assert out == {"plan": "enterprise", "open_tickets": 3, "score": 10}
    assert met["usd"] == 0.001 and met["latency_s"] >= 0 and met["missing"] == 0.0

    # "no record" is a declared answer, not a failure: production is full of unknown ids
    out, met = asyncio.run(X.call(m, {"customer_id": "NOPE"}, token=KEY))
    assert out is None and met["missing"] == 1.0

    for cid, fragment in [("BOOM", "500"), ("WRONG", "missing the declared field"), ("BADENUM", "not one of")]:
        with pytest.raises(X.StepError) as e:
            asyncio.run(X.call(m, {"customer_id": cid}, token=KEY))
        assert fragment in str(e.value)

    with pytest.raises(X.StepError) as e:
        asyncio.run(X.call(m, {"customer_id": "C1"}, token="wrong"))
    assert "credential" in str(e.value)


# ---- enrichment ------------------------------------------------------------------------------

def test_enrich_freezes_caches_and_survives_a_failure(server):
    sp = spec()
    rows = [{"message": "m", "customer_id": c} for c in ["C1", "C2", "C1", "C1", "NOPE", "BOOM"]]
    CALLS.clear()
    out, rep = asyncio.run(X.enrich(sp, sp.steps[0], rows, token=KEY))

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

    bad = spec(); bad.steps[0].credential = None
    rep = ValidationReport(); SV.check_steps(bad, cols, rep)
    assert any("paste a step key" in i.message for i in rep.issues if i.level == "error")

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


# ---- a step authorised by an OAuth grant --------------------------------------------------------

def _oauth_manifest():
    """The same records service, but declaring that it acts on the user's own account."""
    import json
    d = X.steps_dir()
    (d / "hs.json").write_text(json.dumps({
        "step": 1, "id": "hs", "version": "1.0.0", "name": "HubSpot tickets",
        "transport": json.loads(X.load_manifest("rec@1.0.0").transport.model_dump_json()),
        "auth": {"kind": "oauth", "provider": "hubspot", "scopes": ["oauth", "crm.objects.tickets.read"]},
        "missing": {"status": 404}, "inputs": [{"name": "customer_id", "required": True}],
        "outputs": [{"name": "plan"}, {"name": "open_tickets", "type": "number"}, {"name": "score", "type": "number"}],
        "cacheable": True, "cache_key": ["customer_id"],
    }))
    return X.load_manifest("hs@1.0.0")


def _oauth_spec(credential):
    from app.models import EvaluateSpec, ModuleSpec, ProjectSpec, ScorerSpec, StepSpec
    return ProjectSpec(name="t", modules=[ModuleSpec(id="route", template="{message} {hs_plan}", description="routes")],
                       steps=[StepSpec(id="hs", manifest="hs@1.0.0", inputs={"customer_id": "customer_id"}, credential=credential)],
                       evaluate=EvaluateSpec(scorers=[ScorerSpec()], label_column="queue"))


def test_the_token_is_fetched_for_every_call_not_once(server):
    """An access token lasts thirty minutes; an enrichment can outlive it. The step must ask for a token per call so
    the refresh inside `oauth.access_token` gets its chance, rather than pinning one value at the top of the run."""
    m = _oauth_manifest()
    asked = []

    async def token():
        asked.append(1)
        return KEY

    rows = [{"message": "m", "customer_id": c} for c in ["C1", "C2", "C3"]]
    out, rep = asyncio.run(X.enrich(_oauth_spec("conn:abc"), _oauth_spec("conn:abc").steps[0], rows, token=token))
    assert rep["calls"] == 3 and len(asked) == 3          # once per call, not once per run
    assert out[0]["hs_plan"] == "enterprise"


def test_an_oauth_step_demands_a_connection_not_a_pasted_key(server):
    _oauth_manifest()
    cols = ["message", "customer_id", "queue"]
    for cred in (None, "step:some-key"):
        rep = ValidationReport(); SV.check_steps(_oauth_spec(cred), cols, rep)
        assert any("acts on your own hubspot account" in i.message for i in rep.issues if i.level == "error")
    rep = ValidationReport(); SV.check_steps(_oauth_spec("conn:abc"), cols, rep)
    assert not [i for i in rep.issues if i.level == "error"]


def test_missing_scopes_are_caught_before_anything_is_spent(server):
    _oauth_manifest()
    spec = _oauth_spec("conn:abc")
    full = {"id": "abc", "provider": "hubspot", "scopes": "oauth crm.objects.tickets.read"}

    rep = ValidationReport(); SV.check_scopes(spec, [full], rep)
    assert not rep.issues

    rep = ValidationReport(); SV.check_scopes(spec, [{**full, "scopes": "oauth"}], rep)
    e = [i for i in rep.issues if i.level == "error"]
    assert e and e[0].data["missing"] == ["crm.objects.tickets.read"] and "reconnect" in e[0].message

    rep = ValidationReport(); SV.check_scopes(spec, [{**full, "provider": "salesforce"}], rep)
    assert any("but points at a salesforce one" in i.message for i in rep.issues if i.level == "error")

    rep = ValidationReport(); SV.check_scopes(spec, [], rep)
    assert any("no longer exists" in i.message for i in rep.issues if i.level == "error")


# ---- calling an API that was not written for us -------------------------------------------------

HUBSPOT_SEEN: list[dict] = []


def _hubspot_app():
    """HubSpot's contact search, in the shape it actually has: a filter body in, a nested envelope out, and "not
    found" expressed as a 200 with an empty list rather than a 404."""
    app = FastAPI()

    @app.post("/crm/v3/objects/contacts/search")
    async def search(request: Request):
        body = await request.json()
        HUBSPOT_SEEN.append(body)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return JSONResponse({"status": "error", "message": "unauthorized"}, status_code=401)
        email = body["filterGroups"][0]["filters"][0]["value"]
        if email != "ada@example.com":
            return {"total": 0, "results": []}
        return {"total": 1, "results": [{"id": "701", "properties": {
            "email": email, "firstname": "Ada", "lastname": "Lovelace", "company": "Analytical Engines",
            "lifecyclestage": "customer", "createdate": "2026-01-02T00:00:00Z"}}]}

    return app


@pytest.fixture(scope="module")
def hubspot():
    import json
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(_hubspot_app(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        import time as _t; _t.sleep(0.02)
    # the manifest that ships, with only its url redirected - and written to a directory this fixture owns, never
    # to the repo's. A test that edits a shipped file is a worse bug than the one it is checking.
    real = json.loads((Path(__file__).resolve().parent.parent / "steps" / "hubspot-contact.json").read_text())
    assert real["transport"]["url"].startswith("https://api.hubapi.com"), "the shipped manifest was overwritten"
    real["transport"]["url"] = f"http://127.0.0.1:{port}/crm/v3/objects/contacts/search"
    d = Path(tempfile.mkdtemp())
    (d / "hubspot-contact.json").write_text(json.dumps(real))
    before = os.environ.get("STUDIO_STEPS")
    os.environ["STUDIO_STEPS"] = str(d)
    yield
    srv.should_exit = True
    if before is None:
        os.environ.pop("STUDIO_STEPS", None)
    else:
        os.environ["STUDIO_STEPS"] = before


def test_the_shipped_hubspot_manifest_against_hubspots_own_shapes(hubspot):
    """The demo records service was written to fit our transport. A real API is not, and this is the manifest that
    ships - only its url is redirected at a local stand-in with HubSpot's request and response shapes."""
    m = X.load_manifest("hubspot-contact@1.0.0")
    HUBSPOT_SEEN.clear()

    out, met = asyncio.run(X.call(m, {"email": "ada@example.com"}, token=KEY))
    assert out == {"firstname": "Ada", "lastname": "Lovelace", "company": "Analytical Engines",
                   "lifecyclestage": "customer", "createdate": "2026-01-02T00:00:00Z"}
    # the body template put our input where HubSpot expects it, rather than posting our own dict
    assert HUBSPOT_SEEN[0]["filterGroups"][0]["filters"][0] == {"propertyName": "email", "operator": "EQ", "value": "ada@example.com"}
    assert HUBSPOT_SEEN[0]["limit"] == 1 and "email" in HUBSPOT_SEEN[0]["properties"]

    # "no contact" is a 200 with an empty list here, not a 404: a missing row, never a failed one
    out, met = asyncio.run(X.call(m, {"email": "nobody@example.com"}, token=KEY))
    assert out is None and met["missing"] == 1.0

    with pytest.raises(X.StepError) as e:
        asyncio.run(X.call(m, {"email": "ada@example.com"}, token="wrong"))
    assert "credential" in str(e.value)


def test_a_step_key_and_a_connection_are_both_offered_for_either(hubspot):
    m = X.load_manifest("hubspot-contact@1.0.0")
    assert m.auth["kind"] == "either" and m.oauth_provider == "hubspot" and m.may_use_key
    sp = _oauth_spec(None)
    sp.steps[0].manifest = "hubspot-contact@1.0.0"
    sp.steps[0].inputs = {"email": "email"}
    rep = ValidationReport(); SV.check_steps(sp, ["email", "queue"], rep)
    msg = [i.message for i in rep.issues if i.level == "error"]
    assert msg and "connect your hubspot account, or paste a step key" in msg[0]
    for cred in ("step:k1", "conn:c1"):          # either is genuinely either
        sp.steps[0].credential = cred
        rep = ValidationReport(); SV.check_steps(sp, ["email", "queue"], rep)
        assert not [i for i in rep.issues if i.level == "error"]
