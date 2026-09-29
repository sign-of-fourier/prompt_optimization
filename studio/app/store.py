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
create table if not exists step_credentials (id text primary key, user_id text not null, label text not null,
    secret text not null, created real not null);
create table if not exists connections (id text primary key, user_id text not null, provider text not null,
    account text, label text not null, tokens text not null, expires real, scopes text, created real not null, refreshed real);
create index if not exists connections_user on connections (user_id, provider);
create table if not exists oauth_states (state text primary key, user_id text not null, provider text not null,
    redirect text, created real not null);
create table if not exists corpora (id text primary key, user_id text not null, name text not null, created real not null,
    updated real not null);
create index if not exists corpora_user on corpora (user_id, created);
create table if not exists corpus_documents (id text primary key, corpus_id text not null, user_id text not null, name text not null,
    sha256 text not null, bytes integer not null, n_chunks integer not null, chunker text not null, blob text not null,
    created real not null);
create index if not exists corpus_documents_corpus on corpus_documents (corpus_id, created);
create table if not exists corpus_chunks (id text primary key, document_id text not null, corpus_id text not null, ord integer not null,
    heading text not null, text text not null, tokens integer not null);
create index if not exists corpus_chunks_document on corpus_chunks (document_id, ord);
create index if not exists corpus_chunks_corpus on corpus_chunks (corpus_id);
create table if not exists corpus_vectors (document_id text not null, corpus_id text not null, model text not null, dims integer not null,
    blob text not null, created real not null, primary key (document_id, model));
