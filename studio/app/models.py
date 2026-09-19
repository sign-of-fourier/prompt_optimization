"""The canvas spec: what the UI edits and what `compile` turns into a bpto Task + schedule.

Everything here is plain JSON-serialisable data. Model classes, scorers and feedback are *names* resolved at
compile time; nothing in the spec is code.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

FieldType = Literal["string", "number", "boolean", "string[]"]


class SchemaField(BaseModel):
    name: str
    type: FieldType = "string"
    description: str = ""
    enum: list[str] | None = None  # for `next` on an orchestrating module this is filled from its edges


class ModuleSpec(BaseModel):
    id: str
    template: str
    description: str = ""          # what the module is supposed to do - read by the rewriter
    schema_fields: list[SchemaField] = Field(default_factory=list)  # empty = free text
    model: str | None = None       # None -> the project's default eval model
    max_tokens: int = 512


class EdgeSpec(BaseModel):
    source: str
    target: str
    name: str = ""                 # the `next` value that selects it (orchestrating sources only)
    mapping: dict[str, str] = Field(default_factory=dict)  # target placeholder <- source output field ("$text" = raw)
    default: bool = False


ScorerType = Literal["exact_match", "contains", "token_f1", "regex", "json_field", "numeric", "llm_judge", "llm_judge_free"]


class ScorerSpec(BaseModel):
    type: ScorerType = "exact_match"
    name: str = ""                 # metric name; defaults per type
    field: str | None = None       # output field to compare (None = whole text / whole parsed)
    normalize: bool = True         # lowercase/strip punctuation+articles for string comparisons
    pattern: str | None = None     # regex
    tolerance: float = 0.0         # numeric
    rubric: str = ""               # llm_judge*
    judge_model: str | None = None


class EvaluateSpec(BaseModel):
    label_column: str | None = "answer"   # None only with llm_judge_free
    scorers: list[ScorerSpec] = Field(default_factory=lambda: [ScorerSpec()])
    token_count: bool = True
    objective: dict[str, float] = Field(default_factory=lambda: {"accuracy": 1.0})
    pareto: dict[str, bool] = Field(default_factory=dict)   # metric -> maximize, for the run view


class BOSpec(BaseModel):
    q: int = 2
    pca: int = 4
    acquisition: Literal["qei", "kriging", "quantecarlo"] = "qei"


class OptimizerSpec(BaseModel):
    engine: Literal["gepa", "bo"] = "gepa"
    mode: Literal["weighted", "uniform", "best"] = "weighted"   # expand seat (gepa)
    bo: BOSpec = Field(default_factory=BOSpec)                   # expand seat (bo)
    parents_per_round: int = 1
    children: int = 1
    minibatch: int = 5                                           # survive seat: the only knob
    rounds: int = 10
    no_improvement_rounds: int | None = 4
    max_usd: float | None = 2.0
    max_calls: int | None = None
    reflect_model: str = "us.amazon.nova-micro-v1:0"
    reflect_temperature: float = 1.0
    feedback: Literal["plain", "templated", "critic"] = "critic"
    feedback_template: str = "expected: {expected}; model answered: {predicted}; metrics: {metrics}"
    critic_model: str | None = None                              # None -> reflect_model
    eval_rows: int | None = None                                 # downsample the eval set (never the minibatch)
    holdout_frac: float = 0.2
    seed: int = 0

    @model_validator(mode="after")
    def _floors(self):
        if self.minibatch < 3:
            raise ValueError("minibatch below 3 demonstrably harms the gate; 3 is the floor")
        return self


class ProjectSpec(BaseModel):
    name: str = "untitled"
    modules: list[ModuleSpec]
    edges: list[EdgeSpec] = Field(default_factory=list)
    entry: str | None = None       # None -> first module
    max_steps: int = 8
    eval_model: str = "us.amazon.nova-micro-v1:0"
    evaluate: EvaluateSpec = Field(default_factory=EvaluateSpec)
    optimizer: OptimizerSpec = Field(default_factory=OptimizerSpec)
    mutate: list[str] | None = None  # modules the search rewrites (round-robin); None = all
    layout: dict[str, Any] = Field(default_factory=dict)  # canvas positions etc.; opaque to the backend

    @property
    def entry_id(self) -> str:
        return self.entry or self.modules[0].id

    def module(self, mid: str) -> ModuleSpec:
        return next(m for m in self.modules if m.id == mid)

    def outgoing(self, mid: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.source == mid]

    def is_orchestrating(self, mid: str) -> bool:
        return len(self.outgoing(mid)) > 1


class Issue(BaseModel):
    level: Literal["error", "warn", "info"]
    stage: str          # graph | mapping | labels | scorer | pilot | cost
    where: str = ""     # module id, column, row id
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    issues: list[Issue] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    def add(self, level, stage, message, where="", **data):
        self.issues.append(Issue(level=level, stage=stage, where=where, message=message, data=data))
