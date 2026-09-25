"""Project bundles: one JSON document = a ProjectSpec + its dataset (rows, mapping, label) + a blurb.

The same document serves three purposes: the examples library (`examples/*.json`, checked in, read-only), export
("Download this project" - optionally with a run's best prompts as the templates) and import. Runs are never
bundled: they carry caches, spend and per-user cost; "the optimized prompts" is just a spec whose templates are them.
Rows are inlined up to MAX_INLINE_BYTES; a larger dataset exports without rows and says so (S3-backed datasets later).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import db
from .datasets import columns, flatten, parse_upload
from .models import ProjectSpec

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples"
SAMPLE_DIR = ROOT / "sample"
MAX_INLINE_BYTES = 2 * 1024 * 1024
FORMAT = 1


class BundleDataset(BaseModel):
    name: str = "dataset"
    rows: list[dict[str, Any]] | None = None     # inline rows, or
    sample: str | None = None                    # a file under sample/ (library entries only; keeps the repo small)
    input_map: dict[str, str] = Field(default_factory=dict)
    label_column: str | None = None
    omitted: str | None = None                   # set on export when rows were too large to inline


class Bundle(BaseModel):
    bundle: int = FORMAT
    name: str
    blurb: str = ""                              # one line, on the card
    description: str = ""                        # a few sentences, in the detail view
    tags: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)   # what the user must have before this works
    updated: str = ""                            # ISO date; the library sorts newest first within a tag
    order: int = 100                             # tiebreak for entries we want pinned
    spec: ProjectSpec
    dataset: BundleDataset | None = None

    def rows(self) -> list[dict[str, Any]] | None:
        d = self.dataset
        if d is None:
            return None
        if d.rows is not None:
            return d.rows
        if d.sample:
            p = SAMPLE_DIR / Path(d.sample).name  # no path traversal: sample files only
            return flatten(parse_upload(p.name, p.read_bytes()))
        return None


# ---- library --------------------------------------------------------------------------

def load_example(slug: str) -> Bundle | None:
    p = EXAMPLES_DIR / f"{Path(slug).name}.json"
    if not p.is_file():
        return None
    return Bundle.model_validate_json(p.read_text())


def list_examples() -> list[dict[str, Any]]:
    out = []
    for p in sorted(EXAMPLES_DIR.glob("*.json")):
        b = Bundle.model_validate_json(p.read_text())
        rows = b.rows()
        # "modules" and "steps" are different things now: prompts the optimizer rewrites, and external steps it does not
        out.append({"slug": p.stem, "name": b.name, "blurb": b.blurb, "description": b.description, "tags": b.tags,
                    "requires": b.requires, "updated": b.updated, "order": b.order,
                    "modules": len(b.spec.modules), "steps": len(b.spec.steps),
                    "rows": len(rows) if rows else 0, "goal": b.spec.optimizer.goal, "dataset": b.dataset.name if b.dataset else None,
                    "eval_model": b.spec.eval_model, "rounds": b.spec.optimizer.rounds})
    return sorted(out, key=lambda e: (e["order"], e["name"]))


def tags() -> list[str]:
    """Every tag in the library, most used first: the filter chips are derived, never hand-maintained."""
    from collections import Counter
    c = Counter(t for e in list_examples() for t in e["tags"])
    return [t for t, _ in c.most_common()]


# ---- export / import ----------------------------------------------------------------------

def export_bundle(con, project: dict, dataset: dict | None, *, templates: dict[str, str] | None = None, blurb: str = "") -> Bundle:  # noqa: E501
    """`project` / `dataset` are db rows (spec already parsed). `templates` (module id -> template), e.g. a run's best
    node, replaces the templates of the spec - the way to bundle 'the optimized prompts'."""
    spec = ProjectSpec.model_validate(project["spec"])
    if templates:
        for m in spec.modules:
            if m.id in templates:
                m.template = templates[m.id]
    spec.layout = {k: v for k, v in spec.layout.items() if k != "tutorial"}
    ds = None
    if dataset:
        rows = json.loads(Path(dataset["path"]).read_text())
        size = Path(dataset["path"]).stat().st_size
        ds = BundleDataset(name=dataset["name"], input_map=dataset["input_map"], label_column=dataset["label_column"],
                           rows=rows if size <= MAX_INLINE_BYTES else None,
                           omitted=None if size <= MAX_INLINE_BYTES else f"{size // 1024} KB of rows exceed the {MAX_INLINE_BYTES // 1024 // 1024} MB inline limit; attach the file separately")
    return Bundle(name=spec.name, blurb=blurb, spec=spec, dataset=ds)


def import_bundle(con, user_id: str, b: Bundle, *, datasets_dir: Path, unique_name, coerce_models) -> dict[str, Any]:
    """Creates the project (and its dataset when rows are present) under `user_id`. `unique_name(con, user_id, name)`
    and `coerce_models(spec)` are main.py's; the same rules as a hand-made project apply."""
    spec = b.spec.model_copy(deep=True)
    spec.name = unique_name(con, user_id, b.name or spec.name or "untitled")
    spec.layout = {**spec.layout, "tutorial": {"step": 0, "dismissed": True}}  # a bundle is never the empty canvas the tutorial opens on
    coerce_models(spec)
    pid = db.new_id()
    con.execute("insert into projects values (?,?,?,?,?)", (pid, user_id, spec.name, spec.model_dump_json(), db.now()))
    out: dict[str, Any] = {"id": pid, "name": spec.name, "dataset": None}
    rows = b.rows()
    if rows:
        did = db.new_id()
        path = datasets_dir / f"{did}.json"
        path.write_text(json.dumps(rows, default=str))
        d = b.dataset
        con.execute("insert into datasets values (?,?,?,?,?,?,?,?,?)",
                    (did, pid, d.name, str(path), json.dumps(columns(rows)), len(rows), json.dumps(d.input_map), d.label_column, db.now()))
        out["dataset"] = {"id": did, "name": d.name, "n_rows": len(rows)}
    con.commit()
    return out
