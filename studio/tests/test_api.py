import asyncio
import io
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient

import tempfile
os.environ["STUDIO_DATA"] = os.path.join(tempfile.gettempdir(), "studio_test_data")
os.environ["STUDIO_INSECURE_COOKIE"] = "1"
import shutil
shutil.rmtree(os.environ["STUDIO_DATA"], ignore_errors=True)

from app.main import app as _wrapped  # noqa: E402
app = _wrapped.app

SPEC = {
    "name": "qa",
    "modules": [{"id": "answer", "template": "Answer the question from the context.\nContext: {context}\nQuestion: {question}",
                 "description": "answers a question from a context", "schema_fields": [{"name": "answer"}]}],
    "edges": [],
    "evaluate": {"scorers": [{"type": "exact_match", "field": "answer"}], "objective": {"accuracy": 1.0}},
    "optimizer": {"rounds": 2, "minibatch": 3, "feedback": "plain", "no_improvement_rounds": None, "holdout_frac": 0.25},
}
ROWS = [{"context": f"Fact {i}: the capital of Land{i} is City{i}.", "question": f"What is the capital of Land{i}?", "answer": f"City{i}"} for i in range(16)]


async def _flow():
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/projects"); assert r.status_code == 401
            r = await c.post("/auth/signup", json={"email": "a@b.co", "password": "password1"}); assert r.status_code == 200, r.text
            r = await c.post("/projects", json=SPEC); pid = r.json()["id"]
            r = await c.get("/projects"); assert [p["id"] for p in r.json()] == [pid]
            # default names increment instead of colliding; delete removes the project
            p2 = (await c.post("/projects")).json(); p3 = (await c.post("/projects")).json()
            assert p2["spec"]["name"] == "untitled" and p3["spec"]["name"] == "untitled 2"
            await c.delete(f"/projects/{p2['id']}"); await c.delete(f"/projects/{p3['id']}")
            r = await c.get("/projects"); assert [p["id"] for p in r.json()] == [pid]
            assert (await c.get(f"/projects/{p2['id']}")).status_code == 404
            body = "\n".join(json.dumps(x) for x in ROWS).encode()
            r = await c.post(f"/projects/{pid}/datasets", files={"file": ("d.jsonl", body, "application/json")}); assert r.status_code == 200, r.text
            did = r.json()["id"]; assert r.json()["columns"] == ["context", "question", "answer"] and r.json()["label_column"] == "answer"
            r = await c.post(f"/projects/{pid}/validate", json={"dataset_id": did}); assert not r.json()["ok"]  # no mapping yet
            await c.put(f"/datasets/{did}/mapping", json={"input_map": {"context": "context", "question": "question"}, "label_column": "answer"})
            r = await c.post(f"/projects/{pid}/validate", json={"dataset_id": did}); assert r.json()["ok"], r.json()
            r = await c.post(f"/projects/{pid}/pilot", json={"dataset_id": did, "rows": 6, "mock": True}); assert r.status_code == 200, r.text
            j = r.json(); assert "baseline" in j["pilot"] and j["cost"]["usd"]["total"] >= 0 and "rewrite" in j["pilot"]
            r = await c.post(f"/projects/{pid}/runs", json={"dataset_id": did, "mock": True}); rid = r.json()["id"]
            for _ in range(200):
                await asyncio.sleep(0.05)
                st = (await c.get(f"/runs/{rid}")).json()
                if st["live"]["state"] in ("done", "failed", "stopped"):
                    break
            assert st["live"]["state"] == "done", st
            assert st["summary"]["holdout"]["best"] is not None and st["live"]["train_rows"] == 12 and st["live"]["holdout_rows"] == 4
            tr = (await c.get(f"/runs/{rid}/tree")).json(); assert len(tr["nodes"]) >= 2
            ev = (await c.get(f"/runs/{rid}/events")).json(); assert ev["next"] > 0
            nid = tr["nodes"][-1]["id"]; nd = (await c.get(f"/runs/{rid}/nodes/{nid}")).json(); assert "node" in nd
            r = await c.get(f"/projects/{pid}/cost", params={"dataset_id": did}); assert r.json()["assumptions"]["full_rows"] == 12
            # endpoints & keys: a custom OpenAI-compatible endpoint shows its models in the catalog and routes to itself
            os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", "test-token")  # house Bedrock key present
            r = await c.get("/models"); assert r.json()["house_keys"] is True and all(m["source"] == "house" for m in r.json()["models"])
            r = await c.post("/credentials", json={"provider": "custom", "label": "my vllm", "config": {"base_url": "http://127.0.0.1:1/v1", "api_key": "sk-x"}, "models": ["llama-3.3-70b"]})
            cid = r.json()["id"]
            r = await c.get("/credentials"); cr = r.json()["credentials"][0]
            assert cr["config"]["api_key"].startswith("•••") and cr["models"] == ["llama-3.3-70b"]
            r = await c.get("/models"); m = r.json()["models"][0]; assert m["id"] == "llama-3.3-70b" and m["source"] == cid
            r = await c.post(f"/credentials/{cid}/test"); assert r.json()["ok"] is False and "llama" in r.json()["model"]  # nothing listens on port 1
            r = await c.post("/credentials", json={"provider": "custom", "config": {"base_url": "x"}}); assert r.status_code == 400
            await c.delete(f"/credentials/{cid}"); assert (await c.get("/credentials")).json()["credentials"] == []
            # usage log: every call of the pilot and the run was recorded for this user
            u = (await c.get("/usage")).json()
            assert u["totals"]["calls"] > 0 and u["totals"]["usd"] == 0 and {m["source"] for m in u["by_model"]} == {"mock"}
            # tier: beginner by default -> nova micro + lite on house keys, caps 4/4; q above the cap is clamped on start
            r = await c.get("/models"); j = r.json()
            assert j["tier"] == "beginner" and j["max_q"] == 4 and [m["id"] for m in j["models"] if m["source"] == "house"] == ["us.amazon.nova-micro-v1:0", "us.amazon.nova-lite-v1:0"]
            spec2 = {**SPEC, "optimizer": {**SPEC["optimizer"], "engine": "bo", "bo": {"q": 9, "pca": 4, "acquisition": "qei"}}}
            await c.put(f"/projects/{pid}", json=spec2)
            r = await c.post(f"/projects/{pid}/runs", json={"dataset_id": did, "mock": True}); assert any("q 9 -> 4" in n for n in r.json()["notes"])
            for _ in range(200):
                await asyncio.sleep(0.05)
                if (await c.get(f"/runs/{r.json()['id']}")).json()["live"]["state"] != "running": break
            # a free-tier user with no credentials sees no house models and cannot start a live run
            app.state.db.execute("update users set tier='free'"); app.state.db.commit()
            r = await c.get("/models"); assert r.json()["house_keys"] is False and r.json()["models"] == [] and r.json()["max_concurrency"] == 1
            r = await c.post(f"/projects/{pid}/runs", json={"dataset_id": did, "mock": False}); assert r.status_code == 403 and "Endpoints" in r.text
            # tutorial sample: downloadable, and loadable straight into a project
            r = await c.get("/sample/tickets.jsonl"); assert r.status_code == 200 and r.text.count("\n") == 50
            r = await c.post(f"/projects/{pid}/datasets/sample"); assert r.json()["n_rows"] == 50 and r.json()["label_column"] == "queue"
            await c.post("/auth/logout"); r = await c.get("/auth/me"); assert r.status_code == 401


def test_full_api_flow():
    asyncio.run(_flow())
