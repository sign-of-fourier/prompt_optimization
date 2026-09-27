"""The Jev judge beta: TypeSafe's typed-answer model as a yes/no scorer, behind three gates.

1. `STUDIO_JEV=1` on the service (the kill switch), and a key: `OPENROUTER_API_KEY` or `TYPESAFE_API_KEY`.
2. The user is on `STUDIO_JEV_USERS` (comma-separated emails, or `*` for every signed-in user).
3. Shadow only: the scorer records `jev_match` beside the project's own scorers and validation refuses any objective
   weight on it, so a Jev outage, a wrong verdict or a surprise bill cannot steer a run.

Every failure (no client, HTTP error, its own call cap reached, a malformed answer) is caught inside the scorer and
recorded as `<name>_failed` = 1 for that row; it never becomes a row error, which would zero the row's other metrics.
The Jev client has its own `Budget` (calls only: the OpenRouter price is not in `clients.prices()` yet).
Everything bpto-side is `bpto.JevClient` + `bpto.jev_judge` (bpto README, experiments/2026-09-27-jev-judge-pilot).
"""
from __future__ import annotations

import json
import os
from typing import Any

from .models import ProjectSpec, ScorerSpec, ValidationReport

DEFAULT_QUESTION = "Is the model answer the same answer as the reference answer?"
MODEL = os.environ.get("STUDIO_JEV_MODEL", "jev-1.13")   # pinned: the run's cache outlives a jev-latest bump
MAX_CALLS = int(os.environ.get("STUDIO_JEV_MAX_CALLS", "3000"))
INPUT_CHARS = 2000   # per input value: a retrieval context can be long, and Jev bills input tokens


def _key() -> tuple[str, str] | None:
    if os.environ.get("OPENROUTER_API_KEY"):
        return "https://openrouter.ai/api", os.environ["OPENROUTER_API_KEY"]
    if os.environ.get("TYPESAFE_API_KEY"):
        return "https://api.typesafe.ai", os.environ["TYPESAFE_API_KEY"]
    return None


def server_enabled(mock: bool = False) -> bool:
    """The service flag, plus a key unless every call goes to the mock."""
    return os.environ.get("STUDIO_JEV") == "1" and (mock or _key() is not None)


def enabled_for(user: dict | None, mock: bool = False) -> bool:
    if not server_enabled(mock) or not user:
        return False
    allow = {e.strip().lower() for e in os.environ.get("STUDIO_JEV_USERS", "").split(",") if e.strip()}
    return "*" in allow or (user.get("email") or "").lower() in allow


def uses_jev(spec: ProjectSpec) -> bool:
    return any(sc.type == "jev_match" for sc in spec.evaluate.scorers)


def metric_names(sc: ScorerSpec) -> list[str]:
    name = sc.name or "jev_match"
    return [name, f"{name}_failed"]


def check(spec: ProjectSpec, user: dict | None, rep: ValidationReport, mock: bool = False) -> None:
    """The beta's validation: gated per user, shadow only. Runs next to validate_static wherever a run can start."""
    if not uses_jev(spec):
        return
    if not enabled_for(user, mock):
        rep.add("error", "scorer", "this project uses the Jev judge beta, which is not enabled for your account on this server; remove that scorer to run")
    for sc in spec.evaluate.scorers:
        if sc.type == "jev_match":
            weighted = [k for k in metric_names(sc) if spec.evaluate.objective.get(k)]
            if weighted:
                rep.add("error", "scorer", f"the Jev judge is shadow-only during the beta: it is recorded, not optimized; set the weight on {weighted[0]!r} to 0")
    rep.add("info", "scorer", "Jev judge (beta): recorded beside your scorers for comparison, never weighted; one Jev call per row per evaluation")


# ---- client ----------------------------------------------------------------------------------

def _mock_transport():
    """Offline Jev: P(yes) is high when the normalized reference appears in the model answer, low otherwise."""
    import httpx
    from .scorers import normalize

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        st = body["state"] if isinstance(body["state"], dict) else {}
        ref, ans = normalize(st.get("reference_answer")), normalize(st.get("model_answer"))
        p = 0.95 if ref and ref in ans else 0.05
        return httpx.Response(200, json={"model": body.get("model"), "answers": {k: {"noul": p} for k in body["questions"]},
                                         "usage": {"input_tokens": 100, "output_tokens": 10}})
    return httpx.MockTransport(handle)


def make_client(mock: bool = False, max_calls: int = MAX_CALLS):
    """A JevClient with its own call cap, or None when the server has no Jev (the scorer then records failures)."""
    if not server_enabled(mock):
        return None
    from bpto import Budget, JevClient
    budget = Budget(max_calls=max_calls)
    if mock:
        return JevClient(MODEL, base_url="http://jev.mock", transport=_mock_transport(), budget=budget, max_retries=0)
    base, key = _key()
    return JevClient(MODEL, base_url=base, api_key=key, budget=budget, max_retries=2)


# ---- scorer ----------------------------------------------------------------------------------

def scorer(sc: ScorerSpec, client) -> Any:
    """`bpto.jev_judge` with one yes/no question, wrapped so that nothing Jev does can fail a row."""
    from bpto import jev_judge
    from bpto.llm.jev import noul
    from .scorers import predicted

    name, failed = metric_names(sc)
    if client is None:
        async def _off(prompt, ex, comp, ctx):
            return {name: 0.0, failed: 1.0}
        return _off

    def state(ex, comp):
        # fixed key order: Jev reads the state in order and its answers move with it (the pilot's NOTES)
        got = predicted(comp, sc.field)
        return {"inputs": {k: str(v)[:INPUT_CHARS] for k, v in (ex.inputs or {}).items()},
                "reference_answer": ex.answer,
                "model_answer": got if isinstance(got, str) else json.dumps(got, default=str)}

    inner = jev_judge({name: noul(sc.rubric or DEFAULT_QUESTION)}, client, state=state)

    async def _s(prompt, ex, comp, ctx):
        try:
            m = await inner(prompt, ex, comp, ctx)
            return {name: max(0.0, min(1.0, float(m[name]))), failed: 0.0}
        except Exception:   # includes the Jev client's own BudgetExceeded: the run's budget is a different object
            return {name: 0.0, failed: 1.0}
    return _s
