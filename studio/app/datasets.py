"""Upload -> rows -> bpto Dataset. Column mapping is explicit: which columns are inputs (by placeholder name) and
which is the label. `id` is kept when present."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from bpto import Dataset


def parse_upload(name: str, data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig")
    low = name.lower()
    if low.endswith(".jsonl"):
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    if low.endswith(".json"):
        obj = json.loads(text)
        if isinstance(obj, dict):
            obj = obj.get("data") or obj.get("rows") or obj.get("examples") or list(obj.values())[0]
        return list(obj)
    if low.endswith(".csv") or low.endswith(".tsv"):
        dialect = "excel-tab" if low.endswith(".tsv") else "excel"
        return list(csv.DictReader(io.StringIO(text), dialect=dialect))
    raise ValueError("unsupported file type; use .jsonl, .json, .csv or .tsv")


def flatten(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """bpto-style rows ({"inputs": {...}, "answer": ...}) become flat columns so mapping is uniform."""
    out = []
    for r in rows:
        if isinstance(r.get("inputs"), dict):
            flat = {**r["inputs"], **{k: v for k, v in r.items() if k != "inputs"}}
        else:
            flat = dict(r)
        out.append(flat)
    return out


def columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for r in rows:
        for k in r:
            if k not in seen:
                seen.append(k)
    return seen


def to_dataset(rows: list[dict[str, Any]], input_map: dict[str, str], label_column: str | None, id_column: str | None = None) -> Dataset:
    """input_map: placeholder -> column."""
    recs = []
    for i, r in enumerate(rows):
        recs.append({"id": str(r.get(id_column or "id", i)) if (id_column or "id") in r else str(i),
                     "inputs": {ph: r.get(col) for ph, col in input_map.items()},
                     "answer": r.get(label_column) if label_column else None,
                     "meta": {k: v for k, v in r.items() if k not in input_map.values() and k != label_column}})
    return Dataset.from_records(recs)
