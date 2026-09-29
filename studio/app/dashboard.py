"""The dashboard (DASHBOARD.md, step 2): what each hosted version is doing.

Computed on request from `traces` (each trace carries its request's cost, loops and steps included), not from an
hourly table: at today's volumes a query is cheap, and `summary` is the one entry point a rollup table would replace.
Cost is the AWS cost as metered; the surcharge is not defined yet. Times are UTC; a month is a calendar month.
"""
from __future__ import annotations

import calendar
import math
import time
from datetime import datetime, timezone
from typing import Any

from . import hosting as H, store

FORECAST_MIN_DAYS = 3        # younger than this: "too new to forecast"
ERROR_RATE_ALERT = 0.05      # DASHBOARD.md's proposal, with at least ERROR_MIN_REQUESTS in the window
ERROR_MIN_REQUESTS = 20


def _pct(xs: list[float], q: float) -> float | None:
    """Nearest-rank percentile: honest on small samples (no interpolation between two requests)."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return s[k]


def _month_bounds(now: float) -> tuple[float, float, float, int, int]:
    """(start of this month, start of last month, start of today, days in this month, day of month), all UTC."""
    d = datetime.fromtimestamp(now, timezone.utc)
    this = datetime(d.year, d.month, 1, tzinfo=timezone.utc)
    last = datetime(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1, 1, tzinfo=timezone.utc)
    today = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return this.timestamp(), last.timestamp(), today.timestamp(), calendar.monthrange(d.year, d.month)[1], d.day


def _row(con, user_id: str, vid: str, hosted_at: float, window_s: float, now: float) -> dict[str, Any] | None:
    v = store.get_version(con, vid, user_id)
    if v is None:
        return None
    month0, last0, today0, days_in_month, day = _month_bounds(now)
    since = min(now - window_s, last0)
    ts = con.execute("select created, usd, latency_s, error from traces where version_id=? and user_id=? and created>=?"
                     " order by created", (vid, user_id, since)).fetchall()
    win = [t for t in ts if t[0] >= now - window_s]
    ok_lat = [t[2] for t in win if t[3] is None and t[2] is not None]
    errors = sum(1 for t in win if t[3] is not None)
    # sparkline: 24 hourly buckets, or 7 daily ones
    n_b, width = (24, 3600.0) if window_s <= 86400 else (7, 86400.0)
    buckets = [0] * n_b
    for t in win:
        i = int((t[0] - (now - n_b * width)) // width)
        if 0 <= i < n_b:
            buckets[i] += 1
    usd = lambda a, b: sum((t[1] or 0.0) for t in ts if a <= t[0] < b)
    mtd, today, last_month = usd(month0, now + 1), usd(today0, now + 1), usd(last0, month0)
    age_days = (now - hosted_at) / 86400
    first = max(hosted_at, now - 7 * 86400)
    days7 = max(1.0, (now - first) / 86400)
    daily = usd(now - 7 * 86400, now + 1) / days7
    forecast = mtd + daily * (days_in_month - day) if age_days >= FORECAST_MIN_DAYS else None
    spent_win = sum((t[1] or 0.0) for t in win)
    ho = v.get("holdout") or {}
    rate = errors / len(win) if win else 0.0
    status = ("idle", "no requests in this window") if not win else \
             ("errors", f"{rate:.0%} of requests failed") if rate > ERROR_RATE_ALERT and len(win) >= ERROR_MIN_REQUESTS else \
             ("healthy", "healthy")
    proj = con.execute("select name from projects where id=?", (v["project_id"],)).fetchone()
    return {
        "version_id": vid, "label": v["label"], "project_id": v["project_id"], "project": proj[0] if proj else "",
        "hosted_at": hosted_at, "url": H.public_url(vid),
        "requests": len(win), "errors": errors, "error_rate": rate, "sparkline": buckets,
        "p50": _pct(ok_lat, 0.5), "p95": _pct(ok_lat, 0.95),
        "cost": {"today": today, "month": mtd, "last_month": last_month, "forecast": forecast,
                 "forecast_note": None if forecast is not None else f"too new to forecast (hosted {age_days:.1f} days)",
                 "per_request": spent_win / len(win) if win else None, "estimate": H.estimate(v).get("usd_per_request")},
        "earned": {"score": v.get("score"), "holdout": ho.get("best_score"), "n_rows": v.get("n_rows")},
        "status": status[0], "status_text": status[1],
    }


def summary(con, user_id: str, window: str = "24h", now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    window_s = 7 * 86400.0 if window == "7d" else 86400.0
    hosted = con.execute("select version_id, hosted from hosted_versions where user_id=? order by hosted", (user_id,)).fetchall()
    rows = [r for r in (_row(con, user_id, vid, at, window_s, now) for vid, at in hosted) if r]
    fc = [r["cost"]["forecast"] for r in rows]
    return {
        "window": "7d" if window == "7d" else "24h", "updated": now,
        "totals": {"hosted": len(rows), "requests": sum(r["requests"] for r in rows),
                   "month": sum(r["cost"]["month"] for r in rows), "last_month": sum(r["cost"]["last_month"] for r in rows),
                   # a forecast for the account only when every hosted version has one; otherwise say which do not
                   "forecast": sum(fc) if rows and all(f is not None for f in fc) else None,
                   "forecast_partial": sum(1 for f in fc if f is None)},
        "rows": rows,
    }
