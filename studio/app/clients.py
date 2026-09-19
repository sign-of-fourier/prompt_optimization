"""Model access for the studio: one `RoutingClient` per run that dispatches on `config.model` to a provider client,
so modules on one canvas may use different providers while cache, budget, semaphore and usage stay in one place
(all of that lives in bpto's `ModelClient` base and is inherited here)."""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from bpto import Budget, CompletionCache, MockClient, ModelClient, ModelConfig
from bpto.llm.base import PRICES, Completion

# model id -> (provider, display). Extend freely; the UI's dropdown reads this.
CATALOG: dict[str, dict[str, Any]] = {
    "us.amazon.nova-micro-v1:0": {"provider": "bedrock", "label": "Nova Micro (Bedrock)", "sampling": True},
    "us.amazon.nova-lite-v1:0": {"provider": "bedrock", "label": "Nova Lite (Bedrock)", "sampling": True},
    "us.amazon.nova-pro-v1:0": {"provider": "bedrock", "label": "Nova Pro (Bedrock)", "sampling": True},
    "claude-haiku-4-5-20251001": {"provider": "anthropic", "label": "Claude Haiku 4.5", "sampling": True},
    "claude-sonnet-5": {"provider": "anthropic", "label": "Claude Sonnet 5", "sampling": False},
    "claude-opus-5": {"provider": "anthropic", "label": "Claude Opus 5", "sampling": False},
    "gpt-4.1-mini": {"provider": "openai", "label": "GPT-4.1 mini", "sampling": True},
    "gpt-4.1": {"provider": "openai", "label": "GPT-4.1", "sampling": True},
}
# $ per 1M tokens (input, output) for models bpto doesn't price. Verify before relying on them.
EXTRA_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}


def prices() -> dict[str, tuple[float, float]]:
    return {**PRICES, **EXTRA_PRICES}


def provider_of(model: str) -> str:
    if model in CATALOG:
        return CATALOG[model]["provider"]
    if "nova" in model or model.startswith("us.") or model.startswith("amazon."):
        return "bedrock"
    if model.startswith("claude"):
        return "anthropic"
    return "openai"


def make_provider(provider: str, model: str, **kw) -> ModelClient:
    if provider == "bedrock":
        from bpto import BedrockClient
        return BedrockClient(model, region=os.environ.get("AWS_REGION", "us-east-1"), **kw)
    if provider == "anthropic":
        from bpto import AnthropicClient
        return AnthropicClient(model, api_key=os.environ.get("ANTHROPIC_API_KEY"), **kw)
    from bpto import OpenAICompatibleClient
    return OpenAICompatibleClient(model, base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                                  api_key=os.environ.get("OPENAI_API_KEY"), **kw)


class Access:
    """What a user may run on: their own credentials (model id -> credential, provider -> credential), whether the
    house keys are allowed for everything else and which house models the tier sees, plus the tier's caps."""

    def __init__(self, credentials: list[dict] | None = None, house_keys: bool = True, house_models: list[str] | None = None,
                 max_concurrency: int = 8, max_q: int = 4, tier: str = "", user_id: str = ""):
        from .tiers import house_key_present
        self.credentials = list(credentials or [])
        self.house_keys = house_keys
        self.max_concurrency, self.max_q, self.tier, self.user_id = max_concurrency, max_q, tier, user_id
        allowed = set(CATALOG) if house_models is None else set(house_models)
        self.house_models = [m for m in CATALOG if m in allowed and house_key_present(CATALOG[m]["provider"])] if house_keys else []
        self.by_model: dict[str, dict] = {}
        self.by_provider: dict[str, dict] = {}
        for c in self.credentials:
            for m in c["models"]:
                self.by_model.setdefault(m, c)
            if c["provider"] in ("bedrock", "anthropic", "openai"):
                self.by_provider.setdefault(c["provider"], c)

    def credential_for(self, model: str) -> dict | None:
        return self.by_model.get(model) or self.by_provider.get(provider_of(model))

    def can_use(self, model: str) -> bool:
        return self.credential_for(model) is not None or model in self.house_models

    def source_of(self, model: str) -> str:
        c = self.credential_for(model)
        return c["id"] if c else "house"

    def catalog(self) -> list[dict]:
        """Models this user may pick: their own endpoints' models first, then the house catalog if allowed."""
        out, seen = [], set()
        for c in self.credentials:
            for m in c["models"]:
                if m not in seen:
                    seen.add(m)
                    house = CATALOG.get(m, {})
                    out.append({"id": m, "provider": c["provider"], "label": f"{house.get('label', m)} · {c['label']}", "source": c["id"],
                                "sampling": house.get("sampling", True)})
        for k in self.house_models:
            if k not in seen:
                out.append({"id": k, **CATALOG[k], "source": "house"})
        return out


