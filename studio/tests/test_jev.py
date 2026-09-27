"""The Jev judge beta (app/jev.py): gated per server and user, shadow-only, and unable to fail a row. Offline: the
Jev client talks to an httpx.MockTransport."""
import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest

from bpto import Budget, JevClient, MockClient, Tree, evaluate

from app import jev as J
from app.clients import make_client
from app.compile import build_task
from app.datasets import to_dataset
from app.models import EvaluateSpec, ModuleSpec, ProjectSpec, SchemaField, ScorerSpec, ValidationReport


@pytest.fixture
def jev_on(monkeypatch):
    monkeypatch.setenv("STUDIO_JEV", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("STUDIO_JEV_USERS", "beta@example.com")


def spec(objective=None, **sc) -> ProjectSpec:
    return ProjectSpec(
        name="j", modules=[ModuleSpec(id="a", template="Q: {q}", schema_fields=[SchemaField(name="answer")])],
        evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match", field="answer"), ScorerSpec(type="jev_match", field="answer", **sc)],
                              objective=objective or {"accuracy": 1.0}))


def run1(scorer, pred, gold):
    return asyncio.run(scorer(None, NS(answer=gold, inputs={"q": "how long?"}), NS(parsed={"answer": pred}, text=pred), None))


def test_gates(monkeypatch, jev_on):
    beta, other = {"email": "Beta@Example.com"}, {"email": "someone@example.com"}
    assert J.enabled_for(beta) and not J.enabled_for(other) and not J.enabled_for(None)
    monkeypatch.setenv("STUDIO_JEV_USERS", "*")
    assert J.enabled_for(other)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert not J.enabled_for(beta) and J.enabled_for(beta, mock=True)   # no key: live off, the mock still works
    monkeypatch.setenv("STUDIO_JEV", "0")
    assert not J.enabled_for(beta, mock=True)                            # the kill switch beats everything
    assert J.make_client() is None


def test_validation_is_per_user_and_shadow_only(jev_on):
    rep = ValidationReport(); J.check(spec(), {"email": "someone@example.com"}, rep)
    assert not rep.ok and any("not enabled for your account" in i.message for i in rep.issues)
    rep = ValidationReport(); J.check(spec(), {"email": "beta@example.com"}, rep)
    assert rep.ok and any("never weighted" in i.message for i in rep.issues)
    rep = ValidationReport(); J.check(spec({"accuracy": 1.0, "jev_match": 0.5}), {"email": "beta@example.com"}, rep)
    assert not rep.ok and any("shadow-only" in i.message for i in rep.issues)
    rep = ValidationReport(); J.check(spec().model_copy(update={"evaluate": EvaluateSpec()}), None, rep)
    assert rep.ok and not rep.issues                                      # projects without Jev never see the beta


def test_scorer_on_the_mock(jev_on):
    sc = J.scorer(ScorerSpec(type="jev_match", field="answer"), J.make_client(mock=True))
    assert run1(sc, "45 days", "45 days") == {"jev_match": 0.95, "jev_match_failed": 0.0}
    assert run1(sc, "30 days", "45 days") == {"jev_match": 0.05, "jev_match_failed": 0.0}


def test_nothing_jev_does_can_fail_a_row(jev_on):
    s = ScorerSpec(type="jev_match", field="answer")
    failed = {"jev_match": 0.0, "jev_match_failed": 1.0}
    assert run1(J.scorer(s, None), "x", "x") == failed                    # server has no Jev
    down = JevClient("jev-1.13", base_url="http://jev.mock", max_retries=0,
                     transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    assert run1(J.scorer(s, down), "x", "x") == failed                    # outage
    junk = JevClient("jev-1.13", base_url="http://jev.mock", max_retries=0,
                     transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"answers": {}})))
    assert run1(J.scorer(s, junk), "x", "x") == failed                    # malformed answer
    capped = J.make_client(mock=True, max_calls=1)
    sc = J.scorer(s, capped)
    assert run1(sc, "a", "a")["jev_match_failed"] == 0.0 and run1(sc, "b", "b") == failed   # its own call cap


def test_through_build_task_other_metrics_survive(jev_on):
    rows = [{"q": f"q{i}", "answer": "yes" if i % 2 else "no"} for i in range(6)]
    mock = MockClient(lambda prompt, cfg, schema: schema(answer="yes"))
    for jc in (J.make_client(mock=True), None):
        task = build_task(spec(), to_dataset(rows, {"q": "q"}, "answer"), make_client("m", mock=mock), jev_client=jc)
        tree = Tree(task)
        ev = asyncio.run(evaluate(dataset=task.dataset).score(tree, tree.root))
        assert ev.metrics["accuracy"] == pytest.approx(0.5)               # the project's own scorer is untouched
        if jc is not None:
            assert ev.metrics["jev_match"] == pytest.approx((3 * 0.95 + 3 * 0.05) / 6) and ev.metrics["jev_match_failed"] == 0.0
        else:
            assert ev.metrics["jev_match_failed"] == 1.0 and ev.score == pytest.approx(0.5)
