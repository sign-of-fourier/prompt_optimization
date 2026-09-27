import asyncio
import json
import re

import pytest

from bpto import MockClient, Tree, run
from bpto.ops import Variants

from app.clients import make_client
from app.compile import CriticNote, build_program, build_schedule, build_schema, build_task
from app.datasets import columns, flatten, parse_upload, to_dataset
from app.models import EdgeSpec, EvaluateSpec, ModuleSpec, OptimizerSpec, ProjectSpec, SchemaField, ScorerSpec
from app.validation import check_graph, check_mapping, pilot, project_cost, validate_static
from app.models import ValidationReport

ROWS = [{"id": str(i), "text": f"Document {i} says the capital is City{i}.", "answer": f"City{i}", "prev": ""} for i in range(12)]
INPUT_MAP = {"text": "text", "prev": "prev"}


def spec(**over) -> ProjectSpec:
    base = dict(
        name="loop",
        modules=[
            ModuleSpec(id="extract", template="Extract the key fact from: {text}{prev}", description="extracts the key fact",
                       schema_fields=[SchemaField(name="fact")]),
            ModuleSpec(id="shorten", template="Shorten: {fact}", description="shortens", schema_fields=[SchemaField(name="short")]),
            ModuleSpec(id="check", template="Is this a good answer? {short}{prev}", description="decides whether to retry",
                       schema_fields=[SchemaField(name="note")]),
            ModuleSpec(id="final", template="Answer: {short}", description="final answer", schema_fields=[SchemaField(name="answer")]),
        ],
        edges=[EdgeSpec(source="extract", target="shorten", mapping={"fact": "fact"}),
               EdgeSpec(source="shorten", target="check", mapping={"short": "short"}),
               EdgeSpec(source="check", target="extract", name="retry", mapping={"prev": "note"}),
               EdgeSpec(source="check", target="final", name="done", default=True)],
        evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match", field="answer")], objective={"accuracy": 1.0}),
        optimizer=OptimizerSpec(rounds=3, minibatch=3, feedback="critic", reflect_model="mock-reflect", no_improvement_rounds=None),
    )
    base.update(over)
    return ProjectSpec(**base)


def handler(prompt, cfg, schema):
    name = schema.__name__ if schema else ""
    if schema is Variants:
        base = re.search(r"<prompt>\n(.*?)\n</prompt>", prompt, re.S).group(1)
        return Variants(prompts=[f"{base} Be precise."])
    if schema is CriticNote:
        return CriticNote(feedback="the extract step dropped the city name")
    if name == "Out_extract":
        return schema(fact="capital is " + (re.search(r"City\d+", prompt).group(0)))
    if name == "Out_shorten":
        return schema(short=re.search(r"City\d+", prompt).group(0))
    if name == "Out_check":
        return schema(note=" (retry)", next="retry" if "(retry)" not in prompt else "done")
    if name == "Out_final":
        return schema(answer=re.search(r"City\d+", prompt).group(0))
    return "??"


def make_task_and_client(s=None):
    s = s or spec()
    mock = MockClient(handler)
    client = make_client(s.eval_model, mock=mock)
    ds = to_dataset(ROWS, INPUT_MAP, "answer")
    return build_task(s, ds, client), client


def test_schema_and_program():
    s = spec()
    Check = build_schema(s, s.module("check"))
    assert set(Check.model_fields) == {"note", "next"}
    assert Check(note="x", next="retry").next == "retry"
    with pytest.raises(Exception):
        Check(note="x", next="nope")
    assert build_schema(s, ModuleSpec(id="free", template="x")) is None
    p = build_program(s)
    assert p.is_graph and p.graph_errors() == [] and p.terminals == ["final"] and p.max_steps == 8
    assert p.edges["check"][1].default and p.edges["check"][0].mapping == {"prev": "note"}


