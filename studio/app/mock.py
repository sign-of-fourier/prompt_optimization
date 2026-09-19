"""A schema-generic mock model for offline runs (STUDIO_MOCK=1 or a project's `mock` flag): fills any structured
schema with plausible values, answers free text with a snippet of the prompt, and rewrites prompts by appending a
cue. Plumbing checks only - it knows nothing about the task."""
from __future__ import annotations

import hashlib
import random
import re
import typing

from pydantic import BaseModel

from bpto import MockClient
from bpto.ops import Variants
from bpto.scoring import JudgeVerdict

CUES = ["Be precise.", "Think step by step.", "Answer with the shortest exact span.", "Compare both entities.", "Copy names exactly."]


def _seed(prompt: str) -> random.Random:
    return random.Random(int(hashlib.md5(prompt.encode()).hexdigest(), 16))


def handler(prompt: str, cfg, schema: type[BaseModel] | None):
    rnd = _seed(prompt)
    if schema is Variants:
        base = re.search(r"<prompt>\n(.*?)\n</prompt>", prompt, re.S)
        n = int(re.search(r"(?:Return|write) (\d+)", prompt).group(1)) if re.search(r"(?:Return|write) (\d+)", prompt) else 1
        t = base.group(1) if base else prompt[:200]
        return Variants(prompts=[f"{t} {rnd.choice(CUES)}" for _ in range(n)])
    if schema is JudgeVerdict:
        return JudgeVerdict(score=rnd.choice([0.0, 0.5, 1.0]), reason="mock")
    if schema is None:
        # echo something label-like: the last capitalised token or the last 6 words
        m = re.findall(r"\b[A-Z][a-zA-Z0-9]+\b", prompt)
        return (m[-1] if m and rnd.random() < 0.6 else " ".join(prompt.split()[-6:]))
    values = {}
    for name, f in schema.model_fields.items():
        ann = f.annotation
        origin = typing.get_origin(ann)
        if origin is typing.Literal:
            values[name] = rnd.choice(typing.get_args(ann))
        elif ann is bool:
            values[name] = rnd.random() < 0.5
        elif ann in (int, float):
            values[name] = rnd.randint(0, 100) if ann is int else round(rnd.random(), 2)
        elif origin is list:
            m = re.findall(r"^([^\n:]{1,80}): ", prompt, re.M)
            values[name] = m[:2] if m else ["a", "b"]
        else:
            m = re.findall(r"\b[A-Z][a-zA-Z0-9]+\b", prompt)
            values[name] = (m[-1] if m and rnd.random() < 0.6 else " ".join(prompt.split()[-5:]))
    return schema(**values)


def mock_client(**kw) -> MockClient:
    return MockClient(handler, **kw)
