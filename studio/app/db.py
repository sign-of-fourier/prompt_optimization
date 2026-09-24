"""SQLite persistence. One file under data/. Rows are dicts; JSON columns are decoded on read."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("STUDIO_DATA", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "studio.sqlite"

SCHEMA = """
create table if not exists users (id text primary key, email text unique not null, pw_hash text not null, created real not null);
create table if not exists sessions (token text primary key, user_id text not null, expires real not null);
create table if not exists projects (id text primary key, user_id text not null, name text not null, spec text not null, updated real not null);
create table if not exists datasets (id text primary key, project_id text not null, name text not null, path text not null,
    columns text not null, n_rows integer not null, input_map text not null, label_column text, created real not null);
create table if not exists validations (id text primary key, project_id text not null, dataset_id text not null, report text not null,
    pilot text, cost text, created real not null);
create table if not exists runs (id text primary key, project_id text not null, dataset_id text not null, status text not null,
    dir text not null, started real not null, finished real, summary text, error text);
create table if not exists credentials (id text primary key, user_id text not null, provider text not null, label text not null,
    config text not null, models text not null, created real not null);
create table if not exists usage_log (id integer primary key autoincrement, ts real not null, user_id text not null, tier text,
    project_id text, run_id text, purpose text, model text not null, source text not null, input_tokens integer not null,
    output_tokens integer not null, cached integer not null, latency_s real, usd real not null);
create index if not exists usage_user_ts on usage_log (user_id, ts);
"""
MIGRATIONS = ["alter table users add column tier text",
              # a run has to carry the spec it ran: publishing a version from it later must not pick up
              # whatever the canvas has drifted to since
              "alter table runs add column spec text"]
JSON_COLS = {"spec", "columns", "input_map", "report", "pilot", "cost", "summary"}  # not "models"/"config": credentials decode their own


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            con.execute(m); con.commit()
        except sqlite3.OperationalError:
            pass  # already applied
    return con


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


def row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS:
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def rows(rs) -> list[dict]:
    return [row(r) for r in rs]


def enc(v):
    return json.dumps(v) if isinstance(v, (dict, list)) else v


def usage_logger(con, user_id: str, tier: str | None, project_id: str | None = None, run_id: str | None = None):
    """A RoutingClient `on_call` that appends one usage_log row per completion (cache hits included, usd 0)."""
    def _log(r: dict) -> None:
        con.execute("insert into usage_log (ts, user_id, tier, project_id, run_id, purpose, model, source, input_tokens, output_tokens, cached, latency_s, usd)"
                    " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now(), user_id, tier, project_id, run_id, r["purpose"], r["model"], r["source"], r["input_tokens"], r["output_tokens"],
                     r["cached"], r["latency_s"], r["usd"]))
        con.commit()
    return _log


def usage_summary(con, user_id: str, days: int = 30) -> dict:
    since = now() - days * 86400
    by_model = rows(con.execute("select model, source, count(*) calls, sum(cached) cached, sum(input_tokens) input_tokens, sum(output_tokens) output_tokens,"
                                " sum(usd) usd from usage_log where user_id=? and ts>? group by model, source order by usd desc", (user_id, since)))
    tot = row(con.execute("select count(*) calls, coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(output_tokens),0) output_tokens,"
                          " coalesce(sum(case when source='house' then usd else 0 end),0) house_usd, coalesce(sum(usd),0) usd from usage_log where user_id=? and ts>?", (user_id, since)).fetchone())
    return {"days": days, "totals": tot, "by_model": by_model}