def test_static_validation_passes_and_catches_mistakes():
    rep = validate_static(spec(), ROWS, INPUT_MAP)
    assert rep.ok, [i.message for i in rep.issues if i.level == "error"]
    assert any("loop" in i.message for i in rep.issues) and rep.summary["label_kind"] == "short_text"
    # loop-back placeholder without a first-visit column
    rep = validate_static(spec(), [{k: v for k, v in r.items() if k != "prev"} for r in ROWS], {"text": "text"})
    assert any("loop-back" in i.message and i.level == "error" for i in rep.issues)
    # edge maps a field the source does not output
    s = spec()
    s.edges[0].mapping = {"fact": "nope"}
    rep = validate_static(s, ROWS, INPUT_MAP)
    assert any("does not output" in i.message for i in rep.issues)
    # orchestrator without default / with duplicate names
    s = spec()
    s.edges[3].default = False
    rep = ValidationReport(); check_graph(s, rep)
    assert any("default edge" in i.message for i in rep.issues)
    # scorer field the terminal lacks; objective on an unknown metric
    s = spec(evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match", field="answr")], objective={"f1": 1.0}))
    rep = validate_static(s, ROWS, INPUT_MAP)
    msgs = [i.message for i in rep.issues if i.level == "error"]
    assert any("no such output field" in m for m in msgs) and any("objective weighs metric 'f1'" in m for m in msgs)
    # missing label column with a scorer that needs one
    s = spec(evaluate=EvaluateSpec(label_column=None, scorers=[ScorerSpec(type="exact_match", field="answer")]))
    assert not validate_static(s, ROWS, INPUT_MAP).ok
    s = spec(evaluate=EvaluateSpec(label_column=None, scorers=[ScorerSpec(type="llm_judge_free", rubric="ok?", name="accuracy")]))
    assert validate_static(s, ROWS, INPUT_MAP).ok
    # tiny dataset warns
    assert any("noisy" in i.message for i in validate_static(spec(), ROWS[:5], INPUT_MAP).issues)


def test_label_kind_and_histogram():
    rows = [{"text": f"t{i}", "prev": "", "answer": ["Yes", "yes", "No", "no "][i % 4]} for i in range(20)]
    rep = validate_static(spec(), rows, INPUT_MAP)
    assert rep.summary["label_kind"] in ("class", "boolean") and rep.summary["label_histogram"]
    assert any("differ only in case" in i.message for i in rep.issues)
    rows = [{"text": f"t{i}", "prev": "", "answer": "A" if i < 18 else "B"} for i in range(20)]
    assert any("imbalance" in i.message for i in validate_static(spec(), rows, INPUT_MAP).issues)


def test_pilot_reports_loop_and_band():
    task, client = make_task_and_client()
    rep = ValidationReport()
    out = asyncio.run(pilot(spec(), task, rows=6, rep=rep, rewrite=True))
    assert out["baseline"] == 1.0 and out["nondeterminism_band"] == 0.0 and out["avg_steps"] == 7.0  # one retry per example
    assert out["examples"][0]["path"] == ["extract", "shorten", "check", "final"]
    assert "rewrite" in out and any("no headroom" in i.message for i in rep.issues)
    assert client.usage.calls > 0 and out["tokens_per_module"]["extract"] > 0


def test_end_to_end_run_with_critic_and_round_robin():
    s = spec()
    task, client = make_task_and_client(s)
    schedule, stop = build_schedule(s, task)
    tree = Tree(task)
    res = asyncio.run(run(tree, schedule, stop=stop))
    reflected = [n for n in tree if n.origin.op == "reflect"]
    assert len(reflected) == 3 and res.rounds == 4
    assert [n.origin.params["module"] for n in reflected] == ["extract", "shorten", "check"]
    # every reflected child is a Program with the same edges; the critic note reached the reflection prompt
    assert all(n.prompt.edges == tree.root.prompt.edges for n in reflected)
    metas = [p for p in client._mock.calls if "Feedback:" in p]
    assert metas and all("dropped the city name" in m for m in metas)
    # the critic reads the template of the step it reviews, in round-robin order
    critics = [p for p in client._mock.calls if p.startswith("You are reviewing one run")]
    templates = {m.id: m.template for m in s.modules}
    assert critics and all("<prompt>" in p for p in critics)
    for mod in ("extract", "shorten", "check"):
        assert any(f"step named '{mod}'" in p and templates[mod] in p for p in critics)
    # gate: minibatch evaluation happened; accepted children got the full set
    assert all(n.evaluation is not None for n in reflected)
    assert tree.root.evaluation.metrics["steps"] == 7.0


def test_bo_engine_schedule_runs_offline():
    from bpto.bo import HashEmbedder
    s = spec(optimizer=OptimizerSpec(engine="bo", rounds=3, minibatch=3, feedback="plain", reflect_model="mock", no_improvement_rounds=None))
    task, client = make_task_and_client(s)
    schedule, stop = build_schedule(s, task, embedder=HashEmbedder(dim=32))
    tree = Tree(task)
    asyncio.run(run(tree, schedule, stop=stop))
    assert sum(1 for n in tree if n.origin.op == "reflect") == 3


def test_bo_round_proposes_q_even_when_the_pool_is_smaller():
    """q = 4 on a one-node pool must still propose four rewrites (four independent calls on the root), else a parallel
    run is just a shorter run; with two candidates each parent gets two calls."""
    from bpto import Stop
    from bpto.bo import HashEmbedder
    s = spec(optimizer=OptimizerSpec(engine="bo", rounds=3, minibatch=3, feedback="plain", reflect_model="mock", no_improvement_rounds=None,
                                     bo={"q": 4}))
    task, _ = make_task_and_client(s)
    schedule, _ = build_schedule(s, task, embedder=HashEmbedder(dim=32))
    tree = Tree(task)
    asyncio.run(run(tree, schedule, stop=Stop(rounds=1)))  # round 0: the root's full evaluation, nothing else
    reflect = lambda steps: next(st for st in steps if st.name.endswith("/reflect")).op
    assert reflect(schedule(tree, 1)).calls == 4
    child = tree.add_child(tree.root, tree.root.prompt, tree.root.origin)
    child.evaluation = tree.root.evaluation  # a second fully evaluated candidate
    assert reflect(schedule(tree, 2)).calls == 2


def test_cost_projection_uses_pilot_numbers():
    s = spec()
    c = project_cost(s, n_rows=200, avg_steps=4.0, tokens_per_module={"extract": 400, "shorten": 100, "check": 80, "final": 60})
    assert c["calls"]["round0"] == 160 * 4 and c["calls"]["critic"] == 3 * 3
    assert c["calls_worst_case"]["gate"] == c["calls"]["gate"] * 2  # max_steps 8 / avg 4
    assert c["usd"]["total"] > 0 and "mock-reflect" in c["assumptions"]["unpriced_models"]


def test_dataset_parsing_and_mapping():
    rows = parse_upload("d.csv", b"text,answer\nhello,world\nfoo,bar\n")
    assert rows == [{"text": "hello", "answer": "world"}, {"text": "foo", "answer": "bar"}]
    rows = flatten(parse_upload("d.jsonl", json.dumps({"inputs": {"q": "x"}, "answer": "y", "id": "k"}).encode()))
    assert rows == [{"q": "x", "answer": "y", "id": "k"}] and columns(rows) == ["q", "answer", "id"]
    ds = to_dataset(rows, {"question": "q"}, "answer")
    assert ds[0].id == "k" and ds[0].inputs == {"question": "x"} and ds[0].answer == "y"


def test_minibatch_floor():
    with pytest.raises(ValueError):
        OptimizerSpec(minibatch=2)


def test_compress_goal_swaps_reflection_and_records_template_tokens():
    s = spec(optimizer=OptimizerSpec(goal="compress", rounds=1, minibatch=3, feedback="critic", reflect_model="mock-reflect",
                                     no_improvement_rounds=None),
             evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match", field="answer")],
                                   objective={"accuracy": 1.0, "template_tokens": -0.002}))
    task, client = make_task_and_client(s)
    schedule, stop = build_schedule(s, task)
    tree = Tree(task)
    asyncio.run(run(tree, schedule, stop=stop))
    m = tree.root.evaluation.metrics
    # every step's template counted (mock tokenizer: chars/4), so more than the entry template alone
    assert m["template_tokens"] > len("Extract the key fact from: ") / 4 and m["template_tokens"] == round(m["template_tokens"])
    metas = [p for p in client._mock.calls if "<prompt>" in p and "Feedback:" in p]
    assert metas and all("SHORTER prompt template" in p and "template tokens:" in p for p in metas)
    assert all("add concrete rules" not in p for p in metas)
    critics = [p for p in client._mock.calls if "SHORTER without losing accuracy" in p]
    assert critics and all("Extract the key fact from: {text}{prev}" in p for p in critics)
    # correct rows count as successes even though the token penalty keeps every row's objective below 1
    from app.compile import correct_rows_pass
    passed = correct_rows_pass(s.evaluate.objective)
    ok = [r for r in tree.root.evaluation.per_example if r.metrics["accuracy"] >= 1.0]
    assert ok and all(passed(None, r) for r in ok)


