import asyncio
import io
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient

import tempfile
os.environ["STUDIO_DATA"] = os.path.join(tempfile.gettempdir(), "studio_test_data")
os.environ["STUDIO_INSECURE_COOKIE"] = "1"
os.environ["STUDIO_V0"] = "1"   # PLAN.md v0 pieces are flag-gated; the tests run with them on
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
            # bundles: the library lists the checked-in examples; a clone is an ordinary project with its dataset attached
            lib = (await c.get("/examples")).json()
            ex = lib["entries"]
            assert [e["slug"] for e in ex] == ["ticket-triage", "ticket-triage-compress", "ticket-triage-entitlement",
                                               "hubspot-contact-lookup"]
            assert ex[0]["rows"] == 50 and ex[1]["goal"] == "compress" and ex[2]["rows"] == 120
            assert ex[2]["modules"] == 1 and ex[2]["steps"] == 1 and ex[0]["modules"] == 2 and ex[0]["steps"] == 0
            # the library screen filters on these, and the entitlement entry says what it needs before it will run
            assert "classify" in lib["tags"] and all(e["tags"] and e["description"] and e["updated"] for e in ex)
            assert ex[2]["requires"] and not ex[0]["requires"]
            # the connector walkthrough: a step wired to a real third-party API, unauthorised until the user acts
            hs = (await c.post("/examples/hubspot-contact-lookup/clone")).json()
            hp = (await c.get(f"/projects/{hs['id']}")).json()
            assert hp["spec"]["steps"][0]["manifest"] == "hubspot-contact@1.0.0" and hp["spec"]["steps"][0]["credential"] is None
            v = (await c.post(f"/projects/{hs['id']}/validate", json={"dataset_id": hs["dataset"]["id"]})).json()
            msgs = [i["message"] for i in v["report"]["issues"] if i["level"] == "error"]
            assert not v["ok"] and any("connect your hubspot account, or paste a step key" in m for m in msgs)
            assert any("has not been enriched" in m for m in msgs)
            r = await c.post("/examples/ticket-triage/clone"); assert r.status_code == 200, r.text
            cl = r.json(); assert cl["dataset"]["n_rows"] == 50
            cp = (await c.get(f"/projects/{cl['id']}")).json()
            assert cp["name"] == "Ticket triage" and cp["spec"]["layout"]["tutorial"]["dismissed"] and cp["datasets"][0]["label_column"] == "queue"
            r = await c.post(f"/projects/{cl['id']}/validate", json={"dataset_id": cp["datasets"][0]["id"]}); assert r.json()["ok"], r.json()
            assert (await c.post("/examples/nope/clone")).status_code == 404
            r = await c.post("/examples/ticket-triage/clone"); assert r.json()["name"] == "Ticket triage 2"
            # export with a run's best prompts as the templates, then import: the round trip is a new project with the same rows
            b = (await c.get(f"/projects/{pid}/bundle", params={"run_id": rid})).json()
            best = st["summary"]["best"]["modules"]
            assert {m["id"]: m["template"] for m in b["spec"]["modules"]} == best and len(b["dataset"]["rows"]) == 16 and b["dataset"]["label_column"] == "answer"
            r = await c.post("/projects/import", json=b); assert r.status_code == 200, r.text
            ip = (await c.get(f"/projects/{r.json()['id']}")).json()
            assert ip["name"] == SPEC["name"] + " 2" and ip["datasets"][0]["n_rows"] == 16 and ip["datasets"][0]["input_map"] == b["dataset"]["input_map"]
            assert (await c.post("/projects/import", json={**b, "bundle": 99})).status_code == 400
            assert (await c.post("/projects/import", json={**b, "dataset": {**b["dataset"], "rows": None, "sample": "tickets.jsonl"}})).status_code == 400
            # a version records the spec the RUN used, not the canvas as it stands when you get round to publishing
            await c.put(f"/projects/{pid}", json={**SPEC, "max_steps": 3, "modules": [{**SPEC["modules"][0], "description": "drifted"}]})
            vdrift = (await c.post(f"/projects/{pid}/versions", json={"run_id": rid, "label": "from an old run"})).json()
            assert vdrift["spec"]["max_steps"] == 8 and vdrift["spec"]["modules"][0]["description"] != "drifted"   # the run's, not the canvas's
            await c.put(f"/projects/{pid}", json=SPEC)

            # versions (v0): promoting a run node pins the prompts that earned the score, and the fetch reproduces them
            assert (await c.get("/features")).json()["v0"] is True
            v = (await c.post(f"/projects/{pid}/versions", json={"run_id": rid})).json()
            assert v["label"] == "v2" and v["source"]["kind"] == "run_node" and v["source"]["run_id"] == rid
            assert v["score"] == st["summary"]["best"]["score"] and v["dataset_id"] == did and v["holdout"]["best_score"] is not None
            got = (await c.get(f"/versions/{v['id']}")).json()
            assert {m["id"]: m["template"] for m in got["spec"]["modules"]} == best   # the exact prompts that were scored
            assert got["fingerprint"] == v["fingerprint"] and got["spec"]["modules"][0]["model"] == got["spec"]["eval_model"]  # models pinned, not defaulted
            lst = (await c.get(f"/projects/{pid}/versions")).json()
            assert [x["id"] for x in lst] == [v["id"], vdrift["id"]] and "spec" not in lst[0]
            # a named node instead of the run's best: no hold-out, because that node was never scored on those rows
            other = next(n["id"] for n in tr["nodes"] if n["id"] != st["summary"]["best"]["id"])
            v2 = (await c.post(f"/projects/{pid}/versions", json={"run_id": rid, "node_id": other, "label": "candidate"})).json()
            assert v2["source"]["node_id"] == other and v2["holdout"] is None and v2["label"] == "candidate"
            # the canvas as it stands: unscored, and its fingerprint differs from the optimized one
            v3 = (await c.post(f"/projects/{pid}/versions", json={})).json()
            assert v3["source"] == {"kind": "canvas"} and v3["score"] is None
            assert v3["spec"]["modules"][0]["template"] == SPEC["modules"][0]["template"]
            same = {m["id"]: m["template"] for m in v3["spec"]["modules"]} == best
            assert (v3["fingerprint"] == v["fingerprint"]) is same   # the fingerprint tracks what runs, nothing else
            assert (await c.get("/versions/nope")).status_code == 404
            assert (await c.post(f"/projects/{pid}/versions", json={"run_id": "nope"})).status_code == 404
            # versions are scoped to their project and go when it does
            pv = (await c.post("/projects")).json()["id"]
            assert (await c.post(f"/projects/{pv}/versions", json={"run_id": rid})).status_code == 400
            await c.delete(f"/projects/{pv}")
            assert len((await c.get(f"/projects/{pid}/versions")).json()) == 4
            # serving (v0): a workspace key, the version's input contract, and one request through the same compile path
            k = (await c.post("/keys", json={"label": "prod"})).json()
            assert k["key"].startswith("imp_") and k["key"].startswith(k["prefix"])
            assert [x["id"] for x in (await c.get("/keys")).json()] == [k["id"]]
            ct = (await c.get(f"/v/{v['id']}")).json()
            assert ct["inputs"] == ["context", "question"] and ct["outputs"] == ["answer"] and ct["url"].endswith(f"/api/v/{v['id']}/run")
            H = {"Authorization": f"Bearer {k['key']}"}
            assert (await c.post(f"/v/{v['id']}/run", json={"inputs": {}})).status_code == 401          # the cookie is not enough
            assert (await c.post(f"/v/{v['id']}/run", json={"inputs": {}}, headers={"Authorization": "Bearer imp_nope"})).status_code == 401
            r = await c.post(f"/v/{v['id']}/run", json={"inputs": {"context": "x"}}, headers=H)
            assert r.status_code == 400 and "question" in r.text
            # the invariant: the answer a served request gives is the answer the evaluator produced for that row
            nd = (await c.get(f"/runs/{rid}/nodes/{st['summary']['best']['id']}")).json()["node"]
            scored = nd["evaluation"]["per_example"][0]
            row = ROWS[int(scored["example_id"])]
            r = await c.post(f"/v/{v['id']}/run", headers=H,
                             json={"inputs": {"context": row["context"], "question": row["question"]}, "mock": True})
            assert r.status_code == 200, r.text
            served = r.json()
            assert served["output"] == scored["output"], (served["output"], scored["output"])
            assert served["parsed"] == scored["parsed"] and served["path"] == ["answer"]
            # the request left a trace, and it carries what a later outcome will be attached to
            tr2 = (await c.get(f"/projects/{pid}/traces")).json()
            assert tr2[0]["id"] == served["trace_id"] and tr2[0]["version_id"] == v["id"] and tr2[0]["output"] == served["output"]
            assert tr2[0]["inputs"]["question"] == row["question"] and tr2[0]["error"] is None
            await c.delete(f"/keys/{k['id']}"); assert (await c.get("/keys")).json() == []
            assert (await c.post(f"/v/{v['id']}/run", json={"inputs": {}}, headers=H)).status_code == 401  # revoked
            # the flywheel (v0): served answers -> corrections -> a dataset -> a run that starts from what is deployed
            k2 = (await c.post("/keys", json={"label": "loop"})).json(); H2 = {"Authorization": f"Bearer {k2['key']}"}
            tids = []
            for row2 in ROWS[:6]:
                rr = await c.post(f"/v/{v['id']}/run", headers=H2, json={"inputs": {"context": row2["context"], "question": row2["question"]}, "mock": True})
                tids.append((rr.json()["trace_id"], row2["answer"]))
            # an outcome with neither a label nor a value is not an outcome
            assert (await c.post(f"/traces/{tids[0][0]}/outcome", json={"kind": "correction"}, headers=H2)).status_code == 400
            for tid, right in tids:
                assert (await c.post(f"/traces/{tid}/outcome", headers=H2,
                                     json={"kind": "correction", "label": right, "source": "agent"})).status_code == 200
            # corrected twice: the agent changed their mind, that is one row, not two
            await c.post(f"/traces/{tids[0][0]}/outcome", json={"kind": "correction", "label": "Elsewhere"}, headers=H2)
            tl = (await c.get(f"/projects/{pid}/traces")).json()
            assert len(tl[0]["outcomes"]) >= 1 and sum(len(t["outcomes"]) for t in tl) == 7
            assert (await c.post(f"/traces/nope/outcome", json={"label": "x"}, headers=H2)).status_code == 404
            # promote: an ordinary dataset, with provenance on every row
            pr = (await c.post(f"/projects/{pid}/datasets/from-traces", json={"version_id": v["id"]})).json()
            assert pr["n_rows"] == 6 and pr["label_column"] == "answer" and pr["input_map"] == {"context": "context", "question": "question"}
            assert set(["trace_id", "version_id", "captured_at", "label_source"]) <= set(pr["columns"])
            pd_ = (await c.get(f"/datasets/{pr['id']}")).json()
            assert pd_["preview"][0]["answer"] == "Elsewhere"   # the last correction won
            # re-optimize from the deployed prompts, not from whatever the canvas drifted to
            await c.put(f"/projects/{pid}", json={**SPEC, "modules": [{**SPEC["modules"][0], "template": "drifted {context} {question}"}]})
            rs = (await c.post(f"/projects/{pid}/versions/{v['id']}/restore")).json()
            assert {m["id"]: m["template"] for m in rs["spec"]["modules"]} == best and rs["spec"]["name"] == SPEC["name"]
            assert (await c.post(f"/projects/{pid}/validate", json={"dataset_id": pr["id"]})).json()["ok"]
            r2 = await c.post(f"/projects/{pid}/runs", json={"dataset_id": pr["id"], "mock": True}); rid2 = r2.json()["id"]
            for _ in range(300):
                await asyncio.sleep(0.05)
                st2 = (await c.get(f"/runs/{rid2}")).json()
                if st2["live"]["state"] in ("done", "failed", "stopped"): break
            assert st2["live"]["state"] == "done", st2
            assert st2["summary"]["best"]["score"] >= st2["summary"]["root_score"]   # the root here IS the deployed prompt
            await c.delete(f"/keys/{k2['id']}")
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
