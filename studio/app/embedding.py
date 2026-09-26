"""Embedding with a meter on it. bpto's embedders take a `Budget` but have no usage hook, so every embedder the
studio builds is wrapped here: the wrapped embedder charges a local `Budget`, and each `embed()` call writes what it
added to that budget as one `usage_log` row. Before this, the BO embedder's Titan calls reached neither.

Two embedders: Titan v2 on the house Bedrock account, and bpto's `HashEmbedder` under the mock (offline, free,
deterministic). Vectors are stored per model id, so the two never mix.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Sequence

from bpto import Budget

from .clients import prices

TITAN = "amazon.titan-embed-text-v2:0"
HASH = "hash-v1-256"
RETRIEVAL_DIMS = 1024  # Titan v2 is priced per input token, not per dimension: the largest size costs nothing extra


class MeteredEmbedder:
    def __init__(self, inner: Any, model: str, budget: Budget, *, source: str, purpose: str,
                 on_call: Callable[[dict], None] | None = None):
        self.inner, self.model, self.budget, self.source, self.purpose, self.on_call = inner, model, budget, source, purpose, on_call
        self._logged_tokens, self._logged_usd = 0, 0.0  # what earlier rows already reported: overlapping calls never double-count

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        t0 = time.monotonic()
        try:
            return await self.inner.embed(texts)
        finally:
            tokens, usd = self.budget.spent.input_tokens - self._logged_tokens, self.budget.spent_usd - self._logged_usd
            self._logged_tokens += tokens
            self._logged_usd += usd
            if self.on_call is not None and (tokens or self.source == "mock"):
                self.on_call({"model": self.model, "source": self.source, "purpose": self.purpose, "input_tokens": tokens,
                              "output_tokens": 0, "cached": 0, "latency_s": round(time.monotonic() - t0, 4), "usd": usd})


def make_embedder(*, mock: bool, purpose: str, on_call=None, max_usd: float | None = None, dims: int = RETRIEVAL_DIMS,
                  max_concurrency: int = 8, hash_dims: int = 256) -> MeteredEmbedder:
    budget = Budget(max_usd=max_usd, prices=prices())
    if mock:
        from bpto.bo import HashEmbedder
        return MeteredEmbedder(HashEmbedder(dim=hash_dims), HASH, budget, source="mock", purpose=purpose, on_call=on_call)
    from bpto.bo import BedrockEmbedder
    inner = BedrockEmbedder(TITAN, region=os.environ.get("AWS_REGION", "us-east-1"), dimensions=dims,
                            max_concurrency=max_concurrency, budget=budget)
    return MeteredEmbedder(inner, TITAN, budget, source="house", purpose=purpose, on_call=on_call)


def estimate_usd(tokens: int, model: str = TITAN) -> float:
    return tokens * prices().get(model, (0.0, 0.0))[0] / 1e6
