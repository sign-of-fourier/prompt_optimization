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
