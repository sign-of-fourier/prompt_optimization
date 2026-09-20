"""FastAPI app. Mounted by nginx at /api/ on the studio host, or /studio/api/ under a marketing site (routes here are
unprefixed). `uvicorn app.main:app --port 8100`."""
from __future__ import annotations

import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, credentials as C, db, runs as R, tiers
from .clients import Access, make_client
from .compile import build_task
from .datasets import columns, flatten, parse_upload, to_dataset
from .models import ProjectSpec, ValidationReport
from . import brand
from .validation import pilot as run_pilot, project_cost, validate_static

DATASETS_DIR = db.DATA_DIR / "datasets"
MOCK_DEFAULT = os.environ.get("STUDIO_MOCK") == "1"


def load_env():
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env()
    app.state.db = db.connect()
    app.state.runs = R.RunManager()
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    yield
    app.state.db.close()


app = FastAPI(title=brand.name(), lifespan=lifespan)


# ---- auth -------------------------------------------------------------------------------

class Credentials(BaseModel):
    email: str
    password: str


@app.post("/auth/signup")
def signup(c: Credentials, request: Request, response: Response):
    u = auth.signup(request.app.state.db, c.email, c.password)
    auth.start_session(request.app.state.db, response, u["id"])
    return u


@app.post("/auth/login")
def login(c: Credentials, request: Request, response: Response):
    u = auth.login(request.app.state.db, c.email, c.password)
    auth.start_session(request.app.state.db, response, u["id"])
    return u


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.app.state.db, request, response)
    return {"ok": True}


@app.get("/auth/me")
def me(user=auth.User):
    return user


def _access(request: Request, user: dict) -> Access:
    t = tiers.tier_of(user.get("tier"))
    return Access(C.load_credentials(request.app.state.db, user["id"]), house_keys=t["house_keys"], house_models=t["house_models"],
                  max_concurrency=t["max_concurrency"], max_q=t["max_q"], tier=user.get("tier") or tiers.DEFAULT_TIER, user_id=user["id"])


def _caps(spec: ProjectSpec, access: Access) -> list[str]:
    """Clamp the spec's parallelism to the tier; returns what was changed."""
    notes = []
    o = spec.optimizer
    if o.bo.q > access.max_q:
        notes.append(f"BO batch q {o.bo.q} -> {access.max_q} (tier cap)"); o.bo.q = access.max_q
    if o.parents_per_round > access.max_q:
        notes.append(f"parents per round {o.parents_per_round} -> {access.max_q} (tier cap)"); o.parents_per_round = access.max_q
    return notes


@app.get("/models")
def models(request: Request, user=auth.User):
    a = _access(request, user)
    return {"models": a.catalog(), "mock": MOCK_DEFAULT, "house_keys": a.house_keys, "own_keys": tiers.allows(user.get("tier"), "own_keys"),
            "tier": a.tier, "max_concurrency": a.max_concurrency, "max_q": a.max_q}


@app.get("/usage")
def usage(request: Request, days: int = 30, user=auth.User):
    return db.usage_summary(request.app.state.db, user["id"], days)


# ---- endpoints & credentials --------------------------------------------------------------

class CredentialBody(BaseModel):
    provider: str
    label: str = ""
    config: dict[str, Any] = {}
    models: list[str] | None = None


@app.get("/credentials")
def list_credentials(request: Request, user=auth.User):
    return {"credentials": C.list_credentials(request.app.state.db, user["id"]), "providers": C.PROVIDERS, "default_models": C.DEFAULT_MODELS}


@app.post("/credentials")
async def add_credential(body: CredentialBody, request: Request, user=auth.User):
    if not tiers.allows(user.get("tier"), "own_keys"):
        raise HTTPException(403, "your tier cannot add endpoints")
    return C.add_credential(request.app.state.db, user["id"], body.provider, body.label, body.config, body.models)


