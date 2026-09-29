"""The serving box: hosted versions on their own machine, away from optimization runs (PLAN.md Piece 2's split).

    uvicorn app.serve_box:app --host <private ip> --port 8200

Same code as the studio, a different entry point: it answers `/v/{vid}` and `/v/{vid}/run` through the same
`serving.serve` the studio uses (so an answer is byte for byte what evaluation produced), and it knows nothing about
the canvas, runs, datasets or the studio's database. The studio pushes what it needs over `/internal/*`.

Layout under SERVE_DATA - customers' data under `tenants/`, one folder each, and nothing of ours in there:

    keys.json                                  API key hashes -> owner and tier (sha256 only, never the key)
    tenants/<user_id>/versions/<vid>/version.json          the pinned spec, as published
    tenants/<user_id>/versions/<vid>/steps/<step>.json     a search step's passages (document, heading, text)
    tenants/<user_id>/versions/<vid>/steps/<step>.<model>.npy   their vectors
    tenants/<user_id>/ledger.jsonl             one line per served request and per model call; the studio pulls it

Deleting a customer is removing their folder and their key hashes. Environment: SERVE_DATA, SERVE_TOKEN (the shared
secret for /internal, from the studio's .env), SERVE_MOCK=1 (offline model and embedder, for tests).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import serving, tiers
from .clients import Access
from .models import ProjectSpec

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # user, version, key and step ids: nothing that can climb out of a folder


def root() -> Path:
    return Path(os.environ.get("SERVE_DATA", Path(__file__).resolve().parent.parent / "serve_data"))


def mock() -> bool:
    return os.environ.get("SERVE_MOCK") == "1"


def _id(x: str, what: str) -> str:
    if not ID.match(x or ""):
        raise HTTPException(400, f"bad {what}")
    return x


def tenant(uid: str) -> Path:
    return root() / "tenants" / _id(uid, "user id")


def _write(p: Path, data: str | bytes) -> None:
    """Atomic: a request never reads half a version."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    tmp.replace(p)


# ---- keys ------------------------------------------------------------------------------------

_keys_lock = asyncio.Lock()


def _keys() -> dict[str, dict]:
    p = root() / "keys.json"
    return json.loads(p.read_text()) if p.exists() else {}


def key_owner(request: Request) -> dict:
    """The same header contract as the studio's serving route: `Authorization: Bearer <key>` or `X-API-Key`."""
    hdr = request.headers.get("authorization") or ""
    key = hdr[7:].strip() if hdr.lower().startswith("bearer ") else (request.headers.get("x-api-key") or "").strip()
    rec = _keys().get(hashlib.sha256(key.encode()).hexdigest()) if key else None   # the studio's _key_hash
    if not rec:
        raise HTTPException(401, "a workspace API key is required: send it as `Authorization: Bearer <key>`")
    return rec


def internal(request: Request) -> None:
    tok = os.environ.get("SERVE_TOKEN")
    if not tok:
        raise HTTPException(503, "this serving box has no SERVE_TOKEN")
    if request.headers.get("x-serve-token") != tok:
        raise HTTPException(401, "bad serve token")


# ---- ledger ----------------------------------------------------------------------------------

_ledger_lock = asyncio.Lock()


async def _append(uid: str, rows: list[dict]) -> None:
    if not rows:
        return
    async with _ledger_lock:
        p = tenant(uid) / "ledger.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write("".join(json.dumps(r, default=str) + "\n" for r in rows))


# ---- app -------------------------------------------------------------------------------------

app = FastAPI(title="serving box")


@app.get("/health")
def health():
    return {"ok": True, "mock": mock()}


class VersionPush(BaseModel):
    version: dict[str, Any]                       # id, project_id, label, spec, fingerprint
    steps: dict[str, dict[str, Any]] = {}         # step id -> {"chunks": [...], "vectors": {model: [[...], ...]}}


