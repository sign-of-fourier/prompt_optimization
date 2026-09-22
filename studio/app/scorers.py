"""Point-and-click scorers: each `ScorerSpec.type` maps to a bpto `Scorer`. Metrics stay vectors (bpto rule)."""
from __future__ import annotations

import json
import re
import string
from collections import Counter
from typing import Any

from bpto import ModelClient, ModelConfig, Program, llm_judge
from bpto.scoring import JudgeVerdict

from .models import ScorerSpec

DEFAULT_NAMES = {"exact_match": "accuracy", "contains": "contains", "token_f1": "f1", "regex": "regex_match",
                 "json_field": "field_match", "numeric": "numeric_match", "llm_judge": "judge", "llm_judge_free": "judge"}


def normalize(s: Any) -> str:
    s = str(s if s is not None else "").lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return " ".join(s.split())


def predicted(completion, field: str | None) -> Any:
    """The thing to compare: a parsed field, the whole parsed object, or the text."""
    p = completion.parsed
    if hasattr(p, "model_dump"):
        p = p.model_dump()
    if field:
        return p.get(field) if isinstance(p, dict) else None
    return p if p is not None else completion.text


def token_f1(pred: str, gold: str) -> float:
    a, b = normalize(pred).split(), normalize(gold).split()
    common = sum((Counter(a) & Counter(b)).values())
    if not a or not b or common == 0:
        return 0.0
    p, r = common / len(a), common / len(b)
    return 2 * p * r / (p + r)


def build(spec: ScorerSpec, judge_client: ModelClient | None = None, judge_config: ModelConfig | None = None):
    name = spec.name or DEFAULT_NAMES[spec.type]
    norm = normalize if spec.normalize else (lambda x: str(x if x is not None else ""))
    t = spec.type

    if t == "exact_match":
        def _s(prompt, ex, comp, ctx):
            got = predicted(comp, spec.field)
            if isinstance(got, (dict, list)):
                got = json.dumps(got, sort_keys=True)
            return {name: 1.0 if norm(got) == norm(ex.answer) else 0.0}
    elif t == "contains":
        def _s(prompt, ex, comp, ctx):
            got = predicted(comp, spec.field)
            return {name: 1.0 if norm(ex.answer) and norm(ex.answer) in norm(got) else 0.0}
    elif t == "token_f1":
        def _s(prompt, ex, comp, ctx):
            return {name: token_f1(str(predicted(comp, spec.field) or ""), str(ex.answer or ""))}
    elif t == "regex":
        rx = re.compile(spec.pattern or "")

        def _s(prompt, ex, comp, ctx):
            got = str(predicted(comp, spec.field) or "")
            m = rx.search(got)
            if ex.answer is None:           # pattern presence only
                return {name: 1.0 if m else 0.0}
            hit = m.group(1) if m and m.groups() else (m.group(0) if m else "")
            return {name: 1.0 if norm(hit) == norm(ex.answer) else 0.0}
    elif t == "json_field":
        def _s(prompt, ex, comp, ctx):
            got = predicted(comp, spec.field)
            gold = ex.answer
            if isinstance(gold, dict) and spec.field:
                gold = gold.get(spec.field)
            return {name: 1.0 if norm(got) == norm(gold) else 0.0}
    elif t == "numeric":
        def _s(prompt, ex, comp, ctx):
            try:
                got = float(re.sub(r"[^0-9.\-eE]", "", str(predicted(comp, spec.field))))
                gold = float(ex.answer)
            except (TypeError, ValueError):
                return {name: 0.0}
            return {name: 1.0 if abs(got - gold) <= spec.tolerance * max(1.0, abs(gold)) else 0.0}
    elif t == "llm_judge":
        return llm_judge(spec.rubric or "Is the model answer correct given the reference answer?", name=name,
                         client=judge_client, config=judge_config)
    elif t == "llm_judge_free":
        tmpl = ("You are grading an answer.\nRubric: {rubric}\n\nInput given to the model:\n<input>\n{inputs}\n</input>\n\n"
                "Model answer:\n<answer>\n{output}\n</answer>\n\nGive a score from 0 to 1 and a one-sentence reason.")

        async def _s(prompt, ex, comp, ctx):
            judge = judge_client or ctx.client
            text = tmpl.format(rubric=spec.rubric, inputs=json.dumps(ex.inputs, default=str), output=comp.text)
            v = (await judge.complete(text, config=judge_config, schema=JudgeVerdict)).parsed_as(JudgeVerdict)
            return {name: max(0.0, min(1.0, v.score))}
    else:
        raise ValueError(f"unknown scorer {t}")
    return _s


def needs_label(spec: ScorerSpec) -> bool:
    """Reference-free scorers: the judge without a reference, and a regex used for presence only."""
    return spec.type != "llm_judge_free" and not (spec.type == "regex" and spec.field is None)


def program_template_tokens(name: str = "template_tokens"):
    """Tokens of the prompt templates themselves (placeholders blanked), summed over every step of a program: the
    thing compression shrinks, independent of the example. bpto's `template_tokens` counts only the entry template.
    Counted once per program via the client's tokenizer."""
    cache: dict[str, int] = {}

    async def _score(prompt, example, completion, ctx):
        key = prompt.hash
        if key not in cache:
            mods = prompt.modules if isinstance(prompt, Program) else {None: prompt}
            cfgs = ctx.task.config
            total = 0
            for mid, p in mods.items():
                cfg = cfgs.get(mid) if isinstance(cfgs, dict) else cfgs
                total += await ctx.client.count_tokens(p.template.format(**{ph: "" for ph in p.placeholders}), cfg)
            cache[key] = total
        return {name: float(cache[key])}
    return _score
