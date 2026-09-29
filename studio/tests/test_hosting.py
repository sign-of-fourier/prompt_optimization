"""Hosting (beta upsell): the studio copies a version to the serving box (app/serve_box.py), the box answers it, and
its ledger comes back as traces and usage. Offline: the box runs in-process behind an httpx ASGITransport."""
import asyncio
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient

from test_api import KB, RAG_SPEC, ROWS, SPEC, app   # same app, same data dir, same fixtures

from app import hosting as H
from app import serve_box


@pytest.fixture
def box(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVE_BOX_URL", "http://box")
    monkeypatch.setenv("SERVE_TOKEN", "t0ken")
    monkeypatch.setenv("SERVE_DATA", str(tmp_path / "serve"))
    monkeypatch.setenv("SERVE_MOCK", "1")
    monkeypatch.setenv("HOSTING_PULL_S", "3600")          # the test pulls by hand
    monkeypatch.setenv("STUDIO_BEDROCK_ROLE", "1")        # house models visible, as on a box with an instance role
    monkeypatch.setattr(H, "_transport", ASGITransport(app=serve_box.app))
    return tmp_path / "serve"


def _box_client():
    return AsyncClient(transport=ASGITransport(app=serve_box.app), base_url="http://box")


async def _signup(c, email):
    r = await c.post("/auth/signup", json={"email": email, "password": "password1"}); assert r.status_code == 200, r.text
    return (await c.get("/auth/me")).json()["id"]


async def _hosting_flow(root):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as a, \
                   AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as b, _box_client() as bx:
            ua = await _signup(a, "host-a@b.co")
            pid = (await a.post("/projects", json=SPEC)).json()["id"]
            body = "\n".join(json.dumps(x) for x in ROWS).encode()
            did = (await a.post(f"/projects/{pid}/datasets", files={"file": ("d.jsonl", body, "application/json")})).json()["id"]
            await a.put(f"/datasets/{did}/mapping", json={"input_map": {"context": "context", "question": "question"}, "label_column": "answer"})
            rid = (await a.post(f"/projects/{pid}/runs", json={"dataset_id": did, "mock": True})).json()["id"]
            for _ in range(200):
                await asyncio.sleep(0.05)
                st = (await a.get(f"/runs/{rid}")).json()
                if st["live"]["state"] in ("done", "failed", "stopped"):
                    break
            assert st["live"]["state"] == "done", st
            v = (await a.post(f"/projects/{pid}/versions", json={"run_id": rid})).json()
            k = (await a.post("/keys", json={"label": "hosted"})).json()
            KA = {"Authorization": f"Bearer {k['key']}"}

            # host it: the version and the key hash land in the customer's folder, nothing else does
            h = (await a.post(f"/versions/{v['id']}/host")).json()
            assert h["hosted"] and h["url"].endswith(f"/serve/v/{v['id']}/run")
            assert (root / "tenants" / ua / "versions" / v["id"] / "version.json").exists()
            assert sorted(p.name for p in root.iterdir()) == ["keys.json", "tenants"]
            assert (await bx.get(f"/v/{v['id']}", headers=KA)).json()["inputs"] == ["context", "question"]

            # the invariant, across machines: the box answers with the bytes evaluation produced for that row
            nd = (await a.get(f"/runs/{rid}/nodes/{st['summary']['best']['id']}")).json()["node"]
            scored = nd["evaluation"]["per_example"][0]
            row = ROWS[int(scored["example_id"])]
            r = await bx.post(f"/v/{v['id']}/run", headers=KA, json={"inputs": {"context": row["context"], "question": row["question"]}})
            assert r.status_code == 200, r.text
            served = r.json()
            assert served["output"] == scored["output"] and served["parsed"] == scored["parsed"] and served["path"] == ["answer"]
            assert (await bx.post(f"/v/{v['id']}/run", headers=KA, json={"inputs": {"context": "x"}})).status_code == 400

            # the ledger comes home: the hosted request is a studio trace, and its model call is a serve usage row
            assert (await a.post("/hosting/sync")).json()["imported"] >= 2
            tr = (await a.get(f"/projects/{pid}/traces")).json()
            assert served["trace_id"] in {t["id"] for t in tr}
            assert (await a.post("/hosting/sync")).json()["imported"] == 0          # the cursor moved: nothing twice
            con = app.state.db
            assert con.execute("select count(*) from usage_log where user_id=? and purpose='serve'", (ua,)).fetchone()[0] >= 1

            # another customer: their own folder; A's key cannot reach B's version, nor B's A's
            ub = await _signup(b, "host-b@b.co")
            pb = (await b.post("/projects", json=SPEC)).json()["id"]
            vb = (await b.post(f"/projects/{pb}/versions", json={})).json()
            kb = (await b.post("/keys", json={})).json()
            KB_ = {"Authorization": f"Bearer {kb['key']}"}
            assert (await b.post(f"/versions/{vb['id']}/host")).status_code == 200
            assert (root / "tenants" / ub / "versions" / vb["id"]).is_dir()
            ask = {"inputs": {"context": "c", "question": "q"}}
            assert (await bx.post(f"/v/{vb['id']}/run", headers=KA, json=ask)).status_code == 404
            assert (await bx.post(f"/v/{v['id']}/run", headers=KB_, json=ask)).status_code == 404
            assert (await bx.post(f"/v/{vb['id']}/run", headers=KB_, json=ask)).status_code == 200
            assert (await a.post(f"/versions/{vb['id']}/host")).status_code == 404   # nor host someone else's version
            await b.post("/hosting/sync")
            assert all(t["user_id"] == ub for t in (await b.get(f"/projects/{pb}/traces")).json())

            # a new key reaches the box; revoking reaches it first
            k2 = (await a.post("/keys", json={"label": "second"})).json()
            assert "hosting_warning" not in k2
            K2 = {"Authorization": f"Bearer {k2['key']}"}
            assert (await bx.post(f"/v/{v['id']}/run", headers=K2, json=ask)).status_code == 200
            await a.delete(f"/keys/{k['id']}")
            assert (await bx.post(f"/v/{v['id']}/run", headers=KA, json=ask)).status_code == 401

            # unhost: gone from the box, still a version in the studio
            assert (await a.delete(f"/versions/{v['id']}/host")).json()["hosted"] is False
            assert (await bx.post(f"/v/{v['id']}/run", headers=K2, json=ask)).status_code == 404
            assert (await a.get(f"/versions/{v['id']}")).status_code == 200

            # the box replaced: a new, empty box gets every hosted version and key back from the studio's records
            import shutil
            shutil.rmtree(root)
            assert (await bx.post(f"/v/{vb['id']}/run", headers=KB_, json=ask)).status_code == 401
            out = await H.resync(con)
            assert vb["id"] in out["pushed"] and not out["failed"]
            assert (await bx.post(f"/v/{vb['id']}/run", headers=KB_, json=ask)).status_code == 200
            # unhosted stays unhosted; A has nothing hosted, so the new box does not even know A's keys
            assert (await bx.post(f"/v/{v['id']}/run", headers=K2, json=ask)).status_code in (401, 404)

            # a customer leaving: their folder and key hashes, and nobody else's
            await H.delete_tenant(con, ub)
            assert not (root / "tenants" / ub).exists() and (root / "tenants").exists()
            assert all(r["user_id"] != ub for r in json.loads((root / "keys.json").read_text()).values())


def test_hosting_flow(box):
    asyncio.run(_hosting_flow(box))


async def _hosted_retrieval_flow():
    """A search step travels with its version: the box answers from the shipped passages exactly as the studio does."""
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c, _box_client() as bx:
            await _signup(c, "host-rag@b.co")
            cid = (await c.post("/corpora", json={"name": "kb"})).json()["id"]
            await c.post(f"/corpora/{cid}/documents?mock=true", files=[("files", (n, b)) for n, b in KB.items()])
            spec = {**RAG_SPEC, "steps": [{**RAG_SPEC["steps"][0], "tunables": {"corpus": cid, "k": 1}}]}
            pid = (await c.post("/projects", json=spec)).json()["id"]
            v = (await c.post(f"/projects/{pid}/versions", json={"label": "rag"})).json()
            k = (await c.post("/keys", json={})).json()
            KH = {"Authorization": f"Bearer {k['key']}"}
            assert (await c.post(f"/versions/{v['id']}/host")).status_code == 200
            q = {"question": "When are monthly plans refunded to the card?"}
            here = (await c.post(f"/v/{v['id']}/run", headers=KH, json={"inputs": q, "mock": True})).json()
            there = (await bx.post(f"/v/{v['id']}/run", headers=KH, json={"inputs": q})).json()
            assert there["steps"]["rag_sources"] == "refunds.md#Monthly plans"
            assert there["steps"] == here["steps"] and there["output"] == here["output"]


def test_hosted_retrieval(box):
    asyncio.run(_hosted_retrieval_flow())


def test_box_refuses_what_it_should(box):
    async def go():
        async with _box_client() as bx:
            assert (await bx.get("/health")).json()["ok"]
            assert (await bx.put("/internal/keys/k1", json={"hash": "h", "user_id": "u"})).status_code == 401
            bad = {"x-serve-token": "nope"}
            assert (await bx.get("/internal/tenants/u/ledger", headers=bad)).status_code == 401
            ok = {"x-serve-token": "t0ken"}
            assert (await bx.get("/internal/tenants/..%2F..%2Fetc/ledger", headers=ok)).status_code in (400, 404)
            assert (await bx.delete("/internal/tenants/a.b", headers=ok)).status_code == 400
            assert (await bx.post("/v/x/run", json={"inputs": {}})).status_code == 401
    asyncio.run(go())


def test_hosting_refuses_steps_that_sign_in(box):
    async def go():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                await _signup(c, "host-hub@b.co")
                spec = {**SPEC, "steps": [{"id": "hub", "manifest": "hubspot-contact@1.0.0", "inputs": {"email": "question"}}]}
                pid = (await c.post("/projects", json=spec)).json()["id"]
                v = (await c.post(f"/projects/{pid}/versions", json={})).json()
                r = await c.post(f"/versions/{v['id']}/host")
                assert r.status_code == 502 and "signs in" in r.text
    asyncio.run(go())