def test_compress_goal_validation_warns_without_token_penalty():
    s = spec(optimizer=OptimizerSpec(goal="compress", rounds=1, minibatch=3, reflect_model="mock-reflect"))
    rep = validate_static(s, ROWS, INPUT_MAP)
    assert any(i.stage == "optimizer" and "template_tokens" in i.message for i in rep.issues)
    s = spec(optimizer=OptimizerSpec(goal="compress", rounds=1, minibatch=3, reflect_model="mock-reflect"),
             evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match", field="answer")], objective={"accuracy": 1.0, "template_tokens": -0.002}))
    rep = validate_static(s, ROWS, INPUT_MAP)
    assert rep.ok and not any(i.stage == "optimizer" for i in rep.issues)


def test_version_pin_and_fingerprint():
    """A version pins what runs: templates and concrete model ids. The fingerprint moves with those and with nothing
    else - editing the optimizer or the canvas layout gives the same fingerprint, editing a prompt does not."""
    from app import versions as V
    s = spec(layout={"extract": {"x": 1}})
    a = V.pin(s)
    assert a.layout == {} and all(m.model == s.eval_model for m in a.modules)
    assert a.optimizer.critic_model == a.optimizer.reflect_model
    assert s.modules[0].model is None and s.layout          # pinning is pure: the source spec is untouched

    s.optimizer.rounds += 7
    s.layout = {}
    assert V.fingerprint(V.pin(s)) == V.fingerprint(a)      # how it was produced is not what it does

    b = V.pin(s, {"extract": "Pull the fact out of {text}{prev}"})
    assert b.modules[0].template.startswith("Pull the fact") and V.fingerprint(b) != V.fingerprint(a)


def test_balanced_accuracy_weights_rows_by_class_share():
    from types import SimpleNamespace as NS
    from app import scorers as S
    from app.compile import correct_rows_pass
    labels = ["yes", "yes", "yes", "no"]
    sc = S.build(ScorerSpec(type="exact_match", field="label", balanced=True), labels=labels)
    score = lambda pred, gold: sc(None, NS(answer=gold), NS(parsed={"label": pred}, text=pred), None)
    perfect = [score(y, y) for y in labels]
    always_yes = [score("yes", y) for y in labels]
    mean = lambda rows, k: sum(r[k] for r in rows) / len(rows)
    # perfect predictions: balanced accuracy 1.0; always the majority class: accuracy 0.75 but balanced 0.5
    assert mean(perfect, "accuracy_balanced") == pytest.approx(1.0)
    assert mean(always_yes, "accuracy") == pytest.approx(0.75) and mean(always_yes, "accuracy_balanced") == pytest.approx(0.5)
    # a label the rows never showed weighs as if it appeared once; balanced off emits accuracy alone
    assert score("maybe", "maybe")["accuracy_balanced"] == pytest.approx(4 / 2)
    assert set(S.build(ScorerSpec(type="exact_match", field="label"))(None, NS(answer="a"), NS(parsed={"label": "a"}, text="a"), None)) == {"accuracy"}
    # the reflector judges a row on the unweighted twin: a correct majority-class row (weight < 1) still passes
    passed = correct_rows_pass({"accuracy_balanced": 1.0})
    assert passed(None, NS(error=None, metrics=perfect[0])) and not passed(None, NS(error=None, metrics=always_yes[3]))


def test_balanced_scorer_through_build_task():
    s = spec(evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match", field="answer", balanced=True)],
                                   objective={"accuracy_balanced": 1.0}))
    task, client = make_task_and_client(s)
    tree = Tree(task)
    schedule, stop = build_schedule(s, task)
    asyncio.run(run(tree, schedule, stop=stop))
    m = tree.root.evaluation.metrics
    # twelve distinct labels: every weight is 1, so balanced equals plain accuracy
    assert m["accuracy_balanced"] == pytest.approx(m["accuracy"]) and m["accuracy"] == 1.0
    # the validator knows the balanced metric exists, and only when the scorer asks for it
    assert not [i for i in validate_static(s, ROWS, INPUT_MAP).issues if "no scorer produces" in i.message]
    off = spec(evaluate=EvaluateSpec(scorers=[ScorerSpec(type="exact_match", field="answer")], objective={"accuracy_balanced": 1.0}))
    assert [i for i in validate_static(off, ROWS, INPUT_MAP).issues if "no scorer produces" in i.message]


