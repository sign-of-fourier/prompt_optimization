"""A customer-records service: the demo external step, and the reference implementation for anyone writing one.

It is deliberately NOT part of the studio. It runs as its own process on its own port with its own key, and the
studio reaches it over HTTP exactly as it would reach a third party. If the studio ever special-cases this service -
a hardcoded url, a skipped credential - the boundary is fiction and the piece has failed.

    RECORDS_API_KEY=... uvicorn service:app --port 8200

Contract (see records.json manifest in studio/steps/):
    POST /records  {"customer_id": "C1000"} -> 200 {"customer_id": ..., "plan": ..., ...}
                                            -> 404 {"detail": "no record"}   <- how this service says "unknown"
Auth: Authorization: Bearer <key>.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

RECORDS = json.loads((Path(__file__).resolve().parent / "records.json").read_text())
API_KEY = os.environ.get("RECORDS_API_KEY", "dev-key")
# A real lookup crosses a network and hits somebody's database. On loopback it would return in under a millisecond,
# which would make every latency number the studio records a fantasy - so the delay is explicit and declared.
LATENCY_MS = (60, 120)

app = FastAPI(title="Customer records (demo external step)")


class Lookup(BaseModel):
    customer_id: str


def _auth(authorization: str | None) -> None:
    key = authorization[7:].strip() if (authorization or "").lower().startswith("bearer ") else ""
    if key != API_KEY:
        raise HTTPException(401, "bad or missing api key")


@app.post("/records")
async def records(body: Lookup, authorization: str | None = Header(default=None)):
    _auth(authorization)
    await asyncio.sleep(random.uniform(*LATENCY_MS) / 1000)
    rec = RECORDS.get(body.customer_id)
    if rec is None:
        raise HTTPException(404, "no record")
    return rec


@app.get("/health")
async def health():
    return {"ok": True, "records": len(RECORDS)}