class RoutingClient(ModelClient):
    """Dispatches `_complete` to a per-provider client chosen by `config.model`: the user's own credential for that
    model/provider when they have one, else the house keys (if their tier allows). The providers are built with no
    cache/budget of their own: this client's base class holds them for the whole run."""

    def __init__(self, default_model: str, *, access: Access | None = None, mock=None, on_call=None, purpose: str = "", **kw):
        super().__init__(default_config=ModelConfig(model=default_model), **kw)
        self.access = access or Access()
        self._providers: dict[str, ModelClient] = {}
        self._mock = mock  # a MockClient (tests / --mock runs): every model routes to it
        self.on_call, self.purpose = on_call, purpose  # on_call(record) after every completion, cached ones included

    async def complete(self, prompt: str, *, config: ModelConfig | None = None, schema: type[BaseModel] | None = None) -> Completion:
        cfg = self.default_config.merged(config)
        comp = await super().complete(prompt, config=config, schema=schema)
        if self.on_call is not None:
            pin, pout = prices().get(cfg.model) or prices().get(".".join(cfg.model.split(".")[1:])) or (0.0, 0.0)
            self.on_call({"model": cfg.model, "source": "mock" if self._mock is not None else self.access.source_of(cfg.model),
                          "purpose": self.purpose, "input_tokens": comp.input_tokens, "output_tokens": comp.output_tokens,
                          "cached": int(comp.cached), "latency_s": round(comp.latency_s, 4),
                          "usd": 0.0 if (comp.cached or self._mock is not None) else (comp.input_tokens * pin + comp.output_tokens * pout) / 1e6})
        return comp

    def provider(self, model: str) -> ModelClient:
        if self._mock is not None:
            return self._mock
        cred = self.access.credential_for(model)
        key = f"cred:{cred['id']}" if cred else f"house:{provider_of(model)}"
        if key not in self._providers:
            if cred:
                from .credentials import build_client
                self._providers[key] = build_client(cred, model, max_concurrency=1000)  # our semaphore bounds, not theirs
            elif model in self.access.house_models:
                self._providers[key] = make_provider(provider_of(model), model, max_concurrency=1000)
            else:
                raise PermissionError(f"model {model!r} is not available on your tier and you have no endpoint for it: add one under Endpoints & keys")
        return self._providers[key]

    async def _complete(self, prompt: str, cfg: ModelConfig, schema: type[BaseModel] | None) -> Completion:
        return await self.provider(cfg.model)._complete(prompt, cfg, schema)

    async def count_tokens(self, text: str, config: ModelConfig | None = None) -> int:
        cfg = self.default_config.merged(config)
        return await self.provider(cfg.model).count_tokens(text, cfg)


def make_client(default_model: str, *, cache_path=None, budget: Budget | None = None, max_concurrency: int | None = None,
                mock: MockClient | None = None, access: Access | None = None, on_call=None, purpose: str = "") -> RoutingClient:
    """`max_concurrency` defaults to the tier's cap and can only lower it."""
    cap = access.max_concurrency if access else 8
    return RoutingClient(default_model, mock=mock, access=access, on_call=on_call, purpose=purpose,
                         cache=CompletionCache(cache_path) if cache_path else None, budget=budget,
                         max_concurrency=min(cap, max_concurrency) if max_concurrency else cap)
