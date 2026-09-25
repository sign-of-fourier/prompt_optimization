"""External steps (PLAN.md Piece 4, EXTERNAL-STEPS.md).

A node on the canvas is either **part of the program** - a prompt, which the optimizer rewrites - or **not**: an
external step, fixed, part of the environment. This module is everything the studio knows about the second kind.

Two rules decide the whole design:

1. A step is declared as *data* (a manifest), never as Python a caller registers. Users define these through a UI.
2. Evaluation reads a **frozen** snapshot of the step's output, materialised onto the dataset row at capture time;
   serving calls the step **live**. Not for cost - for correctness: a row labelled `escalation` because
   `open_tickets` was 2 when the ticket arrived is a lie six weeks later, and optimizing against it teaches the
   prompt a rule the data no longer supports.

v0 placement is pre-program only: a step whose inputs come from the row. A step whose input is a prompt's output
needs bpto's executor to host non-prompt nodes, which is upstream work and a pin bump.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .models import ProjectSpec, StepSpec

DEFAULT_STEPS_DIR = Path(__file__).resolve().parent.parent / "steps"
FORMAT = 1
TYPES = {"string": str, "number": (int, float), "boolean": bool}


def steps_dir() -> Path:
    """Read per call, not at import: tests point STUDIO_STEPS at a temp directory, and a registry will point it
    somewhere else entirely."""
    return Path(os.environ.get("STUDIO_STEPS") or DEFAULT_STEPS_DIR)


# ---- the manifest ----------------------------------------------------------------------------

class StepField(BaseModel):
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None


class Transport(BaseModel):
    kind: str = "http"
    method: str = "POST"
    url: str
    timeout_s: float = 5.0
    retries: int = 1


class Manifest(BaseModel):
    step: int = FORMAT
    id: str
    version: str = "1.0.0"
    name: str = ""
    blurb: str = ""
    transport: Transport
    # {"kind": "bearer"} - a key the user pastes; {"kind": "oauth", "provider": "hubspot", "scopes": [...]} - a grant
    # the user makes on the provider's own consent screen, which this studio then refreshes on their behalf
    auth: dict[str, Any] = Field(default_factory=dict)
    missing: dict[str, Any] = Field(default_factory=dict)   # how this service says "no record"
    inputs: list[StepField] = Field(default_factory=list)
    outputs: list[StepField] = Field(default_factory=list)
    cacheable: bool = True
    cache_key: list[str] = Field(default_factory=list)
    price_usd_per_call: float = 0.0
    tunables: list[StepField] = Field(default_factory=list)

    @property
    def oauth_provider(self) -> str | None:
        return self.auth.get("provider") if self.auth.get("kind") == "oauth" else None

    @property
    def required_scopes(self) -> list[str]:
        return list(self.auth.get("scopes") or [])

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def sha(self) -> str:
        import hashlib
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def load_manifest(ref: str) -> Manifest | None:
    """`ref` is "id" or "id@version"; the file is steps/<id>.json. A version mismatch is not silently accepted -
    a version pins what it ran, and the manifest on disk having moved on is exactly what that pin is for."""
    name, _, want = ref.partition("@")
    p = steps_dir() / f"{Path(name).name}.json"
    if not p.is_file():
        return None
    m = Manifest.model_validate_json(p.read_text())
    return m if (not want or want == m.version) else None


def list_manifests() -> list[dict[str, Any]]:
    out = []
    for p in sorted(steps_dir().glob("*.json")):
        m = Manifest.model_validate_json(p.read_text())
        out.append({"ref": m.ref, "id": m.id, "version": m.version, "name": m.name or m.id, "blurb": m.blurb,
                    "inputs": [f.name for f in m.inputs], "outputs": [f.name for f in m.outputs],
                    "cacheable": m.cacheable, "price_usd_per_call": m.price_usd_per_call,
                    "auth": m.auth.get("kind", ""), "provider": m.oauth_provider, "scopes": m.required_scopes})
    return out


# ---- naming ----------------------------------------------------------------------------------

def column(step_id: str, field: str) -> str:
    """What a step's output is called everywhere else: a flat `<step>_<field>`. Flat because `{a.b}` is attribute
    access in Python's formatter, so a dotted placeholder would blow up at render time."""
    return f"{step_id}_{field}"


def provided(spec: ProjectSpec) -> dict[str, str]:
    """placeholder -> the step id that provides it, for every enabled step."""
    out: dict[str, str] = {}
    for s in spec.steps:
        if not s.enabled:
            continue
        m = load_manifest(s.manifest)
        for f in (m.outputs if m else []):
            out[column(s.id, f.name)] = s.id
    return out


def step_inputs(spec: ProjectSpec) -> dict[str, str]:
    """column -> step id, for every column an enabled step needs from the row."""
    return {col: s.id for s in spec.steps if s.enabled for col in s.inputs.values()}


# ---- calling one ------------------------------------------------------------------------------

class StepError(Exception):
    pass


Token = "str | Callable[[], Awaitable[str]] | None"


async def _bearer(token) -> str:
    """`token` is a string for a pasted key, or an awaitable-returning callable for an OAuth grant. It has to be the
    second kind for OAuth: an access token lasts thirty minutes, so the value must be fetched per call and the
    refresh must happen inside that fetch, not once at the top of an enrichment over six hundred rows."""
    if token is None:
        return ""
    if isinstance(token, str):
        return token
    return await token()