def test_routing_client_retries_bedrock_missing_inference_profile(monkeypatch):
    """Bedrock's intermittent 'Inference Profile ARN not found' is retried; any other error is not."""
    from bpto.llm.base import Completion
    from app.clients import RoutingClient
    monkeypatch.setattr(asyncio, "sleep", lambda s: _noop())  # no real backoff in tests

    class Flaky:
        def __init__(self, err, fails):
            self.err, self.fails, self.calls = err, fails, 0

        async def _complete(self, prompt, cfg, schema):
            self.calls += 1
            if self.calls <= self.fails:
                raise RuntimeError(self.err)
            return Completion(text="ok")

    flaky = Flaky("ResourceNotFoundException: Inference Profile ARN not found", fails=2)
    c = RoutingClient("m", mock=flaky)
    assert asyncio.run(c.complete("hi")).text == "ok" and flaky.calls == 3
    other = Flaky("ValidationException: bad input", fails=1)
    with pytest.raises(RuntimeError):
        asyncio.run(RoutingClient("m", mock=other).complete("hi"))
    assert other.calls == 1


async def _noop():
    return None


def test_library_loop_example_validates_and_loops():
    """The loop walkthrough: extract -> check -> (retry -> extract | done -> final), fed by an empty first-visit column."""
    from app.bundles import load_example
    from app.mock import mock_client
    b = load_example("amount-due-loop")
    rows = b.rows()
    rep = validate_static(b.spec, rows, b.dataset.input_map)
    assert rep.ok, [i.message for i in rep.issues if i.level == "error"]
    assert any("has a loop" in i.message for i in rep.issues)
    assert build_program(b.spec).terminals == ["final"]
    task = build_task(b.spec, to_dataset(rows, b.dataset.input_map, b.dataset.label_column),
                      make_client(b.spec.eval_model, mock=mock_client()))
    out = asyncio.run(pilot(b.spec, task, rows=12, rep=ValidationReport()))
    paths = [e["path"] for e in out["examples"]]
    assert ["extract", "check", "final"] in paths and out["avg_steps"] > 3   # some rows took the retry edge