"""
# `create table if not exists` cannot add a column to a table that already exists on a running box.
MIGRATIONS = ["alter table traces add column steps text",
              # a replaced document a published version still searches: kept for that version, gone from the set
              "alter table corpus_documents add column retired real",
              # how the trace came to be labelled: random (the review queue's sample, the only estimate of live
              # accuracy), suspicious (errors and capped loops first), picked (opened by hand), api; null before this
              "alter table outcomes add column chosen text"]
JSON_COLS = {"spec", "source", "metrics", "holdout", "inputs", "parsed", "path", "steps"}


def init(con) -> None:
    con.executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            con.execute(m)
        except Exception:
            pass  # already applied
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
                 latency_s: float | None = None, error: str | None = None, steps: dict | None = None) -> str:
    """`steps` is what each external step returned for this request. Production calls those live, so without it the
    answer cannot be explained afterwards: the data that produced it has moved on."""
    tid = db.new_id()
    con.execute("insert into traces (id, version_id, project_id, user_id, inputs, output, parsed, path, metrics,"
                " input_tokens, output_tokens, usd, latency_s, error, created, steps) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, version_id, project_id, user_id, json.dumps(inputs, default=str), output, json.dumps(parsed, default=str),
                 json.dumps(path or []), json.dumps(metrics or {}), input_tokens, output_tokens, usd, latency_s, error, db.now(),
                 json.dumps(steps or {}, default=str)))
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
                   value: float | None = None, source: str = "api", note: str = "", chosen: str | None = None) -> dict:
    oid = db.new_id()
    con.execute("insert into outcomes (id, trace_id, project_id, user_id, kind, label, value, source, note, created, chosen)"
                " values (?,?,?,?,?,?,?,?,?,?,?)", (oid, trace_id, project_id, user_id, kind, label, value, source, note, db.now(), chosen))
    con.commit()
    return {"id": oid, "trace_id": trace_id, "kind": kind, "label": label, "value": value, "source": source, "note": note,
            "chosen": chosen, "created": db.now()}


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


# ---- step credentials ------------------------------------------------------------------------
# A step credential is a label and a secret - a different shape from a model credential (provider, config, model
# list), so it gets its own table rather than bending `credentials.py`. The Fernet key is that module's.

def create_step_credential(con, *, user_id: str, label: str, secret: str) -> dict:
    cid = db.new_id()
    con.execute("insert into step_credentials values (?,?,?,?,?)", (cid, user_id, label, secret, db.now()))
    con.commit()
    return {"id": cid, "label": label, "created": db.now()}


def list_step_credentials(con, user_id: str) -> list[dict]:
    return [{"id": r["id"], "label": r["label"], "created": r["created"]}
            for r in con.execute("select id, label, created from step_credentials where user_id=? order by created", (user_id,))]


def step_secret(con, user_id: str, cid: str) -> str | None:
    r = con.execute("select secret from step_credentials where id=? and user_id=?", (cid, user_id)).fetchone()
    return r["secret"] if r else None


def delete_step_credential(con, user_id: str, cid: str) -> None:
    con.execute("delete from step_credentials where id=? and user_id=?", (cid, user_id))
    con.commit()


# ---- oauth connections -------------------------------------------------------------------
# A connection is one authorization a user granted us against one of their accounts elsewhere. Tokens are stored
# encrypted by the caller (`credentials.encrypt`) - this layer never sees them in the clear and never logs them.

def create_connection(con, *, user_id: str, provider: str, account: str | None, label: str, tokens: str,
                      expires: float | None, scopes: str = "") -> dict:
    """One connection per (user, provider, account): re-authorizing replaces the tokens rather than piling up rows."""
    row = con.execute("select id from connections where user_id=? and provider=? and coalesce(account,'')=?",
                      (user_id, provider, account or "")).fetchone()
    cid = row["id"] if row else db.new_id()
    if row:
        con.execute("update connections set label=?, tokens=?, expires=?, scopes=?, refreshed=? where id=?",
                    (label, tokens, expires, scopes, db.now(), cid))
    else:
        con.execute("insert into connections values (?,?,?,?,?,?,?,?,?,?)",
                    (cid, user_id, provider, account, label, tokens, expires, scopes, db.now(), None))
    con.commit()
    return get_connection(con, cid, user_id)


def get_connection(con, cid: str, user_id: str | None = None) -> dict | None:
    q = "select * from connections where id=?" + (" and user_id=?" if user_id else "")
    return _row(con.execute(q, (cid, user_id) if user_id else (cid,)).fetchone())


def list_connections(con, user_id: str) -> list[dict]:
    """Never returns `tokens`: nothing outside the oauth module has any business holding them."""
    return [dict(r) for r in con.execute(
        "select id, provider, account, label, expires, scopes, created, refreshed from connections where user_id=? order by created", (user_id,))]


def update_connection_tokens(con, cid: str, tokens: str, expires: float | None) -> None:
    con.execute("update connections set tokens=?, expires=?, refreshed=? where id=?", (tokens, expires, db.now(), cid))
    con.commit()


def delete_connection(con, user_id: str, cid: str) -> None:
    con.execute("delete from connections where id=? and user_id=?", (cid, user_id))
    con.commit()


# ---- oauth state -------------------------------------------------------------------------
# The CSRF defence for the redirect: a single-use, short-lived token bound to the session that started the flow.
# Server-side rather than a signed cookie, so it can be *consumed* - a replayed callback finds nothing.

STATE_TTL = 600.0


def put_state(con, state: str, user_id: str, provider: str, redirect: str = "") -> None:
    con.execute("delete from oauth_states where created < ?", (db.now() - STATE_TTL,))
    con.execute("insert into oauth_states values (?,?,?,?,?)", (state, user_id, provider, redirect, db.now()))
    con.commit()


def take_state(con, state: str) -> dict | None:
    """Reads and deletes in one go: a state is good for exactly one callback."""
    r = con.execute("select * from oauth_states where state=?", (state,)).fetchone()
    con.execute("delete from oauth_states where state=?", (state,))
    con.commit()
    if not r or db.now() - r["created"] > STATE_TTL:
        return None
    return dict(r)


# ---- corpora (retrieval documents) -------------------------------------------------------
# A corpus is a named, user-owned set of documents; a document never changes once added (re-uploading a changed file
# adds a new document and removes the old one), so a version can pin a corpus by the document ids it held. Chunk text
# lives here because the document browser and retrieval both read it; the original file is a blob addressed by
# `blob_path`, never by a path a caller builds. Vectors are one float32 blob per (document, embed model), rows in
# chunk order, so mock and live vectors of the same document coexist.

BLOBS = db.DATA_DIR / "blobs"


def blob_path(key: str):
    """The one place a blob key becomes a file. S3 replaces this function, not its callers."""
    if not key or ".." in key or key.startswith("/"):
        raise ValueError(f"bad blob key {key!r}")
    p = BLOBS / key
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def create_corpus(con, *, user_id: str, name: str) -> dict:
    cid = db.new_id()
    con.execute("insert into corpora values (?,?,?,?,?)", (cid, user_id, name, db.now(), db.now()))
    con.commit()
    return get_corpus(con, cid, user_id)


def get_corpus(con, cid: str, user_id: str) -> dict | None:
    r = con.execute("select c.*, count(d.id) n_documents, coalesce(sum(d.n_chunks),0) n_chunks, coalesce(sum(d.bytes),0) bytes"
                    " from corpora c left join corpus_documents d on d.corpus_id=c.id and d.retired is null where c.id=? and c.user_id=? group by c.id",
                    (cid, user_id)).fetchone()
    return dict(r) if r else None


def list_corpora(con, user_id: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "select c.*, count(d.id) n_documents, coalesce(sum(d.n_chunks),0) n_chunks, coalesce(sum(d.bytes),0) bytes"
        " from corpora c left join corpus_documents d on d.corpus_id=c.id and d.retired is null where c.user_id=? group by c.id"
        " order by c.created desc", (user_id,))]


def delete_corpus(con, cid: str, user_id: str) -> None:
    for d in list_documents(con, cid, user_id, retired=True):
        delete_document(con, d["id"], user_id, commit=False)
    con.execute("delete from corpora where id=? and user_id=?", (cid, user_id))
    con.commit()


def add_document(con, *, corpus_id: str, user_id: str, name: str, sha256: str, data: bytes, ext: str, chunker: str,
                 chunks: list[dict]) -> dict:
    """`chunks`: dicts with heading, text, tokens, in reading order. The caller owns the corpus check."""
    did = db.new_id()
    key = f"corpora/{corpus_id}/{did}{ext}"
    blob_path(key).write_bytes(data)
    con.execute("insert into corpus_documents (id, corpus_id, user_id, name, sha256, bytes, n_chunks, chunker, blob, created)"
                " values (?,?,?,?,?,?,?,?,?,?)", (did, corpus_id, user_id, name, sha256, len(data), len(chunks), chunker, key, db.now()))
    con.executemany("insert into corpus_chunks values (?,?,?,?,?,?,?)",
                    [(f"{did}-{i}", did, corpus_id, i, c["heading"], c["text"], c["tokens"]) for i, c in enumerate(chunks)])
    con.execute("update corpora set updated=? where id=?", (db.now(), corpus_id))
    con.commit()
    return get_document(con, did, user_id)


def get_document(con, did: str, user_id: str) -> dict | None:
    r = con.execute("select * from corpus_documents where id=? and user_id=?", (did, user_id)).fetchone()
    return dict(r) if r else None


def list_documents(con, corpus_id: str, user_id: str, *, retired: bool = False) -> list[dict]:
    """The set as it stands; `retired=True` adds replaced documents that versions still search."""
    return [dict(r) for r in con.execute("select * from corpus_documents where corpus_id=? and user_id=?"
                                         + ("" if retired else " and retired is null") + " order by name", (corpus_id, user_id))]


def retire_document(con, did: str, user_id: str) -> None:
    con.execute("update corpus_documents set retired=? where id=? and user_id=?", (db.now(), did, user_id))
    con.commit()


def pinned_documents(con, user_id: str) -> dict[str, list[str]]:
    """document id -> the versions whose retrieval steps search it (their spec's `tunables.documents`)."""
    out: dict[str, list[str]] = {}
    for r in con.execute("select id, spec from versions where user_id=?", (user_id,)):
        for st in json.loads(r["spec"]).get("steps") or []:
            for did in (st.get("tunables") or {}).get("documents") or []:
                out.setdefault(did, []).append(r["id"])
    return out


def document_chunks(con, did: str, user_id: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "select c.id, c.ord, c.heading, c.text, c.tokens from corpus_chunks c join corpus_documents d on d.id=c.document_id"
        " where c.document_id=? and d.user_id=? order by c.ord", (did, user_id))]