@app.post("/credentials/{cid}/test")
async def test_credential(cid: str, request: Request, user=auth.User):
    cred = next((c for c in C.load_credentials(request.app.state.db, user["id"]) if c["id"] == cid), None)
    if not cred:
        raise HTTPException(404, "credential not found")
    r = await C.test_credential(cred)
    if r.get("ok"):
        db.usage_logger(request.app.state.db, user["id"], user.get("tier"))({"purpose": "test", "model": r["model"], "source": cid, "input_tokens": r["tokens"],
                                                                             "output_tokens": 0, "cached": 0, "latency_s": None, "usd": 0.0})
    return r


class ModelsBody(BaseModel):
    models: list[str]


@app.put("/credentials/{cid}/models")
def set_models(cid: str, body: ModelsBody, request: Request, user=auth.User):
    C.update_models(request.app.state.db, user["id"], cid, body.models)
    return {"ok": True}


@app.delete("/credentials/{cid}")
def delete_credential(cid: str, request: Request, user=auth.User):
    C.delete_credential(request.app.state.db, user["id"], cid)
    return {"ok": True}


# ---- projects ---------------------------------------------------------------------------

def _project(request: Request, pid: str, user: dict) -> dict:
    p = db.row(request.app.state.db.execute("select * from projects where id=? and user_id=?", (pid, user["id"])).fetchone())
    if not p:
        raise HTTPException(404, "project not found")
    return p


@app.get("/projects")
def list_projects(request: Request, user=auth.User):
    return db.rows(request.app.state.db.execute("select id, name, updated from projects where user_id=? order by updated desc", (user["id"],)))


def _unique_name(con, user_id: str, base: str) -> str:
    """'untitled', then 'untitled 2', 'untitled 3', ... among this user's projects."""
    taken = {r[0] for r in con.execute("select name from projects where user_id=?", (user_id,))}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


@app.post("/projects")
def create_project(request: Request, spec: ProjectSpec = Body(default=None), user=auth.User):
    spec = spec or ProjectSpec(name="untitled", modules=[])
    spec.name = _unique_name(request.app.state.db, user["id"], spec.name or "untitled")
    _coerce_models(spec, _access(request, user))
    pid = db.new_id()
    request.app.state.db.execute("insert into projects values (?,?,?,?,?)", (pid, user["id"], spec.name, spec.model_dump_json(), db.now()))
    request.app.state.db.commit()
    return {"id": pid, "spec": spec.model_dump()}


@app.get("/projects/{pid}")
def get_project(pid: str, request: Request, user=auth.User):
    p = _project(request, pid, user)
    ds = db.rows(request.app.state.db.execute("select id, name, columns, n_rows, input_map, label_column, created from datasets where project_id=? order by created desc", (pid,)))
    return {**p, "datasets": ds}


@app.put("/projects/{pid}")
def update_project(pid: str, spec: ProjectSpec, request: Request, user=auth.User):
    _project(request, pid, user)
    request.app.state.db.execute("update projects set name=?, spec=?, updated=? where id=?", (spec.name, spec.model_dump_json(), db.now(), pid))
    request.app.state.db.commit()
    return {"ok": True}


@app.delete("/projects/{pid}")
def delete_project(pid: str, request: Request, user=auth.User):
    _project(request, pid, user)
    con = request.app.state.db
    # stop anything still running, then drop rows and on-disk artifacts (dataset files, run dirs)
    for (rid,) in con.execute("select id from runs where project_id=?", (pid,)):
        request.app.state.runs.stop(rid)
        shutil.rmtree(R.RUNS_DIR / rid, ignore_errors=True)
    for (path,) in con.execute("select path from datasets where project_id=?", (pid,)):
        Path(path).unlink(missing_ok=True)
    for t in ("runs", "validations", "datasets"):
        con.execute(f"delete from {t} where project_id=?", (pid,))
    con.execute("delete from projects where id=?", (pid,)); con.commit()
    return {"ok": True}


# ---- datasets ---------------------------------------------------------------------------

def _dataset(request: Request, did: str, user: dict) -> dict:
    d = db.row(request.app.state.db.execute("select d.* from datasets d join projects p on p.id=d.project_id where d.id=? and p.user_id=?", (did, user["id"])).fetchone())
    if not d:
        raise HTTPException(404, "dataset not found")
    return d


def _rows(d: dict) -> list[dict]:
    return json.loads(Path(d["path"]).read_text())


