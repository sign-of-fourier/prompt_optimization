"""Dashboard step 2 (app/dashboard.py): the per-hosted-version rows and the cost forecast, on a fresh database with
traces at fixed times, so the arithmetic is checked exactly."""
import sqlite3
from datetime import datetime, timezone

import pytest

from app import dashboard as D, db, hosting as H, store

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc).timestamp()   # 20 Sep: 10 days left in a 30-day month
DAY = 86400.0


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    for m in db.MIGRATIONS:
        try:
            c.execute(m)
        except sqlite3.OperationalError:
            pass
    store.init(c)
    H.init(c)
    c.execute("insert into users (id, email, pw_hash, created, tier) values ('u1','a@b.co','x',0,'advanced')")
    c.execute("insert into users (id, email, pw_hash, created, tier) values ('u2','c@d.co','x',0,'advanced')")
    for pid, uid in (("p1", "u1"), ("p2", "u2")):
        c.execute("insert into projects values (?,?,?,?,?)", (pid, uid, f"project {pid}", '{"name":"x","modules":[]}', 0))
    return c


SPEC = {"name": "t", "modules": [{"id": "route", "template": "Route: {message}", "model": "us.amazon.nova-micro-v1:0",
                                  "schema_fields": [{"name": "queue"}]}], "eval_model": "us.amazon.nova-micro-v1:0"}


def version(c, uid, pid, hosted_days_ago):
    v = store.create_version(c, project_id=pid, user_id=uid, label="v1", spec=SPEC, fingerprint="f", source={"kind": "run_node"},
                             score=0.8, metrics={"prompt_tokens": 380.0, "output_tokens": 6.0}, n_rows=40, holdout={"best_score": 0.78})
    c.execute("insert into hosted_versions values (?,?,?)", (v["id"], uid, NOW - hosted_days_ago * DAY))
    return v["id"]


def trace(c, vid, uid, pid, at, usd=0.001, lat=1.0, error=None):
    c.execute("insert into traces (id, version_id, project_id, user_id, inputs, output, latency_s, usd, error, created)"
              " values (?,?,?,?,?,?,?,?,?,?)", (db.new_id(), vid, pid, uid, "{}", "x", lat, usd, error, at))


def test_rows_and_forecast(con):
    vid = version(con, "u1", "p1", hosted_days_ago=30)
    for d in range(1, 8):                                   # $0.001 a day for the last 7 days, one request each
        trace(con, vid, "u1", "p1", NOW - d * DAY - 60)
    for i in range(4):                                      # today: 4 requests, one failed, latencies 1..4 s
        trace(con, vid, "u1", "p1", NOW - 3600 * (i + 1), usd=0.002, lat=float(i + 1), error="boom" if i == 3 else None)
    trace(con, vid, "u1", "p1", datetime(2026, 8, 15, tzinfo=timezone.utc).timestamp(), usd=0.5)   # last month
    s = D.summary(con, "u1", "24h", now=NOW)
    r = s["rows"][0]
    assert s["totals"]["hosted"] == 1 and r["requests"] == 4 and r["errors"] == 1 and r["error_rate"] == 0.25
    assert r["p50"] == 2.0 and r["p95"] == 3.0                   # nearest rank over the three that succeeded
    assert sum(r["sparkline"]) == 4 and len(r["sparkline"]) == 24
    c = r["cost"]
    assert c["today"] == pytest.approx(0.008) and c["last_month"] == pytest.approx(0.5)
    month = 7 * 0.001 + 0.008
    assert c["month"] == pytest.approx(month)
    # forecast: month to date + 10 remaining days x the last 7 days' average daily spend (the 7th day back is just outside)
    assert c["forecast"] == pytest.approx(month + 10 * (6 * 0.001 + 0.008) / 7)
    assert c["per_request"] == pytest.approx(0.002) and c["estimate"] > 0
    assert r["earned"] == {"score": 0.8, "holdout": 0.78, "n_rows": 40} and r["status"] == "healthy"   # under 20 requests: no error alert
    w = D.summary(con, "u1", "7d", now=NOW)["rows"][0]
    assert w["requests"] == 4 + 6 and len(w["sparkline"]) == 7   # the 7th day back sits just outside the window


def test_new_versions_errors_and_isolation(con):
    vid = version(con, "u1", "p1", hosted_days_ago=1)
    for i in range(25):
        trace(con, vid, "u1", "p1", NOW - 60 * (i + 1), error="x" if i < 3 else None)
    r = D.summary(con, "u1", now=NOW)["rows"][0]
    assert r["cost"]["forecast"] is None and "too new" in r["cost"]["forecast_note"]
    assert r["status"] == "errors" and D.summary(con, "u1", now=NOW)["totals"]["forecast"] is None
    # another customer's hosted version and traces never appear, and an account with nothing hosted gets no rows
    other = version(con, "u2", "p2", hosted_days_ago=10)
    trace(con, other, "u2", "p2", NOW - 60)
    assert [x["version_id"] for x in D.summary(con, "u1", now=NOW)["rows"]] == [vid]
    assert D.summary(con, "u2", now=NOW)["rows"][0]["requests"] == 1
    con.execute("delete from hosted_versions where user_id='u1'")
    assert D.summary(con, "u1", now=NOW)["rows"] == []
    # idle: hosted, no traffic in the window
    idle = version(con, "u1", "p1", hosted_days_ago=5)
    assert D.summary(con, "u1", now=NOW)["rows"][0]["status"] == "idle"