def test_set_f1_scores_lists_order_and_case_blind():
    """The Nantucket example: any order, any case, list or text; partial credit; beta trades misses against extras."""
    from types import SimpleNamespace as NS
    from app import scorers as S
    from app.compile import list_feedback
    gold = ["Charles", "Nantucket"]
    sc = S.build(ScorerSpec(type="set_f1", field="names"))
    score = lambda pred, g=gold, s=sc: s(None, NS(answer=g), NS(parsed={"names": pred}, text=str(pred)), None)["set_f1"]
    for pred in (["Nantucket", "Charles"], ["charles", "nantucket"], "Charles, Nantucket", '["Nantucket", "Charles"]'):
        assert score(pred) == 1.0, pred
    assert score(["Charles"]) == pytest.approx(2 / 3) and score([]) == 0.0
    assert score(["Charles", "Nantucket", "Massachusetts"]) == pytest.approx(0.8)
    assert score(["Charles", "Charles", "Nantucket"]) == 1.0                  # a repeated item is still one item
    assert score([], g=[]) == 1.0                                              # nothing to find, nothing claimed
    assert score(["Charles"], g="Charles; Nantucket") == pytest.approx(2 / 3)  # a text label splits on ; too
    # normalize off: case counts
    assert score(["charles", "nantucket"], s=S.build(ScorerSpec(type="set_f1", field="names", normalize=False))) == 0.0
    # beta 2 punishes the miss more than the extra; beta 0.5 the reverse
    recall = S.build(ScorerSpec(type="set_f1", field="names", beta=2.0))
    miss, extra = score(["Charles"], s=recall), score(["Charles", "Nantucket", "Boston", "Salem"], s=recall)
    assert miss < extra
    precision = S.build(ScorerSpec(type="set_f1", field="names", beta=0.5))
    assert score(["Charles"], s=precision) > score(["Charles", "Nantucket", "Boston", "Salem"], s=precision)
    # the rewriter is told exactly what was dropped and what was invented
    fb = list_feedback(lambda ex, r: "expected: ...", [ScorerSpec(type="set_f1", field="names")])
    text = fb(NS(answer=gold), NS(error=None, parsed={"names": ["charles", "Boston"]}, output=""))
    assert "missing: nantucket" in text and "extra (not in the label): boston" in text


