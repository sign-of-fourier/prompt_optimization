"""The storage seam (PLAN.md, Piece 7). Every table v0 adds - `versions` now, traces and outcomes next - is created
and queried *here and nowhere else*: feature code calls functions, never SQL. sqlite and the studio's single
connection today; this module is the unit that gets replaced when the second tenant forces Postgres, and the
signatures below are what that replacement has to keep.

Nothing here updates a version. Versions are immutable by construction: there is no update statement to call.
"""
from __future__ import annotations

import json
from typing import Any

from . import db

SCHEMA = """
create table if not exists versions (id text primary key, project_id text not null, user_id text not null, label text not null,
    spec text not null, fingerprint text not null, source text not null, dataset_id text, score real, metrics text,
    n_rows integer, holdout text, created real not null);
create index if not exists versions_project on versions (project_id, created);
create table if not exists api_keys (id text primary key, user_id text not null, label text not null, hash text not null,
    prefix text not null, created real not null, last_used real);
create index if not exists api_keys_hash on api_keys (hash);
create table if not exists traces (id text primary key, version_id text not null, project_id text not null, user_id text not null,
    inputs text not null, output text, parsed text, path text, metrics text, input_tokens integer, output_tokens integer,
    usd real, latency_s real, error text, created real not null);
create index if not exists traces_version on traces (version_id, created);
create table if not exists outcomes (id text primary key, trace_id text not null, project_id text not null, user_id text not null,
    kind text not null, label text, value real, source text not null, note text, created real not null);
create index if not exists outcomes_trace on outcomes (trace_id, created);
"""
JSON_COLS = {"spec", "source", "metrics", "holdout", "inputs", "parsed", "path"}


def init(con) -> None:
    con.executescript(SCHEMA)
    con.commit()


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & set(d):
        if isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d


# ---- versions --------------------------------------------------------------------------

def create_version(con, *, project_id: str, user_id: str, label: str, spec: dict, fingerprint: str, source: dict,
                   dataset_id: str | None = None, score: float | None = None, metrics: dict | None = None,
                   n_rows: int | None = None, holdout: dict | None = None) -> dict:
    vid = db.new_id()
    con.execute("insert into versions values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, project_id, user_id, label, json.dumps(spec), fingerprint, json.dumps(source), dataset_id, score,
                 json.dumps(metrics or {}), n_rows, json.dumps(holdout) if holdout else None, db.now()))
    con.commit()
    return get_version(con, vid)


def get_version(con, vid: str, user_id: str | None = None) -> dict | None:
    q = "select * from versions where id=?" + (" and user_id=?" if user_id else "")
    return _row(con.execute(q, (vid, user_id) if user_id else (vid,)).fetchone())


def list_versions(con, project_id: str, user_id: str) -> list[dict[str, Any]]:
    """The listing omits `spec`: it is the heavy column and only the immutable fetch needs it."""
    rs = con.execute("select id, project_id, label, fingerprint, source, dataset_id, score, metrics, n_rows, holdout, created"
                     " from versions where project_id=? and user_id=? order by created desc", (project_id, user_id))
    return [_row(r) for r in rs]


def latest_version(con, project_id: str, user_id: str) -> dict | None:
    return _row(con.execute("select * from versions where project_id=? and user_id=? order by created desc limit 1",
                            (project_id, user_id)).fetchone())


def delete_project_rows(con, project_id: str) -> None:
    """Called when a project is deleted. A version outliving its project would point at nothing."""
    con.execute("delete from versions where project_id=?", (project_id,))
    con.execute("delete from traces where project_id=?", (project_id,))
    con.execute("delete from outcomes where project_id=?", (project_id,))
    con.commit()


# ---- api keys --------------------------------------------------------------------------
# The serving endpoint is called by machines, not browsers: it authenticates with a key, never the session cookie.
# Only the sha256 is stored, so a lost key is reissued, never recovered.

def create_api_key(con, *, user_id: str, label: str, hash: str, prefix: str) -> dict:
    kid = db.new_id()
    con.execute("insert into api_keys values (?,?,?,?,?,?,?)", (kid, user_id, label, hash, prefix, db.now(), None))
    con.commit()
    return {"id": kid, "label": label, "prefix": prefix, "created": db.now()}