@app.post("/projects/{pid}/datasets")
async def upload_dataset(pid: str, request: Request, file: UploadFile, user=auth.User):
    _project(request, pid, user)
    try:
        rows = flatten(parse_upload(file.filename or "data.jsonl", await file.read()))
    except Exception as e:
        raise HTTPException(400, f"could not parse file: {e}")
    if not rows:
        raise HTTPException(400, "the file has no rows")
    did = db.new_id()
    path = DATASETS_DIR / f"{did}.json"
    path.write_text(json.dumps(rows, default=str))
    cols = columns(rows)
    label = "answer" if "answer" in cols else ("label" if "label" in cols else None)
    request.app.state.db.execute("insert into datasets values (?,?,?,?,?,?,?,?,?)",
                                 (did, pid, file.filename, str(path), json.dumps(cols), len(rows), json.dumps({}), label, db.now()))
    request.app.state.db.commit()
    return {"id": did, "name": file.filename, "columns": cols, "n_rows": len(rows), "label_column": label, "preview": rows[:5]}


SAMPLE = Path(__file__).resolve().parent.parent / "sample" / "tickets.jsonl"


@app.get("/sample/tickets.jsonl")
def sample_download():
    from fastapi.responses import FileResponse
    return FileResponse(SAMPLE, media_type="application/jsonl", filename="tickets.jsonl")


@app.post("/projects/{pid}/datasets/sample")
def load_sample(pid: str, request: Request, user=auth.User):
    """The tutorial dataset, already mapped ({message} <- message, label queue) when the project uses that placeholder."""
    p = _project(request, pid, user)
    rows = flatten(parse_upload("tickets.jsonl", SAMPLE.read_bytes()))
    did = db.new_id()
    path = DATASETS_DIR / f"{did}.json"
    path.write_text(json.dumps(rows))
    cols = columns(rows)
    spec = ProjectSpec.model_validate(p["spec"])
    phs = {ph for m in spec.modules for ph in __import__("bpto").Prompt(template=m.template).placeholders} if spec.modules else set()
    input_map = {"message": "message"} if "message" in phs or not phs else {}
    request.app.state.db.execute("insert into datasets values (?,?,?,?,?,?,?,?,?)",
                                 (did, pid, "tickets.jsonl (sample)", str(path), json.dumps(cols), len(rows), json.dumps(input_map), "queue", db.now()))
    request.app.state.db.commit()
    return {"id": did, "name": "tickets.jsonl (sample)", "columns": cols, "n_rows": len(rows), "label_column": "queue", "input_map": input_map}


class Mapping(BaseModel):
    input_map: dict[str, str]
    label_column: str | None = None


@app.put("/datasets/{did}/mapping")
def set_mapping(did: str, m: Mapping, request: Request, user=auth.User):
    _dataset(request, did, user)
    request.app.state.db.execute("update datasets set input_map=?, label_column=? where id=?", (json.dumps(m.input_map), m.label_column, did))
    request.app.state.db.commit()
    return {"ok": True}


@app.get("/datasets/{did}")
def get_dataset(did: str, request: Request, user=auth.User):
    d = _dataset(request, did, user)
    return {**{k: v for k, v in d.items() if k != "path"}, "preview": _rows(d)[:20]}


# ---- validation -------------------------------------------------------------------------

class ValidateBody(BaseModel):
    dataset_id: str
    rows: int = 12          # pilot sample
    mock: bool | None = None


def _spec_and_data(request, pid, body, user):
    p = _project(request, pid, user)
    d = _dataset(request, body.dataset_id, user)
    spec = ProjectSpec.model_validate(p["spec"])
    return spec, d, _rows(d)


@app.post("/projects/{pid}/validate")
def validate(pid: str, body: ValidateBody, request: Request, user=auth.User):
    spec, d, rows = _spec_and_data(request, pid, body, user)
    spec.evaluate.label_column = d["label_column"]
    rep = validate_static(spec, rows, d["input_map"])
    return {"report": rep.model_dump(), "ok": rep.ok}