@app.put("/internal/tenants/{uid}/versions/{vid}", dependencies=[Depends(internal)])
def put_version(uid: str, vid: str, body: VersionPush):
    import numpy as np
    from .steps import load_manifest
    spec = ProjectSpec.model_validate(body.version["spec"])      # refuse anything that would not run
    d = tenant(uid) / "versions" / _id(vid, "version id")
    for st in spec.steps:
        m = load_manifest(st.manifest) if st.enabled else None
        if m and m.transport.kind == "local" and st.id not in body.steps:
            raise HTTPException(400, f"step {st.id!r} searches documents, and none were sent")
    for sid, data in body.steps.items():
        _write(d / "steps" / f"{_id(sid, 'step id')}.json", json.dumps(data["chunks"]))
        for model, rows in (data.get("vectors") or {}).items():
            arr = np.asarray(rows, dtype=np.float32)
            if len(arr) != len(data["chunks"]):
                raise HTTPException(400, f"step {sid!r}: {len(arr)} vectors for {len(data['chunks'])} passages")
            buf = __import__("io").BytesIO()
            np.save(buf, arr, allow_pickle=False)
            _write(d / "steps" / f"{sid}.{_slug(model)}.npy", buf.getvalue())
    _write(d / "version.json", json.dumps({**body.version, "user_id": uid}))   # last: the version exists once complete
    return {"ok": True, "version_id": vid}


@app.delete("/internal/tenants/{uid}/versions/{vid}", dependencies=[Depends(internal)])
def delete_version(uid: str, vid: str):
    import shutil
    shutil.rmtree(tenant(uid) / "versions" / _id(vid, "version id"), ignore_errors=True)
    return {"ok": True}


@app.delete("/internal/tenants/{uid}", dependencies=[Depends(internal)])
async def delete_tenant(uid: str):
    """Everything a customer left here: their folder, and every key hash that names them."""
    import shutil
    shutil.rmtree(tenant(uid), ignore_errors=True)
    async with _keys_lock:
        _write(root() / "keys.json", json.dumps({h: r for h, r in _keys().items() if r["user_id"] != uid}))
    return {"ok": True}


class KeyPush(BaseModel):
    hash: str
    user_id: str
    tier: str | None = None


@app.put("/internal/keys/{kid}", dependencies=[Depends(internal)])
async def put_key(kid: str, body: KeyPush):
    async with _keys_lock:
        keys = {h: r for h, r in _keys().items() if r["key_id"] != kid}
        keys[body.hash] = {"key_id": _id(kid, "key id"), "user_id": _id(body.user_id, "user id"), "tier": body.tier}
        _write(root() / "keys.json", json.dumps(keys))
    return {"ok": True}


@app.delete("/internal/keys/{kid}", dependencies=[Depends(internal)])
async def delete_key(kid: str):
    async with _keys_lock:
        _write(root() / "keys.json", json.dumps({h: r for h, r in _keys().items() if r["key_id"] != kid}))
    return {"ok": True}


@app.get("/internal/tenants/{uid}/ledger", dependencies=[Depends(internal)])
def ledger(uid: str, after: int = 0, limit: int = 1000):
    """Lines `after`.. of the customer's ledger; `next` is where to ask from next time. Append-only, so a line
    number is a stable cursor."""
    p = tenant(uid) / "ledger.jsonl"
    if not p.exists():
        return {"rows": [], "next": after}
    with open(p) as f:
        lines = f.readlines()
    rows = [json.loads(l) for l in lines[after:after + limit]]
    return {"rows": rows, "next": after + len(rows)}


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", model)


def _load(uid: str, vid: str) -> dict:
    p = tenant(uid) / "versions" / _id(vid, "version id") / "version.json"
    if not p.exists():
        raise HTTPException(404, "version not found")   # includes another customer's version: never say which
    return json.loads(p.read_text())


def _access(rec: dict) -> Access:
    t = tiers.tier_of(rec.get("tier"))
    return Access([], house_keys=t["house_keys"], house_models=t["house_models"], max_concurrency=t["max_concurrency"],
                  max_q=t["max_q"], tier=rec.get("tier") or tiers.DEFAULT_TIER, user_id=rec["user_id"])