def list_api_keys(con, user_id: str) -> list[dict]:
    return [dict(r) for r in con.execute("select id, label, prefix, created, last_used from api_keys where user_id=? order by created desc", (user_id,))]


def api_key_owner(con, hash: str) -> str | None:
    r = con.execute("select id, user_id from api_keys where hash=?", (hash,)).fetchone()
    if not r:
        return None
    con.execute("update api_keys set last_used=? where id=?", (db.now(), r["id"]))
    con.commit()
    return r["user_id"]


def delete_api_key(con, user_id: str, kid: str) -> None:
    con.execute("delete from api_keys where id=? and user_id=?", (kid, user_id))
    con.commit()


# ---- traces ----------------------------------------------------------------------------
# Piece 2 writes them; Piece 3 adds outcomes against `id` and promotion to a dataset.

def create_trace(con, *, version_id: str, project_id: str, user_id: str, inputs: dict, output: str | None = None,
                 parsed: Any = None, path: list[str] | None = None, metrics: dict | None = None,
                 input_tokens: int | None = None, output_tokens: int | None = None, usd: float | None = None,
                 latency_s: float | None = None, error: str | None = None) -> str:
    tid = db.new_id()
    con.execute("insert into traces values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, version_id, project_id, user_id, json.dumps(inputs, default=str), output, json.dumps(parsed, default=str),
                 json.dumps(path or []), json.dumps(metrics or {}), input_tokens, output_tokens, usd, latency_s, error, db.now()))
    con.commit()
    return tid


def get_trace(con, tid: str, user_id: str | None = None) -> dict | None:
    q = "select * from traces where id=?" + (" and user_id=?" if user_id else "")
    return _row(con.execute(q, (tid, user_id) if user_id else (tid,)).fetchone())


def list_traces(con, project_id: str, user_id: str, limit: int = 100) -> list[dict]:
    return [_row(r) for r in con.execute("select * from traces where project_id=? and user_id=? order by created desc limit ?",
                                         (project_id, user_id, limit))]


# ---- outcomes --------------------------------------------------------------------------
# What happened after the answer: a correction, a reopen, a rating, a closed deal. Posted against a trace id, by the
# system that learned it - which is usually not the one that made the request, and often much later.

def create_outcome(con, *, trace_id: str, project_id: str, user_id: str, kind: str, label: str | None = None,
                   value: float | None = None, source: str = "api", note: str = "") -> dict:
    oid = db.new_id()
    con.execute("insert into outcomes values (?,?,?,?,?,?,?,?,?,?)",
                (oid, trace_id, project_id, user_id, kind, label, value, source, note, db.now()))
    con.commit()
    return {"id": oid, "trace_id": trace_id, "kind": kind, "label": label, "value": value, "source": source, "note": note, "created": db.now()}


def list_outcomes(con, trace_ids: list[str]) -> dict[str, list[dict]]:
    """trace id -> its outcomes, oldest first."""
    if not trace_ids:
        return {}
    qs = ",".join("?" * len(trace_ids))
    out: dict[str, list[dict]] = {}
    for r in con.execute(f"select * from outcomes where trace_id in ({qs}) order by created", trace_ids):
        out.setdefault(r["trace_id"], []).append(dict(r))
    return out


def labelled_traces(con, project_id: str, user_id: str, *, version_id: str | None = None, kind: str = "correction") -> list[dict]:
    """Traces that carry an outcome of `kind`, newest outcome last so the caller can take the final word. A trace with
    no outcome is not a label and is not returned."""
    q = ("select t.*, o.label as o_label, o.value as o_value, o.kind as o_kind, o.created as o_created, o.source as o_source"
         " from traces t join outcomes o on o.trace_id=t.id where t.project_id=? and t.user_id=? and o.kind=?"
         + (" and t.version_id=?" if version_id else "") + " order by o.created")
    args = [project_id, user_id, kind] + ([version_id] if version_id else [])
    return [_row(r) for r in con.execute(q, args)]