async def call(m: Manifest, inputs: dict[str, Any], *, token=None, client: httpx.AsyncClient | None = None
               ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One call. Returns (outputs or None when the service says "no record", metrics). Raises StepError for a real
    failure - a timeout, a 5xx, a response that does not match the declared schema - which the caller turns into one
    bad row, never a dead run."""
    t = m.transport
    bearer = await _bearer(token) if m.auth.get("kind") in ("bearer", "oauth") else ""
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=t.timeout_s)
    t0 = time.perf_counter()
    try:
        last: Exception | None = None
        for attempt in range(t.retries + 1):
            try:
                r = await client.request(t.method, t.url, json=inputs, headers=headers, timeout=t.timeout_s)
                break
            except Exception as e:                      # network-level: retry, then give up
                last = e
                if attempt == t.retries:
                    raise StepError(f"{type(e).__name__}: {e}") from e
                await asyncio.sleep(0.2 * (attempt + 1))
        took = round(time.perf_counter() - t0, 4)
        metrics = {"latency_s": took, "usd": m.price_usd_per_call, "fail": 0.0, "missing": 0.0}
        if r.status_code == int(m.missing.get("status", 404)):
            return None, {**metrics, "missing": 1.0}
        if r.status_code == 401 or r.status_code == 403:
            raise StepError(f"{r.status_code}: the step rejected the credential")
        if r.status_code >= 400:
            raise StepError(f"{r.status_code}: {r.text[:200]}")
        try:
            body = r.json()
        except ValueError:
            raise StepError("the response was not JSON")
        if not isinstance(body, dict):
            raise StepError(f"expected a JSON object, got {type(body).__name__}")
        out = {}
        for f in m.outputs:
            if f.name not in body:
                raise StepError(f"the response is missing the declared field {f.name!r}")
            v = body[f.name]
            if v is not None and not isinstance(v, TYPES.get(f.type, object)):
                raise StepError(f"field {f.name!r} should be {f.type}, got {type(v).__name__}")
            if f.enum and v not in f.enum:
                raise StepError(f"field {f.name!r} returned {v!r}, which is not one of {f.enum}")
            out[f.name] = v
        return out, metrics
    finally:
        if own:
            await client.aclose()


# ---- enrichment: frozen onto the rows ----------------------------------------------------------

async def enrich(spec: ProjectSpec, step: StepSpec, rows: list[dict[str, Any]], *, token=None,
                 max_concurrency: int = 4) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run `step` over every row once and return (new rows with the step's columns added, a report).

    This is the capture moment. Cached on the declared key, so a hundred tickets from twenty customers cost twenty
    calls. Never mutates the rows it was given: enrichment produces a new dataset, because the old one is what
    earlier runs and versions were scored against.
    """
    m = load_manifest(step.manifest)
    if m is None:
        raise StepError(f"no manifest {step.manifest!r}")
    # single-flight: rows are fetched concurrently, so the cache has to hold a *promise* per key, not a result.
    # Three tickets from the same customer dispatched at once would otherwise all miss and all call.
    inflight: dict[str, asyncio.Future] = {}
    sem = asyncio.Semaphore(max_concurrency)
    captured = time.time()
    report = {"rows": len(rows), "calls": 0, "cached": 0, "missing": 0, "failed": 0, "latency_s": 0.0,
              "usd": 0.0, "errors": []}

    async with httpx.AsyncClient(timeout=m.transport.timeout_s) as client:
        async def fetch(key: str | None, args: dict[str, Any]):
            if key is not None and key in inflight:
                report["cached"] += 1
                return await inflight[key]
            fut = asyncio.get_running_loop().create_future()
            if key is not None:
                inflight[key] = fut
            async with sem:
                try:
                    out, met = await call(m, args, token=token, client=client)
                    res = (out, met, None)
                except StepError as e:
                    res = (None, None, str(e))
            report["calls"] += 1
            if res[1] is not None:
                report["latency_s"] += res[1]["latency_s"]
                report["usd"] += res[1]["usd"]
            fut.set_result(res)
            return res

        async def one(row: dict[str, Any]) -> dict[str, Any]:
            args = {name: row.get(col) for name, col in step.inputs.items()}
            key = json.dumps([args.get(k) for k in (m.cache_key or list(args))], default=str) if m.cacheable else None
            out, met, err = await fetch(key, args)
            stamped = {**row, column(step.id, "_captured_at"): captured}
            if err is not None:
                report["failed"] += 1
                if err not in report["errors"] and len(report["errors"]) < 5:
                    report["errors"].append(err)
                return {**stamped, column(step.id, "_error"): err}
            if out is None:
                report["missing"] += 1
                return {**stamped, **{column(step.id, f.name): None for f in m.outputs}}
            return {**stamped, **{column(step.id, k): v for k, v in out.items()}}

        enriched = await asyncio.gather(*[one(r) for r in rows])

    report["coverage"] = (len(rows) - report["missing"] - report["failed"]) / max(1, len(rows))
    report["captured_at"] = captured
    return list(enriched), report
