"""The OAuth authorization-code flow, against a real HTTP provider on a real socket.

What is worth testing here is not "does a token arrive" but the things that are quietly wrong in most
implementations: that `state` is single-use so a replayed callback fails, that it expires, that it is bound to the
user who began the flow, that tokens are encrypted at rest rather than merely out of sight, that a listing never
returns them, and that refresh happens on its own before a caller ever sees an expired token.

HubSpot's own consent screen is the only part this cannot cover.
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

# No STUDIO_DATA of its own: `db.DATA_DIR` is bound when `app.db` is imported, so a second test module setting it
# (or deleting it) pulls the directory out from under whichever module imported first. These tests use their own
# user ids and their own tables, so sharing the data directory is harmless.
from app import credentials as C, db, oauth as O, store  # noqa: E402

ISSUED: list[dict] = []


def _provider_app() -> FastAPI:
    app = FastAPI()

    @app.post("/token")
    async def token(request: Request):
        form = dict(await request.form())
        ISSUED.append(form)
        if form.get("client_id") != "cid" or form.get("client_secret") != "shh":
            return JSONResponse({"message": "bad client"}, status_code=401)
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != "good-code":
                return JSONResponse({"message": "bad code"}, status_code=400)
            if form.get("redirect_uri") != O.redirect_uri("fake"):
                return JSONResponse({"message": "redirect_uri mismatch"}, status_code=400)
            return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 1800}
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") != "rt-1":
                return JSONResponse({"message": "bad refresh token"}, status_code=400)
            return {"access_token": "at-2", "expires_in": 1800}   # no new refresh token, as some providers do
        return JSONResponse({"message": "unsupported"}, status_code=400)

    @app.get("/whoami/{tok}")
    async def whoami(tok: str):
        return {"hub_id": 424242}

    return app


@pytest.fixture(scope="module")
def provider():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(_provider_app(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    O.PROVIDERS["fake"] = O.Provider(
        name="fake", label="Fake", authorize_url=f"http://127.0.0.1:{port}/authorize",
        token_url=f"http://127.0.0.1:{port}/token", scopes=["oauth", "things.read"],
        client_id_env="FAKE_CLIENT_ID", client_secret_env="FAKE_CLIENT_SECRET",
        account_url=f"http://127.0.0.1:{port}/whoami/{{token}}")
    os.environ["FAKE_CLIENT_ID"] = "cid"
    os.environ["FAKE_CLIENT_SECRET"] = "shh"
    yield
    srv.should_exit = True
    O.PROVIDERS.pop("fake", None)


@pytest.fixture()
def con():
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = db.connect()
    store.init(c)
    yield c
    c.close()


def test_authorize_url_carries_what_the_provider_needs(provider, con):
    from urllib.parse import parse_qs, urlparse
    url = O.start(con, "user-1", "fake")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"] and q["scope"] == ["oauth things.read"]
    assert q["redirect_uri"] == [O.redirect_uri("fake")] and q["redirect_uri"][0].startswith("https://")
    assert len(q["state"][0]) >= 20


def test_a_state_works_once(provider, con):
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(O.start(con, "user-1", "fake")).query)["state"][0]
    conn = asyncio.run(O.finish(con, state, "good-code"))
    assert conn["provider"] == "fake" and conn["account"] == "424242" and conn["label"] == "Fake · 424242"
    # replaying the same callback finds nothing: the state was consumed, not merely checked
    with pytest.raises(O.OAuthError) as e:
        asyncio.run(O.finish(con, state, "good-code"))
    assert "expired or was already used" in str(e.value)
    with pytest.raises(O.OAuthError):
        asyncio.run(O.finish(con, "never-issued", "good-code"))


def test_an_expired_state_is_refused(provider, con):
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(O.start(con, "user-1", "fake")).query)["state"][0]
    con.execute("update oauth_states set created=? where state=?", (db.now() - O.store.STATE_TTL - 5, state))
    con.commit()
    with pytest.raises(O.OAuthError):
        asyncio.run(O.finish(con, state, "good-code"))


def test_tokens_are_encrypted_and_never_listed(provider, con):
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(O.start(con, "user-2", "fake")).query)["state"][0]
    conn = asyncio.run(O.finish(con, state, "good-code"))
    raw = con.execute("select tokens from connections where id=?", (conn["id"],)).fetchone()["tokens"]
    assert "at-1" not in raw and "rt-1" not in raw                  # at rest, not merely out of sight
    assert C.decrypt(raw)["access_token"] == "at-1"
    listed = store.list_connections(con, "user-2")
    assert listed and "tokens" not in listed[0] and "expires" in listed[0]


def test_refresh_happens_before_a_caller_sees_an_expired_token(provider, con):
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(O.start(con, "user-3", "fake")).query)["state"][0]
    conn = asyncio.run(O.finish(con, state, "good-code"))

    assert asyncio.run(O.access_token(con, conn)) == "at-1"          # still fresh: no exchange
    n = len(ISSUED)
    assert asyncio.run(O.access_token(con, conn)) == "at-1" and len(ISSUED) == n

    # 30 seconds left is not enough to start a request with
    con.execute("update connections set expires=? where id=?", (time.time() + 30, conn["id"]))
    con.commit()
    conn = store.get_connection(con, conn["id"])
    assert asyncio.run(O.access_token(con, conn)) == "at-2"
    assert ISSUED[-1]["grant_type"] == "refresh_token"

    stored = C.decrypt(store.get_connection(con, conn["id"])["tokens"])
    assert stored["access_token"] == "at-2" and stored["refresh_token"] == "rt-1"   # carried over, not lost


def test_reconnecting_replaces_rather_than_accumulates(provider, con):
    from urllib.parse import parse_qs, urlparse
    for _ in range(3):
        state = parse_qs(urlparse(O.start(con, "user-4", "fake")).query)["state"][0]
        asyncio.run(O.finish(con, state, "good-code"))
    assert len(store.list_connections(con, "user-4")) == 1


def test_a_bad_code_reports_the_provider_and_stores_nothing(provider, con):
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(O.start(con, "user-5", "fake")).query)["state"][0]
    with pytest.raises(O.OAuthError) as e:
        asyncio.run(O.finish(con, state, "wrong-code"))
    assert "refused the token request" in str(e.value)
    assert store.list_connections(con, "user-5") == []


def test_an_unconfigured_provider_says_what_is_missing(provider, con):
    os.environ.pop("FAKE_CLIENT_SECRET")
    try:
        with pytest.raises(O.OAuthError) as e:
            O.start(con, "user-6", "fake")
        assert "FAKE_CLIENT_SECRET" in str(e.value)
    finally:
        os.environ["FAKE_CLIENT_SECRET"] = "shh"