def corpus_chunks(con, corpus_id: str, user_id: str, document_ids: list[str] | None = None) -> list[dict]:
    """Every chunk in the set with its document name: what retrieval searches. `document_ids` (a version's pin)
    selects exactly those documents, retired ones included; None means the set as it stands."""
    q = ("select c.id, c.document_id, d.name document, c.ord, c.heading, c.text, c.tokens from corpus_chunks c"
         " join corpus_documents d on d.id=c.document_id where c.corpus_id=? and d.user_id=?")
    args: list = [corpus_id, user_id]
    if document_ids is None:
        q += " and d.retired is null"
    else:
        q += f" and d.id in ({','.join('?' * len(document_ids))})"
        args += list(document_ids)
    return [dict(r) for r in con.execute(q + " order by d.name, c.ord", args)]


def delete_document(con, did: str, user_id: str, commit: bool = True) -> None:
    """Deletes the text and the file. Once versions pin documents, a pinned one will need a rule here (keep until the
    last version pinning it goes, or refuse); nothing pins yet."""
    d = get_document(con, did, user_id)
    if not d:
        return
    blob_path(d["blob"]).unlink(missing_ok=True)
    for (key,) in con.execute("select blob from corpus_vectors where document_id=?", (did,)).fetchall():
        blob_path(key).unlink(missing_ok=True)
    con.execute("delete from corpus_vectors where document_id=?", (did,))
    con.execute("delete from corpus_chunks where document_id=?", (did,))
    con.execute("delete from corpus_documents where id=?", (did,))
    con.execute("update corpora set updated=? where id=?", (db.now(), d["corpus_id"]))
    if commit:
        con.commit()


