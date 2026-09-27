"""Hosting (the beta upsell): the studio's half of the serving box (`app/serve_box.py`).

Hosting a version copies it - pinned spec, plus the passages and vectors of any search step - into the customer's
folder on the serving box. Versions never change, so a copy is safe and the box never reads the studio's database.
API key hashes are mirrored on create and revoke. The box writes each served request and each model call to the
customer's ledger; `pull` brings those back into `traces` and `usage_log`, so traces, the usage screen and billing
(AWS cost passed through, plus a surcharge) read one ledger.

Configured by SERVE_BOX_URL (the box's private address) and SERVE_TOKEN (the shared secret, in both .env files);
without them nothing here runs and serving stays in-process. Hosted versions run on house models only.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from . import brand, db, store

_transport: httpx.AsyncBaseTransport | None = None   # tests route the box in-process

SCHEMA = """
create table if not exists hosted_versions (version_id text primary key, user_id text not null, hosted real not null);
create index if not exists hosted_user on hosted_versions (user_id);
create table if not exists hosting_ledger (user_id text primary key, next integer not null);
"""


class HostingError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("SERVE_BOX_URL") and os.environ.get("SERVE_TOKEN"))


def init(con) -> None:
    con.executescript(SCHEMA)
    con.commit()


def public_url(vid: str) -> str:
    """nginx sends /serve/ to the serving box; /api/v/ stays the studio's in-process path."""
    return f"{brand.public_url()}/serve/v/{vid}/run"


def _box() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=os.environ["SERVE_BOX_URL"], headers={"x-serve-token": os.environ["SERVE_TOKEN"]},
                             timeout=60.0, transport=_transport)


async def _call(method: str, path: str, **kw) -> dict:
    async with _box() as c:
        r = await c.request(method, path, **kw)
    if r.status_code >= 400:
        raise HostingError(f"serving box answered {r.status_code}: {r.text[:300]}")
    return r.json()


def is_hosted(con, vid: str) -> bool:
    return con.execute("select 1 from hosted_versions where version_id=?", (vid,)).fetchone() is not None


def hosted_ids(con, user_id: str) -> set[str]:
    return {r[0] for r in con.execute("select version_id from hosted_versions where user_id=?", (user_id,))}


def _bundle(con, v: dict, user_id: str) -> dict[str, Any]:
    """What the box needs to answer for `v`: the version, and each search step's passages with their paid-for
    vectors. The offline hash vectors are left out, as bundles leave them out: free to recompute."""
    from . import embedding as E
    from .models import ProjectSpec
    from .steps import load_manifest
    spec = ProjectSpec.model_validate(v["spec"])
    steps: dict[str, dict] = {}
    for st in spec.steps:
        m = load_manifest(st.manifest) if st.enabled else None
        if m is None:
            continue
        if m.transport.kind != "local":
            if m.auth:
                raise HostingError(f"step {st.id!r} signs in to another service; hosted versions cannot carry step keys or connections yet")
            continue
        cid, docs = st.tunables.get("corpus"), st.tunables.get("documents")
        if not cid:
            raise HostingError(f"step {st.id!r} has no document set")
        chunks, mat = store.load_vectors(con, cid, user_id, E.TITAN, docs)
        if not chunks:
            chunks, mat = store.corpus_chunks(con, cid, user_id, docs), None
        keep = ("document", "heading", "text", "tokens")
        steps[st.id] = {"chunks": [{k: c[k] for k in keep} for c in chunks],
                        "vectors": {E.TITAN: mat.tolist()} if mat is not None and len(mat) else {}}
    return {"version": {k: v[k] for k in ("id", "project_id", "label", "fingerprint")} | {"spec": v["spec"]}, "steps": steps}


async def push_keys(con, user_id: str) -> None:
    tier = (con.execute("select tier from users where id=?", (user_id,)).fetchone() or [None])[0]
    for kid, h in con.execute("select id, hash from api_keys where user_id=?", (user_id,)).fetchall():
        await _call("PUT", f"/internal/keys/{kid}", json={"hash": h, "user_id": user_id, "tier": tier})


async def drop_key(kid: str) -> None:
    await _call("DELETE", f"/internal/keys/{kid}")


async def host(con, v: dict, user_id: str) -> dict:
    await _call("PUT", f"/internal/tenants/{user_id}/versions/{v['id']}", json=_bundle(con, v, user_id))
    await push_keys(con, user_id)
    con.execute("insert or replace into hosted_versions values (?,?,?)", (v["id"], user_id, db.now()))
    con.execute("insert or ignore into hosting_ledger values (?, 0)", (user_id,))
    con.commit()
    return {"version_id": v["id"], "hosted": True, "url": public_url(v["id"])}


async def unhost(con, vid: str, user_id: str) -> dict:
    await _call("DELETE", f"/internal/tenants/{user_id}/versions/{vid}")
    con.execute("delete from hosted_versions where version_id=? and user_id=?", (vid, user_id))
    con.commit()
    return {"version_id": vid, "hosted": False}


async def delete_tenant(con, user_id: str) -> None:
    """A customer leaving: their folder and key hashes on the box. Their studio rows are the account deletion's job."""
    await _call("DELETE", f"/internal/tenants/{user_id}")
    con.execute("delete from hosted_versions where user_id=?", (user_id,))
    con.commit()


async def pull(con, user_id: str | None = None) -> int:
    """New ledger lines from the box -> traces and usage_log. The inserts and the cursor move commit together, so a
    crash in between re-pulls rather than loses or doubles a line. Returns lines imported."""
    users = [user_id] if user_id else [r[0] for r in con.execute("select user_id from hosting_ledger")]
    n = 0
    for uid in users:
        row = con.execute("select next from hosting_ledger where user_id=?", (uid,)).fetchone()
        if row is None:
            continue
        got = await _call("GET", f"/internal/tenants/{uid}/ledger", params={"after": row[0]})
        for r in got["rows"]:
            if r.get("user_id") != uid:
                continue   # the box keys lines by folder; a line naming someone else is not imported anywhere
            if r["kind"] == "trace":
                con.execute("insert or ignore into traces (id, version_id, project_id, user_id, inputs, output, parsed, path, metrics,"
                            " input_tokens, output_tokens, usd, latency_s, error, created, steps) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r["id"], r["version_id"], r.get("project_id") or "", uid, json.dumps(r.get("inputs") or {}, default=str),
                             r.get("output"), json.dumps(r.get("parsed"), default=str), json.dumps(r.get("path") or []),
                             json.dumps(r.get("metrics") or {}), r.get("input_tokens"), r.get("output_tokens"), r.get("usd"),
                             r.get("latency_s"), r.get("error"), r["created"], json.dumps(r.get("steps") or {}, default=str)))
            elif r["kind"] == "usage":
                con.execute("insert into usage_log (ts, user_id, tier, project_id, run_id, purpose, model, source, input_tokens,"
                            " output_tokens, cached, latency_s, usd) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r["ts"], uid, r.get("tier"), r.get("project_id"), None, r["purpose"], r["model"], r["source"],
                             r["input_tokens"], r["output_tokens"], r["cached"], r["latency_s"], r["usd"]))
        con.execute("update hosting_ledger set next=? where user_id=?", (got["next"], uid))
        con.commit()
        n += len(got["rows"])
    return n
