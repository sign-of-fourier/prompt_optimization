"""Account tiers (2026-09-19). Data, not code, so it can change while billing is settled.

- house_keys     may run on the site's own credentials (billed at our rates)
- own_keys       may add their own endpoints and credentials (every tier)
- house_models   which house models the tier sees; None = every house model whose provider key is configured
- max_concurrency  in-flight model calls per run (wall-clock parallelism)
- max_q          parents per round (GEPA parents_per_round, BO batch q) - the algorithm's parallelism
- assignable     False = defined but nobody can be put on it yet (enterprise)
"""
from __future__ import annotations

import os

TIERS: dict[str, dict] = {
    "free":       {"house_keys": False, "own_keys": True, "house_models": [],                             "max_concurrency": 1,  "max_q": 1,  "assignable": True},
    "beginner":   {"house_keys": True,  "own_keys": True, "house_models": ["us.amazon.nova-micro-v1:0", "us.amazon.nova-lite-v1:0"], "max_concurrency": 4,  "max_q": 4,  "assignable": True},
    "advanced":   {"house_keys": True,  "own_keys": True, "house_models": None,                           "max_concurrency": 16, "max_q": 16, "assignable": True},
    "enterprise": {"house_keys": True,  "own_keys": True, "house_models": None,                           "max_concurrency": 64, "max_q": 64, "assignable": False},
}
DEFAULT_TIER = os.environ.get("STUDIO_DEFAULT_TIER", "beginner")  # what a new signup gets until the paywall exists


def tier_of(name: str | None) -> dict:
    return TIERS.get(name or DEFAULT_TIER) or TIERS["free"]


def allows(tier: str | None, what: str) -> bool:
    return bool(tier_of(tier).get(what, False))


def limit(tier: str | None, what: str) -> int:
    return int(tier_of(tier)[what])


def house_key_present(provider: str) -> bool:
    """Whether the process has a credential for a house provider; house models without one are hidden."""
    if provider == "bedrock":
        return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"))
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return bool(os.environ.get("OPENAI_API_KEY"))
