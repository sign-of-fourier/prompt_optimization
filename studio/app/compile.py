"""Canvas spec -> bpto objects. The only module that knows both sides.

- `build_schema`   ModuleSpec fields -> a pydantic model (with `next: Literal[edge names]` on orchestrators)
- `build_program`  ProjectSpec -> bpto.Program (modules + edges)
- `build_task`     ProjectSpec + dataset + client -> bpto.Task
- `build_schedule` OptimizerSpec -> a `run()` schedule (GEPA or BO in the expand seat; the gate in the survive seat)
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from bpto import (Dataset, DescendantValue, Edge, Example, LinearObjective, ModelClient, ModelConfig, Program, Prompt,
                  Task, Tree, combine, output_token_count, select, token_count, trace_visits)
from bpto.gepa import ReflectiveExpander
from bpto.gepa.loop import _accepted, minibatch_for
from bpto.gepa.reflect import REFLECT_PROMPT, default_feedback
from bpto.gepa.select import candidates, pareto_sample
from bpto.ops import Op, evaluate
from bpto.search import Step, Stop, step
from bpto.tree import ExampleResult, Node

from . import scorers as S
from .models import ModuleSpec, OptimizerSpec, ProjectSpec

PY_TYPES = {"string": str, "number": float, "boolean": bool, "string[]": list[str]}


# ---- schemas -----------------------------------------------------------------------

def build_schema(spec: ProjectSpec, m: ModuleSpec) -> type[BaseModel] | None:
    fields: dict[str, Any] = {}
    for f in m.schema_fields:
        t = Literal[tuple(f.enum)] if f.enum else PY_TYPES[f.type]  # type: ignore[misc]
        fields[f.name] = (t, Field(description=f.description))
    if spec.is_orchestrating(m.id):
        names = tuple(e.name for e in spec.outgoing(m.id))
        fields["next"] = (Literal[names], Field(description="Which step to take next: one of " + ", ".join(names)))  # type: ignore[misc]
    if not fields:
        return None
    return create_model(f"Out_{m.id}", **fields)


def build_program(spec: ProjectSpec) -> Program:
    edges = {}
    for e in spec.edges:
        edges.setdefault(e.source, []).append(Edge(to=e.target, name=e.name, mapping=e.mapping, default=e.default))
    return Program(modules={m.id: Prompt(template=m.template) for m in spec.modules}, entry=spec.entry_id,
                   edges=edges or None, max_steps=spec.max_steps)


# ---- task --------------------------------------------------------------------------

def build_task(spec: ProjectSpec, dataset: Dataset, client: ModelClient, *, judge_client: ModelClient | None = None) -> Task:
    ev = spec.evaluate
    parts = []
    for sc in ev.scorers:
        jc = ModelConfig(model=sc.judge_model, temperature=0.0) if sc.judge_model else ModelConfig(temperature=0.0)
        parts.append(S.build(sc, judge_client=judge_client or client, judge_config=jc))
    if ev.token_count:
        parts += [token_count(), output_token_count(), S.program_template_tokens()]
    return Task(
        root=build_program(spec),
        description={m.id: m.description or f"module {m.id}" for m in spec.modules},
        dataset=dataset,
        schema={m.id: build_schema(spec, m) for m in spec.modules},
        # evaluation calls pin temperature 0 (Nova's service default is 0.7; see bpto README "Notes")
        config={m.id: ModelConfig(model=m.model or spec.eval_model, max_tokens=m.max_tokens, temperature=0.0) for m in spec.modules},
        scorer=combine(*parts),
        objective=LinearObjective(**ev.objective),
        client=client,
        expander_config=ModelConfig(model=spec.optimizer.reflect_model, max_tokens=2048, temperature=spec.optimizer.reflect_temperature),
    )


# ---- feedback ----------------------------------------------------------------------

CRITIC_PROMPT = (
    "You are reviewing one run of a multi-step LLM program on one example, to help a prompt engineer improve the "
    "prompt of the step named '{module}'.\n\nExample inputs:\n{inputs}\n\nExpected answer: {expected}\n\n"
    "What the program did (each step's input and output, in order):\n{trace}\n\nFinal output: {output}\nMetrics: {metrics}\n\n"
    "Write 2-4 sentences of concrete, specific feedback about what the '{module}' step got wrong or right on this "
    "example and what its prompt should do differently. Name the failure, not the fix in general terms."
)


class CriticNote(BaseModel):
    feedback: str


# ---- compression goal ------------------------------------------------------------------
# bpto's REFLECT_PROMPT asks the rewriter to "add concrete rules, a strategy, brief examples": it only ever grows a
# prompt, so a token penalty in the objective alone just rejects every child. Compression (as in bpto's
# tasks/compression) swaps the directive: keep what prevents the shown failures, cut the rest.

COMPRESS_REFLECT_PROMPT = (
    "I gave an assistant the following prompt template to perform a task. The template is supposed to: {description}\n"
    "It must keep these placeholders exactly, in curly braces: {placeholders}\n\n"
    "Current prompt template:\n<prompt>\n{prompt}\n</prompt>\n\n"
    "Here are examples of task inputs, the assistant's response under this prompt, and feedback on each:\n\n"
    "{directive}\n"
    "Goal: a SHORTER prompt template with the same or better accuracy. Read the feedback: keep whatever rule prevents "
    "the failures shown, cut everything else - filler, repetition, explanations the model does not need. If the "
    "feedback shows failures, fix them in as few words as possible; never add rules for cases that already pass. "
    "Then write {n} shorter prompt template(s). Each must be complete and usable on its own.{seed}"
)

COMPRESS_CRITIC_PROMPT = (
    "You are reviewing one run of a multi-step LLM program on one example, to help a prompt engineer make the prompt "
    "of the step named '{module}' SHORTER without losing accuracy.\n\nExample inputs:\n{inputs}\n\nExpected answer: {expected}\n\n"
    "What the program did (each step's input and output, in order):\n{trace}\n\nFinal output: {output}\nMetrics: {metrics}\n\n"
    "Write 2-3 sentences: if the example failed, name the one rule the '{module}' prompt needs to fix it; if it passed, "
    "say so and name any wording in that step's prompt this example shows to be unnecessary. Do not suggest additions "
    "for cases that already work."
)


def compress_feedback(fallback):
    """Feedback for the compression goal: the usual text plus the template's token count, so the rewriter sees the
    number it is asked to lower (bpto tasks/compression does the same)."""
    def _fb(ex: Example, r: ExampleResult) -> str:
        tok = r.metrics.get("template_tokens")
        return fallback(ex, r) + (f"; template tokens: {tok:.0f} (shorter is better)" if tok is not None else "")
    return _fb


def correct_rows_pass(objective: dict[str, float]):
    """Under a token-penalized objective no row scores >= 1, so bpto's default `passed` would show the rewriter every
    row as a failure. A row passes when each positively weighted metric (the accuracy-like ones) is at its maximum."""
    keys = [k for k, w in objective.items() if w > 0]

    def _passed(ex: Example, r: ExampleResult) -> bool:
        return not r.error and all(r.metrics.get(k, 0.0) >= 1.0 for k in keys)
    return _passed


def _predicted(r: ExampleResult) -> Any:
    p = r.parsed
    if isinstance(p, dict) and len(p) == 1:
        return next(iter(p.values()))
    return p if p is not None else r.output


def templated_feedback(template: str):
    def _fb(ex: Example, r: ExampleResult) -> str:
        if r.error:
            return f"error: {r.error}"
        return template.format(expected=ex.answer, predicted=_predicted(r), metrics=json.dumps(r.metrics),
                               inputs=json.dumps(ex.inputs, default=str)[:1500], output=r.output[:1500])
    return _fb


def critic_feedback(fallback):
    """Reads the note a `Critique` step stored on the result; falls back to templated text."""
    def _fb(ex: Example, r: ExampleResult) -> str:
        note = (r.trace or {}).get("_critic")
        return note if isinstance(note, str) and note else fallback(ex, r)
    return _fb


class Critique(Op):
    """Before reflection: run the critic over exactly the rows the reflector will show for each parent and stash
    the note in `result.trace["_critic"]`. ~minibatch calls per parent; the reflector's own row pick is reused so
    the two agree."""
    name = "critique"

    def __init__(self, expander: ReflectiveExpander, client: ModelClient, config: ModelConfig, module: str | None,
                 prompt: str = CRITIC_PROMPT):
        self.expander, self.client, self.config, self.module, self.prompt = expander, client, config, module, prompt

    async def run_one(self, tree: Tree, node: Node) -> list[Node]:
        src, rows = self.expander.pick(tree, node)
        if src is None:
            return []
        for ex, r in rows:
            if isinstance((r.trace or {}).get("_critic"), str):
                continue
            trace_txt = "\n".join(f"[{m} #{v.get('step_idx', 0)}] input: {str(v.get('input', ''))[:800]}\n  output: {str(v.get('output', ''))[:400]}"
                                  for m in (r.trace or {}) if not m.startswith("_") for v in trace_visits(r, m)) or "(single step)"
            text = self.prompt.format(module=self.module or tree.task.root.entry, inputs=json.dumps(ex.inputs, default=str)[:2000],
                                        expected=ex.answer, trace=trace_txt, output=(r.output or "")[:800], metrics=json.dumps(r.metrics))
            try:
                note = (await self.client.complete(text, config=self.config, schema=CriticNote)).parsed_as(CriticNote).feedback
            except Exception as e:  # the critic is advisory; a failed note falls back to templated feedback
                note = ""
            r.trace = {**(r.trace or {}), "_critic": note}
        return []


# ---- schedule ----------------------------------------------------------------------

def build_schedule(spec: ProjectSpec, task: Task, *, embedder=None, bo_selector=None):
    """A `run()` schedule. Round 0 evaluates the root. Each later round: [critique] -> reflect (parents from the
    expand seat: GEPA's Pareto sample or BO's q-EI) -> minibatch gate -> full evaluation of the accepted."""
    o = spec.optimizer
    modules = spec.mutate or [m.id for m in spec.modules]
    if len(modules) == 1 and modules[0] == spec.entry_id:
        modules = [None]  # entry only: no `module` tag, as a single-prompt run
    full = task.dataset
    ids = {ex.id for ex in full}
    reflect_cfg = task.expander_config
    critic_cfg = ModelConfig(model=o.critic_model or o.reflect_model, max_tokens=512, temperature=0.0)
    base_fb = templated_feedback(o.feedback_template) if o.feedback == "templated" else default_feedback
    fb = critic_feedback(base_fb) if o.feedback == "critic" else base_fb
    compress = o.goal == "compress"
    if compress:
        fb = compress_feedback(fb)
    meta_prompt = COMPRESS_REFLECT_PROMPT if compress else REFLECT_PROMPT
    critic_prompt = COMPRESS_CRITIC_PROMPT if compress else CRITIC_PROMPT
    passed = correct_rows_pass(spec.evaluate.objective) if compress else None

    bo = None
    if o.engine == "bo":
        from bpto.bo import BOSelector, EI, AdditiveGPR, GPR, KrigingBeliever, QEI, QuantecarloQEI
        batch = {"qei": QEI(seed=o.seed), "kriging": KrigingBeliever(), "quantecarlo": QuantecarloQEI()}[o.bo.acquisition]
        multi = len(spec.modules) > 1
        bo = bo_selector or BOSelector(embedder, AdditiveGPR() if multi else GPR(), EI(),
                                       value=DescendantValue(generations=1, agg=list), transform="pit", pca=o.bo.pca, batch=batch)

    def schedule(tree: Tree, r: int) -> list[Step]:
        if not tree.evaluated_nodes():
            return [step(evaluate(dataset=full), select.root, name="root")]
        module = modules[(r - 1) % len(modules)]
        mb = minibatch_for(r, full, o.minibatch, o.seed)
        is_new = lambda n: n.origin.op == "reflect" and n.evaluation is None
        on_mb = lambda n: n.origin.op == "reflect" and n.evaluation is not None and not ids.issubset(set(n.evaluation.dataset_ids))
        expander = ReflectiveExpander(fb, minibatch=o.minibatch, n=o.children, seed=o.seed + r, config=reflect_cfg, module=module,
                                      meta_prompt=meta_prompt, passed=passed)
        if o.engine == "bo":
            pool = lambda t: candidates(t, ids)
            def parents(t):
                c = pool(t)
                return bo.top(min(o.bo.q, len(c)), among=pool)(t) if len(c) > 1 else c[:1]
        else:
            parents = pareto_sample(o.parents_per_round, mode=o.mode, seed=o.seed * 7919 + r, ids=ids)
        steps = []
        if o.feedback == "critic":
            steps.append(step(Critique(expander, task.expander_client, critic_cfg, module, critic_prompt), parents, name=f"r{r}/critique"))
        steps += [
            step(expander, parents, name=f"r{r}/reflect"),
            step(evaluate(dataset=mb), select.where(is_new), name=f"r{r}/minibatch"),
            step(evaluate(dataset=full), lambda t: _accepted(t, select.where(on_mb)(t)), name=f"r{r}/full"),
        ]
        return steps

    stop = Stop(rounds=o.rounds + 1, no_improvement_rounds=o.no_improvement_rounds)
    return schedule, stop