@app.post("/projects/{pid}/pilot")
async def pilot(pid: str, body: ValidateBody, request: Request, user=auth.User):
    spec, d, rows = _spec_and_data(request, pid, body, user)
    spec.evaluate.label_column = d["label_column"]
    rep = validate_static(spec, rows, d["input_map"])
    if not rep.ok:
        raise HTTPException(400, "fix the static validation errors first")
    mock = MOCK_DEFAULT if body.mock is None else body.mock
    access = _access(request, user)
    if not mock:
        _check_models(spec, access)
    _caps(spec, access)
    from bpto import Budget
    client = make_client(spec.eval_model, budget=Budget(max_calls=body.rows * 3 * spec.max_steps + 20, prices=__import__("app.clients", fromlist=["prices"]).prices()),
                         mock=__import__("app.mock", fromlist=["mock_client"]).mock_client() if mock else None, access=access,
                         on_call=db.usage_logger(request.app.state.db, user["id"], access.tier, pid), purpose="pilot")
    task = build_task(spec, to_dataset(rows, d["input_map"], d["label_column"]), client)
    out = await run_pilot(spec, task, rows=body.rows, rep=rep)
    cost = project_cost(spec, len(rows), avg_steps=out.get("avg_steps"), tokens_per_module=out.get("tokens_per_module") or None,
                        avg_in=out.get("avg_input_tokens"), avg_out=out.get("avg_output_tokens"))
    vid = db.new_id()
    request.app.state.db.execute("insert into validations values (?,?,?,?,?,?,?)",
                                 (vid, pid, d["id"], rep.model_dump_json(), json.dumps(out, default=str), json.dumps(cost), db.now()))
    request.app.state.db.commit()
    return {"report": rep.model_dump(), "ok": rep.ok, "pilot": out, "cost": cost, "validation_id": vid}


@app.get("/projects/{pid}/cost")
def cost(pid: str, dataset_id: str, request: Request, user=auth.User):
    p = _project(request, pid, user)
    d = _dataset(request, dataset_id, user)
    spec = ProjectSpec.model_validate(p["spec"])
    v = db.row(request.app.state.db.execute("select pilot from validations where project_id=? and dataset_id=? order by created desc limit 1", (pid, dataset_id)).fetchone())
    pl = (v or {}).get("pilot") or {}
    return project_cost(spec, d["n_rows"], avg_steps=pl.get("avg_steps"), tokens_per_module=pl.get("tokens_per_module") or None,
                        avg_in=pl.get("avg_input_tokens"), avg_out=pl.get("avg_output_tokens"))


# ---- runs -------------------------------------------------------------------------------

class RunBody(BaseModel):
    dataset_id: str
    mock: bool | None = None


@app.post("/projects/{pid}/runs")
async def start_run(pid: str, body: RunBody, request: Request, user=auth.User):
    spec, d, rows = _spec_and_data(request, pid, body, user)
    spec.evaluate.label_column = d["label_column"]
    rep = validate_static(spec, rows, d["input_map"])
    if not rep.ok:
        raise HTTPException(400, {"message": "validation errors", "issues": [i.model_dump() for i in rep.issues if i.level == "error"]})
    v = db.row(request.app.state.db.execute("select pilot from validations where project_id=? and dataset_id=? order by created desc limit 1", (pid, d["id"])).fetchone())
    mock = MOCK_DEFAULT if body.mock is None else body.mock
    access = _access(request, user)
    if not mock:
        _check_models(spec, access)
    notes = _caps(spec, access)
    rid = db.new_id()
    con = request.app.state.db
    con.execute("insert into runs values (?,?,?,?,?,?,?,?,?)", (rid, pid, d["id"], "running", str(R.RUNS_DIR / rid), db.now(), None, None, None))
    con.commit()
    request.app.state.runs.start(con, rid, spec, rows, d["input_map"], d["label_column"], mock=mock, pilot=(v or {}).get("pilot"), access=access,
                                 on_call=db.usage_logger(con, user["id"], access.tier, pid, rid))
    return {"id": rid, "notes": notes}


