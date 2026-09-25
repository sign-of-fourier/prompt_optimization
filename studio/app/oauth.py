"""OAuth 2.0 authorization-code flow, for connecting a user's account on someone else's system.

This is the credential half of PLAN.md Piece 5, and it is deliberately not the same thing as a step credential
(`steps.py`): a step *calls a service* with a key the user already holds, while a connection *acts as the user*
inside their own tenant. That difference is why this needs a consent screen the user can see and revoke, and why a
pasted admin token - which is what v0 started with - was never going to be the answer.

What must be right, because getting it wrong is a redesign rather than a rewrite:

- **`state` is single-use, short-lived and server-side**, bound to the session that began the flow. A signed cookie
  cannot be *consumed*; a replayed callback has to find nothing. `store.take_state` reads and deletes together.
- **Tokens are encrypted at rest** with the same Fernet key the model credentials use, and never logged or returned
  by any listing endpoint.
- **Refresh is part of the flow, not an afterthought.** HubSpot access tokens last 30 minutes, so anything that
  holds one for longer than a request has to refresh, and the refresh has to be what callers go through.

Providers are data, so a second one is a dict entry rather than a code path.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from . import credentials as C, store
from .brand import public_url


class Provider(BaseModel):
    name: str
    label: str
    authorize_url: str
    token_url: str
    scopes: list[str]
    client_id_env: str
    client_secret_env: str
    account_field: str = ""       # field in the token response identifying the account (HubSpot: hub_id)
    account_url: str = ""         # or an endpoint that names the account, called with the fresh token

    def client(self) -> tuple[str, str]:
        cid, secret = os.environ.get(self.client_id_env, ""), os.environ.get(self.client_secret_env, "")
        if not cid or not secret:
            raise OAuthError(f"{self.label} is not configured on this server: set {self.client_id_env} and "
                             f"{self.client_secret_env} in the studio's .env")
        return cid, secret


PROVIDERS: dict[str, Provider] = {
    "hubspot": Provider(
        name="hubspot",
        label="HubSpot",
        authorize_url="https://app.hubspot.com/oauth/authorize",
        token_url="https://api.hubspot.com/oauth/v3/token",   # v1 is deprecated; new integrations use v3
        scopes=["oauth", "crm.objects.contacts.read"],
        client_id_env="HUBSPOT_CLIENT_ID",
        client_secret_env="HUBSPOT_CLIENT_SECRET",
        account_url="https://api.hubspot.com/oauth/v1/access-tokens/{token}",
    ),
}


class OAuthError(Exception):
    pass


def redirect_uri(provider: str) -> str:
    """Must match what is registered with the provider exactly, so it is derived from one place."""
    return f"{public_url()}/api/oauth/{provider}/callback"


def start(con, user_id: str, provider: str) -> str:
    p = PROVIDERS.get(provider)
    if p is None:
        raise OAuthError(f"no such provider {provider!r}")
    cid, _ = p.client()
    state = secrets.token_urlsafe(24)
    store.put_state(con, state, user_id, provider)
    return p.authorize_url + "?" + urlencode({
        "client_id": cid, "redirect_uri": redirect_uri(provider), "scope": " ".join(p.scopes), "state": state,
    })


async def _post_token(p: Provider, form: dict[str, str]) -> dict[str, Any]:
    cid, secret = p.client()
    body = {"client_id": cid, "client_secret": secret, **form}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(p.token_url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        # the provider's message is useful ("redirect_uri mismatch") and carries no secret of ours
        raise OAuthError(f"{p.label} refused the token request ({r.status_code}): {r.text[:300]}")
    return r.json()


async def _token_info(p: Provider, access_token: str) -> tuple[str | None, list[str] | None]:
    """Which account this token is for, and **which scopes were actually granted**.

    The distinction matters: what we put in the authorize URL is what we asked for, and recording that as if it were
    the grant makes `step_validation.check_scopes` compare a manifest against a wish. The provider is the only
    authority on what was given - a user can decline an optional scope, and an app can be configured for fewer than
    the client requests."""
    if not p.account_url:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(p.account_url.format(token=access_token))
        if r.status_code < 400:
            d = r.json()
            v = d.get("hub_id") or d.get("account_id") or d.get("portalId")
            granted = d.get("scopes") if isinstance(d.get("scopes"), list) else None
            return (str(v) if v is not None else None), granted
    except Exception:
        pass       # naming the account is a nicety; failing to do it must not fail the connection
    return None, None


def _store_tokens(tok: dict[str, Any]) -> tuple[str, float | None]:
    expires = time.time() + float(tok["expires_in"]) if tok.get("expires_in") else None
    return C.encrypt({"access_token": tok.get("access_token", ""), "refresh_token": tok.get("refresh_token", "")}), expires


async def finish(con, state: str, code: str) -> dict[str, Any]:
    """The callback. Consumes the state, exchanges the code, stores the tokens encrypted."""
    st = store.take_state(con, state)
    if st is None:
        raise OAuthError("this authorization link has expired or was already used; start again")
    p = PROVIDERS[st["provider"]]
    tok = await _post_token(p, {"grant_type": "authorization_code", "redirect_uri": redirect_uri(p.name), "code": code})
    account, granted = await _token_info(p, tok.get("access_token", ""))
    enc, expires = _store_tokens(tok)
    # what was granted, falling back to what we asked for only when the provider will not say
    scopes = granted if granted is not None else (tok.get("scope") or "").split() or p.scopes
    return store.create_connection(con, user_id=st["user_id"], provider=p.name, account=account,
                                   label=f"{p.label}{' · ' + account if account else ''}", tokens=enc,
                                   expires=expires, scopes=" ".join(scopes))


async def access_token(con, conn: dict) -> str:
    """A token that is valid *now*. Refreshes with a minute to spare, because a token that expires mid-request is
    the failure this function exists to prevent. Every caller goes through here; nothing reads `tokens` directly."""
    p = PROVIDERS[conn["provider"]]
    tok = C.decrypt(conn["tokens"])
    if conn.get("expires") and conn["expires"] - time.time() > 60:
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise OAuthError(f"the {p.label} connection has expired and has no refresh token; reconnect it")
    fresh = await _post_token(p, {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    fresh.setdefault("refresh_token", tok["refresh_token"])   # some providers do not reissue one
    enc, expires = _store_tokens(fresh)
    store.update_connection_tokens(con, conn["id"], enc, expires)
    return fresh.get("access_token", "")