def _model_slug(model: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in model)


def put_vectors(con, *, document_id: str, corpus_id: str, model: str, vectors) -> None:
    """`vectors`: a float32 array, one row per chunk in `ord` order."""
    import numpy as np
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim != 2 or not len(arr):
        raise ValueError("a document needs at least one embedded chunk")
    key = f"corpora/{corpus_id}/{document_id}.{_model_slug(model)}.npy"
    with open(blob_path(key), "wb") as f:
        np.save(f, arr, allow_pickle=False)
    con.execute("insert or replace into corpus_vectors values (?,?,?,?,?,?)", (document_id, corpus_id, model, arr.shape[1], key, db.now()))
    con.commit()


def vector_documents(con, corpus_id: str, model: str) -> set[str]:
    """Documents in the corpus that have vectors for `model`."""
    return {r[0] for r in con.execute("select document_id from corpus_vectors where corpus_id=? and model=?", (corpus_id, model))}


def load_vectors(con, corpus_id: str, user_id: str, model: str, document_ids: list[str] | None = None):
    """-> (chunks, matrix): every chunk of every selected document (see `corpus_chunks`) embedded with `model`, rows
    aligned. Documents without vectors for `model` are left out; the caller decides whether that is acceptable."""
    import numpy as np
    chunks, mats = [], []
    by_doc: dict[str, list[dict]] = {}
    for c in corpus_chunks(con, corpus_id, user_id, document_ids):
        by_doc.setdefault(c["document_id"], []).append(c)
    for r in con.execute("select v.document_id, v.blob from corpus_vectors v join corpus_documents d on d.id=v.document_id"
                         " where v.corpus_id=? and v.model=? and d.user_id=? order by d.name", (corpus_id, model, user_id)):
        if r["document_id"] not in by_doc:
            continue
        m = np.load(blob_path(r["blob"]), allow_pickle=False)
        cs = by_doc[r["document_id"]]
        if len(cs) != len(m):
            raise ValueError(f"vectors for document {r['document_id']} do not match its chunks")
        chunks.extend(cs)
        mats.append(m)
    return chunks, (np.vstack(mats) if mats else np.zeros((0, 0), dtype=np.float32))