def _coerce_models(spec: ProjectSpec, access: Access) -> None:
    """A fresh project may name models the user cannot run (stale client defaults, a downgraded tier):
    point those at a model they can, so the first run is not refused for a setting they never chose."""
    usable = [m["id"] for m in access.catalog()]
    if not usable:
        return
    fallback = spec.eval_model if access.can_use(spec.eval_model) else usable[0]
    spec.eval_model = fallback
    if not access.can_use(spec.optimizer.reflect_model):
        spec.optimizer.reflect_model = fallback
    for m in spec.modules:
        if m.model and not access.can_use(m.model):
            m.model = None
    for sc in spec.evaluate.scorers:
        if sc.judge_model and not access.can_use(sc.judge_model):
            sc.judge_model = None
    if spec.optimizer.critic_model and not access.can_use(spec.optimizer.critic_model):
        spec.optimizer.critic_model = None


def _check_models(spec: ProjectSpec, access: Access) -> None:
    used = {spec.eval_model, spec.optimizer.reflect_model, *(m.model for m in spec.modules if m.model),
            *(s.judge_model for s in spec.evaluate.scorers if s.judge_model), *([spec.optimizer.critic_model] if spec.optimizer.critic_model else [])}
    blocked = sorted(m for m in used if not access.can_use(m))
    if blocked:
        where = {spec.eval_model: "evaluation model (canvas)", spec.optimizer.reflect_model: "reflection model (Optimizer ⚙)",
                 spec.optimizer.critic_model: "critic model (Optimizer ⚙)"}
        where.update({m.model: f"step '{m.id}' model" for m in spec.modules if m.model})
        where.update({s.judge_model: f"judge model (scorer {s.type})" for s in spec.evaluate.scorers if s.judge_model})
        raise HTTPException(403, "your plan cannot run " + "; ".join(f"{m} as the {where.get(m, 'model')}" for m in blocked) +
                            ". Pick a model from the dropdown there, or add your own endpoint under Endpoints & keys" +
                            ("" if access.house_keys else ", or upgrade to run on the site's keys"))


def _run(request: Request, rid: str, user: dict) -> dict:
    r = db.row(request.app.state.db.execute("select r.* from runs r join projects p on p.id=r.project_id where r.id=? and p.user_id=?", (rid, user["id"])).fetchone())
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.get("/projects/{pid}/runs")
def list_runs(pid: str, request: Request, user=auth.User):
    _project(request, pid, user)
    return db.rows(request.app.state.db.execute("select id, dataset_id, status, started, finished, summary, error from runs where project_id=? order by started desc", (pid,)))


@app.get("/runs/{rid}")
def run_status(rid: str, request: Request, user=auth.User):
    r = _run(request, rid, user)
    return {**r, "live": R.read_status(rid)}


@app.get("/runs/{rid}/tree")
def run_tree(rid: str, request: Request, user=auth.User):
    _run(request, rid, user)
    return R.read_tree(rid)


@app.get("/runs/{rid}/nodes/{nid}")
def run_node(rid: str, nid: str, request: Request, user=auth.User):
    _run(request, rid, user)
    n = R.read_node(rid, nid)
    if not n:
        raise HTTPException(404, "node not found")
    return n


@app.get("/runs/{rid}/events")
def run_events(rid: str, request: Request, after: int = 0, user=auth.User):
    _run(request, rid, user)
    ev = R.read_events(rid, after)
    return {"events": ev, "next": after + len(ev)}


@app.post("/runs/{rid}/stop")
async def stop_run(rid: str, request: Request, user=auth.User):
    _run(request, rid, user)
    return {"stopped": request.app.state.runs.stop(rid)}


# dev only: serve the built frontend and accept the nginx-style prefixes (/studio/api, /api) from the same process
WEB = Path(__file__).resolve().parent.parent / "web" / "dist"
if WEB.exists():
    app.mount("/studio", StaticFiles(directory=WEB, html=True), name="web")


class _StripPrefix:
    def __init__(self, app, prefixes=("/studio/api", "/api")):
        self.app, self.prefixes = app, prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for prefix in self.prefixes:
                if scope["path"].startswith(prefix + "/") or scope["path"] == prefix:
                    rest = scope["path"][len(prefix):] or "/"
                    scope = {**scope, "path": rest, "raw_path": rest.encode()}
                    break
        await self.app(scope, receive, send)


app = _StripPrefix(app)  # type: ignore[assignment]
