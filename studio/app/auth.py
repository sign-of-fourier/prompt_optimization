"""Email + password accounts with an httpOnly session cookie. Passwords: scrypt (stdlib)."""
from __future__ import annotations

import hashlib
import os
import secrets

from fastapi import Depends, HTTPException, Request, Response

from . import db, tiers

SESSION_DAYS = 30
COOKIE = "studio_session"


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return salt.hex() + ":" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split(":")
    return secrets.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex(), h)


def signup(con, email: str, pw: str) -> dict:
    email = email.strip().lower()
    if "@" not in email or len(pw) < 8:
        raise HTTPException(400, "a valid email and a password of 8+ characters are required")
    if con.execute("select 1 from users where email=?", (email,)).fetchone():
        raise HTTPException(409, "an account with this email exists")
    uid = db.new_id()
    con.execute("insert into users (id, email, pw_hash, created, tier) values (?,?,?,?,?)", (uid, email, hash_pw(pw), db.now(), tiers.DEFAULT_TIER))
    con.commit()
    return {"id": uid, "email": email}


def login(con, email: str, pw: str) -> dict:
    u = con.execute("select * from users where email=?", (email.strip().lower(),)).fetchone()
    if not u or not check_pw(pw, u["pw_hash"]):
        raise HTTPException(401, "wrong email or password")
    return {"id": u["id"], "email": u["email"]}


def start_session(con, resp: Response, user_id: str) -> None:
    tok = secrets.token_urlsafe(32)
    con.execute("insert into sessions values (?,?,?)", (tok, user_id, db.now() + SESSION_DAYS * 86400))
    con.commit()
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="lax", secure=os.environ.get("STUDIO_INSECURE_COOKIE") != "1",
                    max_age=SESSION_DAYS * 86400, path="/")


def end_session(con, req: Request, resp: Response) -> None:
    tok = req.cookies.get(COOKIE)
    if tok:
        con.execute("delete from sessions where token=?", (tok,)); con.commit()
    resp.delete_cookie(COOKIE, path="/")


def current_user(request: Request) -> dict:
    con = request.app.state.db
    tok = request.cookies.get(COOKIE)
    s = con.execute("select * from sessions where token=? and expires>?", (tok, db.now())).fetchone() if tok else None
    if not s:
        raise HTTPException(401, "not signed in")
    u = con.execute("select id, email, tier from users where id=?", (s["user_id"],)).fetchone()
    if not u:
        raise HTTPException(401, "not signed in")
    return dict(u)


User = Depends(current_user)
