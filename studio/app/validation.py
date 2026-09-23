"""Stages: graph (static) -> mapping (placeholders vs columns) -> labels -> scorer compatibility -> pilot (costs
calls; opt-in) -> cost projection. Every finding is an `Issue`; any `error` disables Run.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any

from bpto import Dataset, Example, ModelConfig, Tree, evaluate, select
from bpto.gepa.select import candidates
from bpto.ops import random as random_rewrite
from bpto.prompt import Prompt
from bpto.tree import Evaluation

from . import scorers as S
from .clients import prices
from .models import ProjectSpec, ValidationReport

MIN_ROWS_WARN = 30
MAX_INPUT_CHARS_REFLECT = 4000  # ReflectiveExpander.max_input_chars default


# ---- static: graph -------------------------------------------------------------------

def check_graph(spec: ProjectSpec, rep: ValidationReport) -> None:
    ids = [m.id for m in spec.modules]
    if len(set(ids)) != len(ids):
        rep.add("error", "graph", "duplicate module ids")
    if not spec.modules:
        rep.add("error", "graph", "the canvas has no modules"); return
    if spec.entry_id not in ids:
        rep.add("error", "graph", f"entry module {spec.entry_id!r} does not exist")
    for m in spec.modules:
        try:
            Prompt(template=m.template).placeholders
        except ValueError as e:
            rep.add("error", "graph", f"prompt template: {e}", where=m.id)
        if not m.template.strip():
            rep.add("error", "graph", "empty prompt", where=m.id)
        outs = spec.outgoing(m.id)
        if len(outs) > 1:
            names = [e.name for e in outs]
            if "" in names or len(set(names)) != len(names):
                rep.add("error", "graph", "an orchestrating module needs a distinct, non-empty name on every outgoing edge", where=m.id, names=names)
            if sum(e.default for e in outs) != 1:
                rep.add("error", "graph", "an orchestrating module needs exactly one default edge (taken when the model's `next` cannot be parsed)", where=m.id)
            declared = {f.name for f in m.schema_fields}
            if "next" in declared:
                rep.add("info", "graph", "`next` is added to this module's schema automatically from its edges; the declared field is replaced", where=m.id)
        for e in outs:
            if e.target not in ids:
                rep.add("error", "graph", f"edge to unknown module {e.target!r}", where=m.id)
    # reachability / terminals / cycles
    seen, todo = set(), [spec.entry_id]
    while todo:
        x = todo.pop()
        if x in seen or x not in ids:
            continue
        seen.add(x); todo.extend(e.target for e in spec.outgoing(x))
    unreachable = [i for i in ids if i not in seen]
    if unreachable:
        rep.add("warn", "graph", f"modules never reached from the entry: {unreachable} (they will be rewritten but never run)")
    if seen and not any(not spec.outgoing(x) for x in seen):
        rep.add("error", "graph", "no terminal is reachable: every path loops forever; the step cap would be the only stop")
    if _has_cycle(spec):
        rep.add("info", "graph", f"the graph has a loop; each example runs at most {spec.max_steps} steps (max_steps), which multiplies cost by up to that factor")


def _has_cycle(spec: ProjectSpec) -> bool:
    color: dict[str, int] = {}
    def visit(x):
        color[x] = 1
        for e in spec.outgoing(x):
            c = color.get(e.target, 0)
            if c == 1 or (c == 0 and visit(e.target)):
                return True
        color[x] = 2
        return False
    return any(color.get(m.id, 0) == 0 and visit(m.id) for m in spec.modules)


# ---- static: placeholders vs columns and mappings -------------------------------------

def check_mapping(spec: ProjectSpec, dataset_columns: list[str], input_map: dict[str, str], rep: ValidationReport) -> None:
    """Every placeholder of every module must be satisfiable on its first visit: from a mapped dataset column or from
    a field mapped on an incoming edge that is reached before it. Loop-back edges do not count for the first visit."""
    mapped_inputs = set(input_map)
    if spec.steps:
        # an external step's outputs arrive as frozen columns; whether this dataset actually carries them is
        # `step_validation.check_frozen`'s job, and saying "no source" here as well would just be noise
        from .steps import provided
        mapped_inputs |= set(provided(spec))
    for ph, col in input_map.items():
        if col not in dataset_columns:
            rep.add("error", "mapping", f"placeholder {{{ph}}} is mapped to column {col!r}, which the dataset does not have", where=ph)
    out_fields = {m.id: {f.name for f in m.schema_fields} | {"$text"} for m in spec.modules}
    # fields available at each module on a forward walk (BFS from entry, edges in order; back-edges ignored)
    avail: dict[str, set[str]] = {m.id: set(mapped_inputs) for m in spec.modules}
    order, seen, todo = [], set(), [spec.entry_id]
    while todo:
        x = todo.pop(0)
        if x in seen:
            continue
        seen.add(x); order.append(x)
        for e in spec.outgoing(x):
            for ph, field in e.mapping.items():
                if field not in out_fields.get(x, set()):
                    rep.add("error", "mapping", f"edge {x} -> {e.target} maps {{{ph}}} from field {field!r}, which {x!r} does not output "
                            f"(its fields: {sorted(out_fields.get(x, set()))})", where=x)
            if e.target not in seen:  # state accumulates along the path: everything available here plus the mapped fields
                avail[e.target] |= avail[x] | set(e.mapping)
                todo.append(e.target)
    for m in spec.modules:
        try:
            phs = Prompt(template=m.template).placeholders
        except ValueError:
            continue
        for ph in phs:
            if ph not in avail[m.id]:
                hint = " (fed only by a loop-back edge: give it a first-visit value as a dataset column)" if any(
                    ph in e.mapping for e in spec.edges if e.target == m.id) else ""
                rep.add("error", "mapping", f"{{{ph}}} has no source when {m.id!r} first runs{hint}", where=m.id)
    unused = [c for c in dataset_columns if c not in input_map.values() and c != spec.evaluate.label_column and c != "id"]
    if unused:
        rep.add("info", "mapping", f"columns not used by any prompt: {unused} (kept as metadata)")


# ---- static: labels and scorer --------------------------------------------------------

def _label_kind(values: list[Any]) -> str:
    nn = [v for v in values if v not in (None, "")]
    if not nn:
        return "empty"
    if all(isinstance(v, bool) or str(v).strip().lower() in ("true", "false", "yes", "no") for v in nn):
        return "boolean"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) or re.fullmatch(r"-?\d+(\.\d+)?", str(v).strip()) for v in nn):
        return "number"
    if all(isinstance(v, (dict, list)) for v in nn):
        return "json"
    distinct = len({S.normalize(v) for v in nn})
    if distinct <= max(2, int(math.sqrt(len(nn)))) and all(len(str(v)) <= 40 for v in nn):
        return "class"
    return "text" if any(len(str(v).split()) > 8 for v in nn) else "short_text"


def check_labels(spec: ProjectSpec, rows: list[dict[str, Any]], input_map: dict[str, str], rep: ValidationReport) -> dict[str, Any]:
    n = len(rows)
    info: dict[str, Any] = {"rows": n}
    if n == 0:
        rep.add("error", "labels", "the dataset is empty"); return info
    if n < MIN_ROWS_WARN:
        rep.add("warn", "labels", f"only {n} rows: single-evaluation scores will be noisy (±{(0.5 / math.sqrt(n)):.0%} at best); 30+ is the practical floor", rows=n)
    # inputs
    seeded = {ph for e in spec.edges for ph in e.mapping}  # placeholders an edge also writes: an empty column is a first-visit seed
    for ph, col in input_map.items():
        empties = sum(1 for r in rows if r.get(col) in (None, ""))
        if empties and ph in seeded:
            rep.add("info", "labels", f"{empties}/{n} rows have an empty {col!r}; it is the first-visit value of {{{ph}}}, later written by an edge", where=col)
        elif empties:
            rep.add("warn", "labels", f"{empties}/{n} rows have an empty {col!r}", where=col)
    keyed = Counter(json.dumps({ph: r.get(col) for ph, col in input_map.items()}, sort_keys=True, default=str) for r in rows)
    dups = sum(c - 1 for c in keyed.values() if c > 1)
    if dups:
        rep.add("warn", "labels", f"{dups} duplicate input rows (identical prompts are cache hits: they add no information)")
    label = spec.evaluate.label_column
    needs = any(S.needs_label(s) for s in spec.evaluate.scorers)
    if not label:
        if needs:
            rep.add("error", "labels", "the chosen scorer needs a label column, none is selected")
        return info
    if label not in {k for r in rows for k in r}:
        rep.add("error", "labels", f"label column {label!r} does not exist"); return info
    values = [r.get(label) for r in rows]
    empties = sum(1 for v in values if v in (None, ""))
    if empties:
        rep.add("warn" if empties < n else "error", "labels", f"{empties}/{n} rows have an empty label", where=label)
    kind = _label_kind(values)
    info["label_kind"] = kind
    if kind in ("class", "boolean"):
        hist = Counter(str(v).strip() for v in values if v not in (None, ""))
        info["label_histogram"] = dict(hist.most_common())
        norm_groups: dict[str, set[str]] = {}
        for v in hist:
            norm_groups.setdefault(S.normalize(v), set()).add(v)
        near = [sorted(g) for g in norm_groups.values() if len(g) > 1]
        if near:
            rep.add("warn", "labels", f"labels that differ only in case/punctuation/whitespace: {near} - the scorer normalizes them, but check they are meant to be the same class")
        top = hist.most_common(1)[0][1] / max(1, sum(hist.values()))
        if top > 0.8:
            rep.add("warn", "labels", f"class imbalance: {top:.0%} of rows share one label; a prompt that always answers it scores {top:.0%}")
        singles = [k for k, c in hist.items() if c == 1]
        if singles:
            rep.add("info", "labels", f"{len(singles)} label(s) occur once: {singles[:8]}")
    # leakage: label verbatim in an input
    leaks = 0
    for r in rows:
        lv = S.normalize(r.get(label))
        if lv and len(lv) >= 3 and any(lv in S.normalize(r.get(col)) for col in input_map.values()):
            leaks += 1
    if leaks:
        lvl = "info" if kind in ("short_text", "text") else "warn"
        rep.add(lvl, "labels", f"{leaks}/{n} labels appear verbatim in an input" + (" (normal for extraction / span tasks)" if lvl == "info" else " (a prompt could copy them out without doing the task)"))
    # scorer vs label kind
    for sc in spec.evaluate.scorers:
        if sc.type == "exact_match" and kind == "text":
            rep.add("warn", "scorer", "exact match on free-text labels almost never matches; use token_f1, contains or an LLM judge")
        if sc.type == "numeric" and kind not in ("number",):
            rep.add("error", "scorer", f"numeric scorer but labels look like {kind}")
        if sc.type in ("llm_judge",) and not sc.rubric:
            rep.add("info", "scorer", "no rubric given; the judge will use a generic correctness rubric")
        if sc.field:
            term = spec.module(_terminal(spec))
            if sc.field not in {f.name for f in term.schema_fields}:
                rep.add("error", "scorer", f"scorer compares field {sc.field!r} but the terminal module {term.id!r} has no such output field", where=term.id)
        if sc.type in ("exact_match", "contains", "json_field", "token_f1") and not sc.field and spec.module(_terminal(spec)).schema_fields:
            rep.add("warn", "scorer", f"the terminal module returns structured fields but the scorer compares the whole output; pick a field (e.g. {spec.module(_terminal(spec)).schema_fields[0].name!r})")
    for k in spec.evaluate.objective:
        known = {S.DEFAULT_NAMES[s.type] if not s.name else s.name for s in spec.evaluate.scorers} | {"prompt_tokens", "output_tokens", "template_tokens", "steps", "capped"}
        if k not in known and not k.startswith(("tokens_per_module.", "parse_fail.")):
            rep.add("error", "scorer", f"objective weighs metric {k!r} which no scorer produces (available: {sorted(known)})")
    if spec.optimizer.goal == "compress":
        if not spec.evaluate.token_count:
            rep.add("error", "optimizer", "goal is compression but token counting is off (Evaluate block)")
        elif not any(k == "template_tokens" and w < 0 for k, w in spec.evaluate.objective.items()):
            rep.add("warn", "optimizer", "goal is compression but the objective does not penalize template_tokens; a shorter prompt "
                                         "cannot win the check batch. Set e.g. template_tokens = -0.002 on the Evaluate block")
    return info


def _terminal(spec: ProjectSpec) -> str:
    seen, todo = set(), [spec.entry_id]
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        if not spec.outgoing(x):
            return x
        todo.extend(e.target for e in spec.outgoing(x))
    return spec.entry_id


# ---- pilot (spends calls) ------------------------------------------------------------

async def pilot(spec: ProjectSpec, task, rows: int = 12, seed: int = 0, rewrite: bool = True, rep: ValidationReport | None = None) -> dict[str, Any]:
    """Root twice (second with the cache bypassed) + one random rewrite, on a seeded sample. Returns diagnostics and
    adds issues: parse failures, loop behaviour, no headroom / flat under rewrite, trace length, token averages."""
    rep = rep or ValidationReport()
    sample = task.dataset.sample(rows, seed)
    tree = Tree(task)
    ev1: Evaluation = await evaluate(dataset=sample).score(tree, tree.root)
    cache = task.client.cache
    task.client.cache = type(cache)()  # fresh empty cache: the second pass must hit the model
    try:
        ev2: Evaluation = await evaluate(dataset=sample).score(tree, tree.root)
    finally:
        task.client.cache = cache
    key = next(iter(spec.evaluate.objective), None)
    def scalar(ev):
        return ev.score
    band = abs(scalar(ev1) - scalar(ev2))
    flips = sum(1 for a, b in zip(ev1.per_example, ev2.per_example) if a.metrics != b.metrics)
    out: dict[str, Any] = {"rows": len(sample), "baseline": scalar(ev1), "baseline_rerun": scalar(ev2), "nondeterminism_band": band,
                           "rows_changed_on_rerun": flips, "metrics": ev1.metrics, "metrics_std": ev1.metrics_std,
                           "sampling_se": {k: v / math.sqrt(max(1, ev1.n)) for k, v in ev1.metrics_std.items()}}
    errors = [r for r in ev1.per_example if r.error]
    if errors:
        lvl = "error" if len(errors) == len(ev1.per_example) else "warn"
        rep.add(lvl, "pilot", f"{len(errors)}/{len(ev1.per_example)} examples failed: {errors[0].error[:160]}")
    for k, v in ev1.metrics.items():
        if k.startswith("parse_fail.") and v > 0:
            rep.add("warn", "pilot", f"{k[len('parse_fail.'):]!r}: the model's `next` could not be parsed on {v:.0%} of runs (default edge taken); Nova returns structured output by instruction - make the schema instruction explicit", where=k.split(".", 1)[1])
    if "steps" in ev1.metrics:
        out["avg_steps"] = ev1.metrics["steps"]
        if ev1.metrics.get("capped", 0) > 0:
            rep.add("warn", "pilot", f"{ev1.metrics['capped']:.0%} of examples hit max_steps={spec.max_steps}: the orchestrator loops until the cap")
        if _has_cycle(spec) and ev1.metrics["steps"] <= len(_forward_path(spec)):
            rep.add("info", "pilot", "the loop-back edge was never taken in the pilot")
    per_module = {k[len("tokens_per_module."):]: v for k, v in ev1.metrics.items() if k.startswith("tokens_per_module.")}
    out["tokens_per_module"] = per_module
    out["avg_input_tokens"] = ev1.metrics.get("prompt_tokens")
    out["avg_output_tokens"] = ev1.metrics.get("output_tokens")
    if band > 0:
        rep.add("info", "pilot", f"non-determinism: the same prompt re-scored {band:.3f} apart ({flips} of {len(sample)} rows changed) at temperature 0; improvements inside this band are noise")
    if scalar(ev1) >= 0.97:
        rep.add("warn", "pilot", f"baseline score {scalar(ev1):.2f}: there is no headroom to optimize on this data")
    if scalar(ev1) <= 0.03 and not errors:
        sample_out = next((r.output[:120] for r in ev1.per_example if r.output), "")
        rep.add("warn", "pilot", f"baseline score {scalar(ev1):.2f} although the model answered (e.g. {sample_out!r}): likely a format mismatch between outputs and labels; check the scorer/field", )
    # trace length vs what the reflector shows
    longest = max((len(str(v.get("input", ""))) for r in ev1.per_example for m, vs in (r.trace or {}).items() if isinstance(vs, list) for v in vs), default=0)
    if longest > MAX_INPUT_CHARS_REFLECT:
        rep.add("info", "pilot", f"module inputs reach {longest} chars; the reflector shows the first {MAX_INPUT_CHARS_REFLECT} of each")
    out["examples"] = [{"id": r.example_id, "output": r.output[:400], "parsed": r.parsed, "answer": next((ex.answer for ex in sample if ex.id == r.example_id), None),
                        "metrics": r.metrics, "error": r.error, "path": [m for m in (r.trace or {}) if not m.startswith("_")]} for r in ev1.per_example]
    if rewrite and not errors:
        kids = await tree.apply(random_rewrite(n=1), select.root)
        if kids:
            ev3 = await evaluate(dataset=sample).score(tree, kids[0])
            out["rewrite"] = {"score": scalar(ev3), "delta": scalar(ev3) - scalar(ev1), "template": kids[0].prompt.template[:400]}
            if abs(out["rewrite"]["delta"]) <= max(band, 1e-9) and scalar(ev1) < 0.97:
                rep.add("warn", "pilot", "a random rewrite of the root scored inside the noise band: the prompt may be flat under rewrites on this data (the HotpotQA single-prompt dead end); consider whether the task has prompt-sensitive headroom")
        else:
            rep.add("warn", "pilot", "the rewriter returned no usable variant (placeholders dropped or malformed JSON); reflection rounds may come back empty")
    out["calls"] = task.client.usage.calls
    return out


def _forward_path(spec: ProjectSpec) -> list[str]:
    seen, x = [], spec.entry_id
    while x and x not in seen:
        seen.append(x)
        outs = spec.outgoing(x)
        x = (next((e.target for e in outs if e.default), outs[0].target) if outs else None)
    return seen


# ---- cost projection -----------------------------------------------------------------

def project_cost(spec: ProjectSpec, n_rows: int, *, avg_steps: float | None = None, tokens_per_module: dict[str, float] | None = None,
                 avg_in: float | None = None, avg_out: float | None = None, acceptance: float = 0.3, reflect_in: float = 3000, reflect_out: float = 800) -> dict[str, Any]:
    """Per-round call counts and $ by model. Steps from the pilot (max_steps worst case), acceptance from history."""
    o = spec.optimizer
    steps = avg_steps or 1.0
    full_rows = min(n_rows, o.eval_rows) if o.eval_rows else n_rows
    full_rows = int(full_rows * (1 - o.holdout_frac))
    parents = o.bo.q if o.engine == "bo" else o.parents_per_round
    accepted = parents * o.children * acceptance
    gate = parents * o.children * o.minibatch * steps
    full = accepted * full_rows * steps
    reflect = parents
    critic = parents * o.minibatch if o.feedback == "critic" else 0
    rounds = o.rounds
    calls = {"round0": full_rows * steps, "gate": gate * rounds, "full": full * rounds, "reflect": reflect * rounds, "critic": critic * rounds}
    total_eval_rollouts = calls["round0"] + calls["gate"] + calls["full"]
    pr = prices()
    def usd(model, n_calls, tin, tout):
        pin, pout = pr.get(model) or pr.get(model.split("/")[-1]) or pr.get(".".join(model.split(".")[1:])) or (0.0, 0.0)
        return n_calls * (tin * pin + tout * pout) / 1e6
    # per-module token averages if the pilot measured them, else the flat averages
    eval_usd = 0.0
    if tokens_per_module and steps:
        per_example = total_eval_rollouts / steps
        for m in spec.modules:
            tok = tokens_per_module.get(m.id, 0.0)
            eval_usd += usd(m.model or spec.eval_model, per_example, tok * 0.85, tok * 0.15)
    else:
        eval_usd = usd(spec.eval_model, total_eval_rollouts, avg_in or 800, avg_out or 80)
    reflect_usd = usd(o.reflect_model, calls["reflect"], reflect_in, reflect_out)
    critic_usd = usd(o.critic_model or o.reflect_model, calls["critic"], 1500, 200)
    worst = dict(calls)
    if _has_cycle(spec) and avg_steps and avg_steps < spec.max_steps:
        f = spec.max_steps / steps
        worst = {k: (v * f if k in ("round0", "gate", "full") else v) for k, v in calls.items()}
    return {"calls": calls, "calls_worst_case": worst, "usd": {"eval": eval_usd, "reflect": reflect_usd, "critic": critic_usd,
            "total": eval_usd + reflect_usd + critic_usd}, "assumptions": {"avg_steps": steps, "acceptance": acceptance, "full_rows": full_rows,
            "rounds": rounds, "unpriced_models": [m for m in {spec.eval_model, o.reflect_model, *[x.model for x in spec.modules if x.model]} if not (pr.get(m) or pr.get(".".join(m.split(".")[1:])))]}}


# ---- entry point ---------------------------------------------------------------------

def validate_static(spec: ProjectSpec, rows: list[dict[str, Any]], input_map: dict[str, str]) -> ValidationReport:
    rep = ValidationReport()
    check_graph(spec, rep)
    from .datasets import columns
    cols = columns(rows)
    check_mapping(spec, cols, input_map, rep)
    rep.summary.update(check_labels(spec, rows, input_map, rep))
    if spec.steps:
        # external steps: the wiring (tier 0), then arithmetic over the frozen columns (tier 2). No model calls.
        from .step_validation import age_note, check_coverage, check_frozen, check_signal, check_steps
        check_steps(spec, cols, rep)
        check_frozen(spec, cols, rep)
        check_coverage(spec, rows, rep)
        check_signal(spec, rows, spec.evaluate.label_column, rep)
        age_note(spec, rows, rep)
    return rep
