"""Per-user endpoints and credentials, encrypted at rest (Fernet; key in .env as STUDIO_SECRET, generated once).

A credential is one provider account: `bedrock` (access key + secret, region), `anthropic` (api key),
`openai` (api key, optional base URL) or `custom` (any OpenAI-compatible server: base URL, optional key, and the
model ids it serves). Models from a user's credentials show up in their model dropdown; runs route those models
to the user's own client, never to the house keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastapi import HTTPException

from bpto import ModelClient

from . import db

PROVIDERS = {
    "bedrock": {"label": "Amazon Bedrock (your AWS account)", "fields": ["access_key_id", "secret_access_key", "region"]},
    "anthropic": {"label": "Anthropic API", "fields": ["api_key"]},
    "openai": {"label": "OpenAI API", "fields": ["api_key"]},
    "custom": {"label": "OpenAI-compatible endpoint (vLLM, Ollama, OpenRouter, Azure...)", "fields": ["base_url", "api_key", "models"]},
}
SECRET_FIELDS = {"secret_access_key", "api_key"}
# default model lists offered when a user adds a provider account (they can edit)
DEFAULT_MODELS = {
    "bedrock": ["us.amazon.nova-micro-v1:0", "us.amazon.nova-lite-v1:0", "us.amazon.nova-pro-v1:0"],
    "anthropic": ["claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5"],
    "openai": ["gpt-4.1-mini", "gpt-4.1"],
    "custom": [],
}


def _fernet() -> Fernet:
    key = os.environ.get("STUDIO_SECRET")
    if not key:
        key = Fernet.generate_key().decode()
        env = Path(__file__).resolve().parent.parent / ".env"
        with open(env, "a") as f:
            f.write(f"\nSTUDIO_SECRET={key}\n")
        os.environ["STUDIO_SECRET"] = key
    return Fernet(key.encode())


def encrypt(d: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(d).encode()).decode()


def decrypt(s: str) -> dict[str, Any]:
    return json.loads(_fernet().decrypt(s.encode()).decode())


def masked(cfg: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in cfg.items():
        out[k] = ("•••••" + str(v)[-4:]) if k in SECRET_FIELDS and v else v
    return out


def list_credentials(con, user_id: str) -> list[dict]:
    rows = db.rows(con.execute("select * from credentials where user_id=? order by created", (user_id,)))
    out = []
    for r in rows:
        cfg = decrypt(r["config"])
        out.append({"id": r["id"], "provider": r["provider"], "label": r["label"], "models": json.loads(r["models"]),
                    "config": masked(cfg), "created": r["created"]})
    return out


def load_credentials(con, user_id: str) -> list[dict]:
    """With secrets, for building clients."""
    return [{"id": r["id"], "provider": r["provider"], "label": r["label"], "models": json.loads(r["models"]), "config": decrypt(r["config"])}
            for r in db.rows(con.execute("select * from credentials where user_id=?", (user_id,)))]


def add_credential(con, user_id: str, provider: str, label: str, config: dict[str, Any], models: list[str] | None) -> dict:
    if provider not in PROVIDERS:
        raise HTTPException(400, f"unknown provider {provider!r}")
    cfg = {k: (config.get(k) or "").strip() for k in PROVIDERS[provider]["fields"] if k != "models"}
    missing = [k for k in cfg if not cfg[k] and not (provider == "custom" and k == "api_key") and k != "region"]
    if missing:
        raise HTTPException(400, f"missing: {', '.join(missing)}")
    if provider == "bedrock":
        cfg["region"] = cfg.get("region") or "us-east-1"
    models = [m.strip() for m in (models or DEFAULT_MODELS[provider]) if m and m.strip()]
    if provider == "custom" and not models:
        raise HTTPException(400, "list at least one model id the endpoint serves")
    cid = db.new_id()
    con.execute("insert into credentials values (?,?,?,?,?,?,?)", (cid, user_id, provider, label or PROVIDERS[provider]["label"], encrypt(cfg), json.dumps(models), db.now()))
    con.commit()
    return {"id": cid}


def delete_credential(con, user_id: str, cid: str) -> None:
    con.execute("delete from credentials where id=? and user_id=?", (cid, user_id)); con.commit()


def update_models(con, user_id: str, cid: str, models: list[str]) -> None:
    con.execute("update credentials set models=? where id=? and user_id=?", (json.dumps([m.strip() for m in models if m.strip()]), cid, user_id)); con.commit()


# ---- building bpto clients from a credential ------------------------------------------

class _UserBedrock:
    """BedrockClient with a boto3 session from the user's keys instead of the process environment."""
    def __new__(cls, model: str, cfg: dict[str, Any], **kw):
        import boto3
        from botocore.config import Config
        from bpto import BedrockClient
        c = BedrockClient(model, region=cfg.get("region") or "us-east-1", **kw)
        session = boto3.Session(aws_access_key_id=cfg["access_key_id"], aws_secret_access_key=cfg["secret_access_key"], region_name=cfg.get("region") or "us-east-1")
        c._rt = session.client("bedrock-runtime", config=Config(retries={"max_attempts": 4, "mode": "adaptive"}))
        return c


def build_client(cred: dict, model: str, **kw) -> ModelClient:
    p, cfg = cred["provider"], cred["config"]
    if p == "bedrock":
        return _UserBedrock(model, cfg, **kw)
    if p == "anthropic":
        from bpto import AnthropicClient
        return AnthropicClient(model, api_key=cfg["api_key"], **kw)
    from bpto import OpenAICompatibleClient
    base = cfg.get("base_url") or "https://api.openai.com/v1"
    return OpenAICompatibleClient(model, base_url=base.rstrip("/"), api_key=cfg.get("api_key") or None, **kw)


async def test_credential(cred: dict) -> dict:
    """One tiny completion on the credential's first model."""
    from bpto import ModelConfig
    model = (cred["models"] or DEFAULT_MODELS[cred["provider"]] or [""])[0]
    if not model:
        return {"ok": False, "error": "no model to test with"}
    try:
        c = build_client(cred, model, max_concurrency=1, max_retries=1)
        comp = await c.complete("Reply with the single word: ok", config=ModelConfig(model=model, max_tokens=8, temperature=0.0))
        return {"ok": True, "model": model, "reply": comp.text[:40], "tokens": comp.input_tokens + comp.output_tokens}
    except Exception as e:
        return {"ok": False, "model": model, "error": f"{type(e).__name__}: {str(e)[:300]}"}