def test_numeric_tolerance_modes():
    from types import SimpleNamespace as NS
    from app import scorers as S
    run1 = lambda sc, got, gold: S.build(sc)(None, NS(answer=gold), NS(parsed={"v": got}, text=str(got)), None)["numeric_match"]
    rel = ScorerSpec(type="numeric", field="v", tolerance=0.01)
    ab = ScorerSpec(type="numeric", field="v", tolerance=0.5, tolerance_mode="absolute")
    assert run1(rel, 1009, 1000) == 1.0 and run1(rel, 1011, 1000) == 0.0       # 1% of 1000
    assert run1(ab, 1000.4, 1000) == 1.0 and run1(ab, 1000.6, 1000) == 0.0     # half a unit, whatever the size
    assert run1(ab, 0.4, 0) == 1.0


def test_set_f1_validation():
    s = spec(modules=[m if m.id != "final" else ModuleSpec(id="final", template="Answer: {short}", description="final answer",
                                                              schema_fields=[SchemaField(name="answer")]) for m in spec().modules],
             evaluate=EvaluateSpec(scorers=[ScorerSpec(type="set_f1", field="answer")], objective={"set_f1": 1.0}))
    msgs = [i.message for i in validate_static(s, ROWS, INPUT_MAP).issues]
    assert any("make it a list field" in m for m in msgs) and not any("no scorer produces" in m for m in msgs)
    s.evaluate.scorers[0].beta = 0
    assert not validate_static(s, ROWS, INPUT_MAP).ok