def _locals(uid: str, vid: str, spec: ProjectSpec, on_call) -> dict[str, Any]:
    """A search step's function, over the passages and vectors shipped with this version."""
    import numpy as np
    from . import corpora as K, embedding as E
    from .steps import load_manifest
    out: dict[str, Any] = {}
    for st in spec.steps:
        m = load_manifest(st.manifest) if st.enabled else None
        if not m or m.transport.kind != "local":
            continue
        emb = E.make_embedder(mock=mock(), purpose=f"step:{st.id}", on_call=on_call)
        d = tenant(uid) / "versions" / vid / "steps"
        vec = d / f"{st.id}.{_slug(emb.model)}.npy"
        if not vec.exists() and mock():
            # the offline hash vectors are never shipped (free to recompute), exactly as bundles do
            chunks = json.loads((d / f"{st.id}.json").read_text())
            out[st.id] = _hash_retriever(chunks, emb, st.tunables.get("k") or K.DEFAULT_K)
            continue
        if not vec.exists():
            raise HTTPException(502, f"step {st.id!r}: this version was hosted without {emb.model} vectors")
        chunks = json.loads((d / f"{st.id}.json").read_text())
        out[st.id] = K.retriever_over(chunks, np.load(vec, allow_pickle=False), emb, st.tunables.get("k") or K.DEFAULT_K)
    return out


def _hash_retriever(chunks, emb, k):
    """Mock only: embed the passages with the offline embedder on first use, then search as usual."""
    from . import corpora as K
    state: dict[str, Any] = {}

    async def retrieve(inputs):
        if "fn" not in state:
            vecs = await emb.embed([K.embed_text(c) for c in chunks])
            state["fn"] = K.retriever_over(chunks, vecs, emb, k)
        return await state["fn"](inputs)
    return retrieve


class ServeBody(BaseModel):
    inputs: dict[str, Any] = {}


@app.get("/v/{vid}")
def contract(vid: str, rec: dict = Depends(key_owner)):
    v = _load(rec["user_id"], vid)
    spec = ProjectSpec.model_validate(v["spec"])
    terminal = next((m for m in spec.modules if not spec.outgoing(m.id)), spec.modules[0] if spec.modules else None)
    return {"version_id": vid, "label": v.get("label"), "inputs": serving.required_inputs(spec), "optional": serving.optional_inputs(spec),
            "outputs": [f.name for f in terminal.schema_fields] if terminal else [], "eval_model": spec.eval_model}


@app.post("/v/{vid}/run")
async def run(vid: str, body: ServeBody, rec: dict = Depends(key_owner)):
    """The studio's `/v/{vid}/run`, answered here: same `serving.serve`, same response shape."""
    uid = rec["user_id"]
    v = _load(uid, vid)
    spec = ProjectSpec.model_validate(v["spec"])
    access = _access(rec)
    usage: list[dict] = []

    def on_call(r: dict) -> None:
        usage.append({"kind": "usage", "ts": time.time(), "user_id": uid, "tier": access.tier, "project_id": v.get("project_id"),
                      "purpose": r["purpose"], "model": r["model"], "source": r["source"], "input_tokens": r["input_tokens"],
                      "output_tokens": r["output_tokens"], "cached": r["cached"], "latency_s": r["latency_s"], "usd": r["usd"]})

    tid = uuid.uuid4().hex
    base = {"kind": "trace", "id": tid, "version_id": vid, "project_id": v.get("project_id"), "user_id": uid,
            "inputs": body.inputs, "created": time.time()}
    try:
        locals_ = _locals(uid, vid, spec, on_call)
        out = await serving.serve(spec, body.inputs, access=access, on_call=on_call, locals=locals_,
                                  mock=__import__("app.mock", fromlist=["mock_client"]).mock_client() if mock() else None)
    except serving.MissingInputs as e:
        raise HTTPException(400, str(e))
    except HTTPException as e:
        await _append(uid, [{**base, "error": str(e.detail)}, *usage])
        raise HTTPException(502, {"message": f"this version cannot run: {e.detail}", "trace_id": tid})
    except Exception as e:
        await _append(uid, [{**base, "error": f"{type(e).__name__}: {e}"}, *usage])
        raise HTTPException(502, {"message": f"the version failed on this input: {type(e).__name__}: {e}", "trace_id": tid})
    await _append(uid, [{**base, "output": out["output"], "parsed": out["parsed"], "path": out["path"], "metrics": out["metrics"],
                         "input_tokens": out["input_tokens"], "output_tokens": out["output_tokens"], "usd": out["usd"],
                         "latency_s": out["latency_s"], "steps": out.get("steps")}, *usage])
    return {"trace_id": tid, "version_id": vid, "output": out["output"], "parsed": out["parsed"], "path": out["path"],
            "steps": out.get("steps") or None,
            "usage": {"input_tokens": out["input_tokens"], "output_tokens": out["output_tokens"], "usd": out["usd"],
                      "latency_s": out["latency_s"]}}
