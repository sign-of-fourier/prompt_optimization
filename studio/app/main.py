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

from . import auth, bundles as B, credentials as C, db, labels as L, oauth as O, runs as R, serving, steps as X, store, tiers, versions as V
from .clients import Access, make_client
from .compile import build_task
from .datasets import columns, flatten, parse_upload, to_dataset
from .models import ProjectSpec, ValidationReport
from . import brand
from .validation import pilot as run_pilot, project_cost, validate_static

DATASETS_DIR = db.DATA_DIR / "datasets"
MOCK_DEFAULT = os.environ.get("STUDIO_MOCK") == "1"
# PLAN.md's v0 pieces ship in this codebase behind one flag, off by default: what is live at impromptune.com today
# is unaffected until STUDIO_V0=1 is set on the service.
V0 = os.environ.get("STUDIO_V0") == "1"


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
    store.init(app.state.db)
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
    store.delete_project_rows(con, pid)
    con.execute("delete from projects where id=?", (pid,)); con.commit()
    return {"ok": True}


# ---- bundles: the examples library, export, import ----------------------------------------

@app.get("/examples")
def list_examples(user=auth.User):
    """The library. Curated by us - there is no submission path - so the repo is the registry and shipping a
    template is a deploy, not a moderation queue."""
    return {"entries": B.list_examples(), "tags": B.tags()}


@app.post("/examples/{slug}/clone")
def clone_example(slug: str, request: Request, user=auth.User):
    b = B.load_example(slug)
    if b is None:
        raise HTTPException(404, "no such example")
    access = _access(request, user)
    return B.import_bundle(request.app.state.db, user["id"], b, datasets_dir=DATASETS_DIR, unique_name=_unique_name,
                           coerce_models=lambda spec: _coerce_models(spec, access))


@app.get("/projects/{pid}/bundle")
def export_project(pid: str, request: Request, dataset_id: str | None = None, run_id: str | None = None, user=auth.User):
    """The project as a bundle. `run_id`: use that run's best prompts as the templates. `dataset_id`: which dataset to
    include (default: the newest)."""
    p = _project(request, pid, user)
    con = request.app.state.db
    d = _dataset(request, dataset_id, user) if dataset_id else db.row(con.execute("select * from datasets where project_id=? order by created desc limit 1", (pid,)).fetchone())
    templates = None
    if run_id:
        r = _run(request, run_id, user)
        if r["project_id"] != pid or not r.get("summary") or not r["summary"].get("best"):
            raise HTTPException(400, "that run has no best node for this project")
        templates = r["summary"]["best"]["modules"]
    return B.export_bundle(con, p, d, templates=templates).model_dump()


@app.post("/projects/import")
def import_project(b: B.Bundle, request: Request, user=auth.User):
    if b.bundle > B.FORMAT:
        raise HTTPException(400, f"bundle format {b.bundle} is newer than this studio understands ({B.FORMAT})")
    if b.dataset and b.dataset.sample:
        raise HTTPException(400, "imported bundles must inline their rows")
    access = _access(request, user)
    return B.import_bundle(request.app.state.db, user["id"], b, datasets_dir=DATASETS_DIR, unique_name=_unique_name,
                           coerce_models=lambda spec: _coerce_models(spec, access))


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
    rep = validate_static(spec, rows, d["input_map"], store.list_connections(request.app.state.db, user["id"]))
    return {"report": rep.model_dump(), "ok": rep.ok}


@app.post("/projects/{pid}/pilot")
async def pilot(pid: str, body: ValidateBody, request: Request, user=auth.User):
    spec, d, rows = _spec_and_data(request, pid, body, user)
    spec.evaluate.label_column = d["label_column"]
    rep = validate_static(spec, rows, d["input_map"], store.list_connections(request.app.state.db, user["id"]))
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
    rep = validate_static(spec, rows, d["input_map"], store.list_connections(request.app.state.db, user["id"]))
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
    # the spec is stored as it is at this instant, after the tier clamps: this row is the record of what ran
    con.execute("insert into runs (id, project_id, dataset_id, status, dir, started, finished, summary, error, spec)"
                " values (?,?,?,?,?,?,?,?,?,?)",
                (rid, pid, d["id"], "running", str(R.RUNS_DIR / rid), db.now(), None, None, None, spec.model_dump_json()))
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
    r = _run(request, rid, user)
    n = R.read_node(rid, nid)
    if not n:
        raise HTTPException(404, "node not found")
    # the expected answers, keyed the way the evaluator keyed them. A run's per-example rows record what the model
    # said and whether it scored, never what the right answer was - which is what makes a per-class collapse
    # invisible in the UI. Rebuilt through `to_dataset` so the ids match by construction rather than by luck.
    try:
        d = db.row(request.app.state.db.execute("select * from datasets where id=?", (r["dataset_id"],)).fetchone())
        if d:
            ds = to_dataset(_rows(d), d["input_map"], d["label_column"])
            n["expected"] = {ex.id: ex.answer for ex in ds if ex.answer is not None}
    except Exception:
        n["expected"] = {}
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


# ---- versions (v0, flag-gated) --------------------------------------------------------------

def _v0() -> None:
    if not V0:
        raise HTTPException(404, "not enabled")


@app.get("/features")
def features():
    return {"v0": V0}


class PublishBody(BaseModel):
    label: str = ""
    run_id: str | None = None      # None -> publish the canvas as it stands (unscored)
    node_id: str | None = None     # None with a run_id -> that run's best fully evaluated node


@app.post("/projects/{pid}/versions")
def publish_version(pid: str, body: PublishBody, request: Request, user=auth.User):
    """Promote a run node (or the canvas) to an immutable version. Nothing is recomputed here: the score recorded is
    the one the node already earned, on the rows it earned it."""
    _v0()
    p = _project(request, pid, user)
    con = request.app.state.db
    spec = ProjectSpec.model_validate(p["spec"])
    extra: dict[str, Any] = {"source": {"kind": "canvas"}}
    templates = None
    if body.run_id:
        r = _run(request, body.run_id, user)
        if r["project_id"] != pid:
            raise HTTPException(400, "that run belongs to another project")
        # the run's own spec, not the canvas as it stands: publishing an hour later must not record settings the run
        # never used. Older runs predate the column and fall back to the canvas, which is the best that can be done.
        if r.get("spec"):
            spec = ProjectSpec.model_validate(r["spec"])
        node = None
        if body.node_id:
            node = next((n for n in R.read_tree(body.run_id)["nodes"] if n["id"] == body.node_id), None)
            if node is None:
                raise HTTPException(404, "node not found in that run")
        elif not (r.get("summary") or {}).get("best"):
            raise HTTPException(400, "that run has no fully evaluated node to publish")
        extra = V.from_run(r, node)
        templates = extra.pop("templates")
    pinned = V.pin(spec, templates)
    n = len(store.list_versions(con, pid, user["id"])) + 1
    return store.create_version(con, project_id=pid, user_id=user["id"], label=body.label or f"v{n}",
                                spec=pinned.model_dump(mode="json"), fingerprint=V.fingerprint(pinned), **extra)


@app.get("/projects/{pid}/versions")
def list_versions(pid: str, request: Request, user=auth.User):
    _v0()
    _project(request, pid, user)
    return store.list_versions(request.app.state.db, pid, user["id"])


@app.get("/versions/{vid}")
def get_version(vid: str, request: Request, user=auth.User):
    """The immutable fetch: the exact spec and prompts that earned this version's score."""
    _v0()
    v = store.get_version(request.app.state.db, vid, user["id"])
    if not v:
        raise HTTPException(404, "version not found")
    return v

# ---- api keys (v0, flag-gated) ---------------------------------------------------------------

import hashlib as _hashlib
import secrets as _secrets

KEY_PREFIX = "imp_"


def _key_hash(key: str) -> str:
    return _hashlib.sha256(key.encode()).hexdigest()


def api_key_user(request: Request) -> dict:
    """Auth for the serving endpoint: a workspace API key in `Authorization: Bearer ...` or `X-API-Key`. Machines
    call this endpoint, so the session cookie is deliberately not accepted."""
    hdr = request.headers.get("authorization") or ""
    key = hdr[7:].strip() if hdr.lower().startswith("bearer ") else (request.headers.get("x-api-key") or "").strip()
    con = request.app.state.db
    uid = store.api_key_owner(con, _key_hash(key)) if key else None
    if not uid:
        raise HTTPException(401, "a workspace API key is required: send it as `Authorization: Bearer <key>`")
    u = con.execute("select id, email, tier from users where id=?", (uid,)).fetchone()
    if not u:
        raise HTTPException(401, "that key's account no longer exists")
    return dict(u)


class KeyBody(BaseModel):
    label: str = ""


@app.post("/keys")
def create_key(body: KeyBody, request: Request, user=auth.User):
    """The only time the key itself is returned: only its sha256 is stored."""
    _v0()
    key = KEY_PREFIX + _secrets.token_urlsafe(32)
    k = store.create_api_key(request.app.state.db, user_id=user["id"], label=body.label or "api key",
                             hash=_key_hash(key), prefix=key[:len(KEY_PREFIX) + 6])
    return {**k, "key": key}


@app.get("/keys")
def list_keys(request: Request, user=auth.User):
    _v0()
    return store.list_api_keys(request.app.state.db, user["id"])


@app.delete("/keys/{kid}")
def delete_key(kid: str, request: Request, user=auth.User):
    _v0()
    store.delete_api_key(request.app.state.db, user["id"], kid)
    return {"ok": True}


# ---- serving (v0, flag-gated) -----------------------------------------------------------------

class ServeBody(BaseModel):
    inputs: dict[str, Any] = {}
    mock: bool | None = None


@app.get("/v/{vid}")
def version_contract(vid: str, request: Request, user=auth.User):
    """What to POST to this version: its input names, and the fields it returns."""
    _v0()
    v = store.get_version(request.app.state.db, vid, user["id"])
    if not v:
        raise HTTPException(404, "version not found")
    spec = ProjectSpec.model_validate(v["spec"])
    terminal = next((m for m in spec.modules if not spec.outgoing(m.id)), spec.modules[0] if spec.modules else None)
    return {"version_id": vid, "label": v["label"], "inputs": serving.required_inputs(spec),
            "outputs": [f.name for f in terminal.schema_fields] if terminal else [], "eval_model": spec.eval_model,
            "url": f"{brand.public_url()}/api/v/{vid}/run"}


@app.post("/v/{vid}/run")
async def serve_version(vid: str, body: ServeBody, request: Request, user=Depends(api_key_user)):
    """Run a published version on one input. The same compile path and the same executor as evaluation - that is the
    point of the endpoint, not an implementation detail."""
    _v0()
    con = request.app.state.db
    v = store.get_version(con, vid, user["id"])
    if not v:
        raise HTTPException(404, "version not found")
    spec = ProjectSpec.model_validate(v["spec"])
    mock = MOCK_DEFAULT if body.mock is None else body.mock
    access = _access(request, user)
    if not mock:
        _check_models(spec, access)
    on_call = db.usage_logger(con, user["id"], access.tier, v["project_id"])
    try:
        out = await serving.serve(spec, body.inputs, access=access, on_call=on_call, tokens=_step_tokens(con, user, spec),
                                  mock=__import__("app.mock", fromlist=["mock_client"]).mock_client() if mock else None)
    except serving.MissingInputs as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # a failed request is still a trace: it is evidence about the version, and Piece 3 hangs outcomes off it
        tid = store.create_trace(con, version_id=vid, project_id=v["project_id"], user_id=user["id"], inputs=body.inputs,
                                 error=f"{type(e).__name__}: {e}")
        raise HTTPException(502, {"message": f"the version failed on this input: {type(e).__name__}: {e}", "trace_id": tid})
    tid = store.create_trace(con, version_id=vid, project_id=v["project_id"], user_id=user["id"], inputs=body.inputs,
                             output=out["output"], parsed=out["parsed"], path=out["path"], metrics=out["metrics"],
                             input_tokens=out["input_tokens"], output_tokens=out["output_tokens"], usd=out["usd"],
                             latency_s=out["latency_s"], steps=out.get("steps"))
    return {"trace_id": tid, "version_id": vid, "output": out["output"], "parsed": out["parsed"], "path": out["path"],
            "steps": out.get("steps") or None,
            "usage": {"input_tokens": out["input_tokens"], "output_tokens": out["output_tokens"], "usd": out["usd"],
                      "latency_s": out["latency_s"]}}


@app.get("/projects/{pid}/traces")
def list_traces(pid: str, request: Request, limit: int = 100, user=auth.User):
    _v0()
    _project(request, pid, user)
    con = request.app.state.db
    ts = store.list_traces(con, pid, user["id"], limit)
    outs = store.list_outcomes(con, [t["id"] for t in ts])
    return [{**t, "outcomes": outs.get(t["id"], [])} for t in ts]


# ---- outcomes (v0, flag-gated) ----------------------------------------------------------------

class OutcomeBody(BaseModel):
    kind: str = "correction"       # correction (carries the right answer) | rating | reopen | conversion | ...
    label: str | None = None       # the right answer, for a correction: this is what makes the trace a label
    value: float | None = None     # a numeric signal (CSAT, revenue) when there is no label
    source: str = "api"            # who says so: an agent, a downstream system, a rule
    note: str = ""


def outcome_user(request: Request) -> dict:
    """An outcome may arrive from the system that learned it (API key) or from a person in the studio (session).
    Unlike serving, both are legitimate: corrections are as often typed by a human as posted by a ticketing system."""
    try:
        return api_key_user(request)
    except HTTPException:
        return auth.current_user(request)


@app.post("/traces/{tid}/outcome")
def post_outcome(tid: str, body: OutcomeBody, request: Request, user=Depends(outcome_user)):
    """What happened after the answer. Posted against a trace id, usually later and by something else."""
    _v0()
    con = request.app.state.db
    t = store.get_trace(con, tid, user["id"])
    if not t:
        raise HTTPException(404, "trace not found")
    if body.label is None and body.value is None:
        raise HTTPException(400, "an outcome needs a label (the right answer) or a value (a numeric signal)")
    return store.create_outcome(con, trace_id=tid, project_id=t["project_id"], user_id=user["id"], kind=body.kind,
                                label=body.label, value=body.value, source=body.source, note=body.note)


@app.post("/projects/{pid}/versions/{vid}/restore")
def restore_version(pid: str, vid: str, request: Request, user=auth.User):
    """Put a published version back on the canvas. Re-optimizing has to start from what is deployed, not from
    whatever the canvas drifted to since - this is the step that makes the loop a loop rather than a fork."""
    _v0()
    p = _project(request, pid, user)
    con = request.app.state.db
    v = store.get_version(con, vid, user["id"])
    if not v or v["project_id"] != pid:
        raise HTTPException(404, "version not found in this project")
    cur = ProjectSpec.model_validate(p["spec"])
    spec = ProjectSpec.model_validate(v["spec"])
    spec.name, spec.layout = cur.name, cur.layout      # the canvas keeps its name and its node positions
    con.execute("update projects set spec=?, updated=? where id=?", (spec.model_dump_json(), db.now(), pid))
    con.commit()
    return {"ok": True, "spec": spec.model_dump()}


class PromoteBody(BaseModel):
    version_id: str | None = None      # only traces from this version
    kind: str = "correction"
    name: str = ""


@app.post("/projects/{pid}/datasets/from-traces")
def promote_traces(pid: str, body: PromoteBody, request: Request, user=auth.User):
    """Turn corrected traces into an ordinary dataset: same table, same mapping, same Run button."""
    _v0()
    p = _project(request, pid, user)
    con = request.app.state.db
    spec = ProjectSpec.model_validate(p["spec"])
    label_column = spec.evaluate.label_column or "answer"
    rows = L.rows_from_traces(store.labelled_traces(con, pid, user["id"], version_id=body.version_id, kind=body.kind), label_column)
    if not rows:
        raise HTTPException(400, "no corrected traces yet: post an outcome with a label against a trace first")
    did = db.new_id()
    path = DATASETS_DIR / f"{did}.json"
    path.write_text(json.dumps(rows, default=str))
    cols = columns(rows)
    input_map = {ph: ph for ph in serving.required_inputs(spec) if ph in cols}
    name = body.name or f"corrections {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}"
    con.execute("insert into datasets values (?,?,?,?,?,?,?,?,?)",
                (did, pid, name, str(path), json.dumps(cols), len(rows), json.dumps(input_map), label_column, db.now()))
    con.commit()
    return {"id": did, "name": name, "columns": cols, "n_rows": len(rows), "input_map": input_map, "label_column": label_column}

# ---- external steps (v0, flag-gated) ----------------------------------------------------------

def _step_tokens(con, user: dict, spec: ProjectSpec) -> dict[str, Any]:
    """step id -> how to authorise its calls: a string for a pasted key, a callable for an OAuth grant.

    A callable rather than a token, because an OAuth access token lasts thirty minutes and an enrichment over six
    hundred rows outlives it. Every call goes through `oauth.access_token`, which refreshes when it needs to."""
    out: dict[str, Any] = {}
    for st in spec.steps:
        if not (st.enabled and st.credential):
            continue
        kind, _, cid = st.credential.partition(":")
        if kind == "step":
            enc = store.step_secret(con, user["id"], cid)
            if enc:
                out[st.id] = C.decrypt(enc).get("secret", "")
        elif kind == "conn":
            conn = store.get_connection(con, cid, user["id"])
            if conn:
                out[st.id] = (lambda c=conn: O.access_token(con, store.get_connection(con, c["id"])))
    return out


@app.get("/steps")
def list_steps(user=auth.User):
    """The installed manifests. In v0 these are files in the repo; a registry is what replaces this."""
    _v0()
    return X.list_manifests()


class StepCredentialBody(BaseModel):
    label: str = ""
    secret: str


@app.get("/step-credentials")
def list_step_credentials(request: Request, user=auth.User):
    """Keys pasted for external steps. Listed here as well as on the step node, so Models & keys is the one screen
    that shows everything the account has handed out."""
    _v0()
    return store.list_step_credentials(request.app.state.db, user["id"])


@app.post("/step-credentials")
def add_step_credential(body: StepCredentialBody, request: Request, user=auth.User):
    _v0()
    if not body.secret.strip():
        raise HTTPException(400, "a secret is required")
    return store.create_step_credential(request.app.state.db, user_id=user["id"], label=body.label or "step key",
                                        secret=C.encrypt({"secret": body.secret.strip()}))


@app.delete("/step-credentials/{cid}")
def delete_step_credential(cid: str, request: Request, user=auth.User):
    _v0()
    store.delete_step_credential(request.app.state.db, user["id"], cid)
    return {"ok": True}


class ProbeBody(BaseModel):
    dataset_id: str | None = None


@app.post("/projects/{pid}/steps/{sid}/probe")
async def probe_step(pid: str, sid: str, body: ProbeBody, request: Request, user=auth.User):
    """Tier 1 (EXTERNAL-STEPS.md §8): two calls. Is it reachable, does the credential work, does the response match
    the declared schema, and how does it say 'no record'? That last one decides whether a missing customer is a
    failed row or a legitimate null, and production is full of them."""
    _v0()
    p = _project(request, pid, user)
    con = request.app.state.db
    spec = ProjectSpec.model_validate(p["spec"])
    st = next((x for x in spec.steps if x.id == sid), None)
    if st is None:
        raise HTTPException(404, "no such step on this canvas")
    m = X.load_manifest(st.manifest)
    if m is None:
        raise HTTPException(400, f"no manifest {st.manifest!r} is installed")
    token = _step_tokens(con, user, spec).get(sid)
    args: dict[str, Any] = {}
    if body.dataset_id:
        d = _dataset(request, body.dataset_id, user)
        rows = _rows(d)
        if rows:
            args = {name: rows[0].get(col) for name, col in st.inputs.items()}
    if not args:
        args = {f.name: "probe" for f in m.inputs}
    out: dict[str, Any] = {"url": m.transport.url, "sent": args}
    try:
        got, met = await X.call(m, args, token=token)
        out.update({"ok": True, "found": got is not None, "returned": got, "latency_s": met["latency_s"]})
    except X.StepError as e:
        return {**out, "ok": False, "error": str(e)}
    # ... and how it answers for something that does not exist
    unknown = {f.name: "__no_such_id__" for f in m.inputs}
    try:
        got2, _ = await X.call(m, unknown, token=token)
        out["missing_behaviour"] = ("returns a record for an unknown id - the step cannot tell you what it does not know"
                                    if got2 is not None else f"status {m.missing.get('status', 404)}: {m.missing.get('means', 'no record')}")
        out["missing_ok"] = got2 is None
    except X.StepError as e:
        out["missing_behaviour"] = f"errors instead of reporting 'no record': {e}"
        out["missing_ok"] = False
    return out


class EnrichBody(BaseModel):
    step_id: str
    name: str = ""


@app.post("/datasets/{did}/enrich")
async def enrich_dataset(did: str, body: EnrichBody, request: Request, user=auth.User):
    """Fetch and freeze: run the step over every row once and write a NEW dataset carrying its columns.

    A new dataset rather than an edit, because the old one is what earlier runs and versions were scored against.
    From here on evaluation reads these frozen values and never calls the step again; serving calls it live."""
    _v0()
    d = _dataset(request, did, user)
    con = request.app.state.db
    p = _project(request, d["project_id"], user)
    spec = ProjectSpec.model_validate(p["spec"])
    st = next((x for x in spec.steps if x.id == body.step_id), None)
    if st is None:
        raise HTTPException(404, "no such step on this canvas")
    missing = [f for f in st.inputs.values() if f not in d["columns"]]
    if missing:
        raise HTTPException(400, f"this dataset has no column {missing[0]!r} for the step to look up")
    try:
        rows, report = await X.enrich(spec, st, _rows(d), token=_step_tokens(con, user, spec).get(st.id))
    except X.StepError as e:
        raise HTTPException(400, str(e))
    nid = db.new_id()
    path = DATASETS_DIR / f"{nid}.json"
    path.write_text(json.dumps(rows, default=str))
    cols = columns(rows)
    m = X.load_manifest(st.manifest)
    input_map = {**d["input_map"], **{X.column(st.id, f.name): X.column(st.id, f.name) for f in (m.outputs if m else [])
                                      if X.column(st.id, f.name) in cols}}
    name = body.name or f"{d['name']} + {st.id}"
    con.execute("insert into datasets values (?,?,?,?,?,?,?,?,?)",
                (nid, d["project_id"], name, str(path), json.dumps(cols), len(rows), json.dumps(input_map), d["label_column"], db.now()))
    con.commit()
    return {"id": nid, "name": name, "columns": cols, "n_rows": len(rows), "input_map": input_map,
            "label_column": d["label_column"], "report": report}

# ---- connections: OAuth to someone else's system (v0, flag-gated) --------------------------------

@app.get("/connections")
def list_connections(request: Request, user=auth.User):
    _v0()
    return {"connections": store.list_connections(request.app.state.db, user["id"]),
            "providers": [{"name": p.name, "label": p.label, "scopes": p.scopes,
                           "configured": bool(os.environ.get(p.client_id_env) and os.environ.get(p.client_secret_env)),
                           "redirect_uri": O.redirect_uri(p.name)} for p in O.PROVIDERS.values()]}


@app.get("/connect/{provider}")
def connect(provider: str, request: Request, user=auth.User):
    """Returns the authorization URL rather than redirecting: the browser is on a JSON API here, and the caller
    decides when to send the user to the consent screen."""
    _v0()
    try:
        return {"url": O.start(request.app.state.db, user["id"], provider)}
    except O.OAuthError as e:
        raise HTTPException(400, str(e))


@app.get("/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request, code: str = "", state: str = "", error: str = "",
                         error_description: str = ""):
    """Where the provider sends the user back. No session is required - the `state` is what proves this callback
    belongs to a flow we started, and it is consumed on first use."""
    _v0()
    from fastapi.responses import HTMLResponse

    def page(title: str, detail: str, ok: bool) -> HTMLResponse:
        # a redirect back into the app would lose the message; this is the one place the API renders anything
        return HTMLResponse(f"""<!doctype html><meta charset=utf-8><title>{title}</title>
<body style="font:15px/1.6 system-ui;background:#0a0f1e;color:#e8ecf7;display:grid;place-items:center;height:100vh;margin:0">
<div style="max-width:32rem;padding:1.5rem;border:1px solid #26325a;border-radius:12px">
<h1 style="margin:0 0 .5rem;font-size:1.1rem;color:{'#5ee3c8' if ok else '#ff6b6b'}">{title}</h1>
<p style="margin:0 0 1rem;color:#9aa6c8">{detail}</p>
<a href="{brand.app_url()}" style="color:#5ee3c8">Back to {brand.name()}</a></div>""", status_code=200 if ok else 400)

    if error:
        return page("Authorization refused", f"{error}: {error_description}"[:300], False)
    if not code or not state:
        return page("Something is missing", "the provider did not send a code and a state", False)
    try:
        conn = await O.finish(request.app.state.db, state, code)
    except O.OAuthError as e:
        return page("Could not finish connecting", str(e)[:300], False)
    return page(f"Connected to {conn['label']}", "You can close this tab and go back to the studio.", True)


@app.delete("/connections/{cid}")
def disconnect(cid: str, request: Request, user=auth.User):
    _v0()
    store.delete_connection(request.app.state.db, user["id"], cid)
    return {"ok": True}


@app.post("/connections/{cid}/probe")
async def probe_connection(cid: str, request: Request, user=auth.User):
    """Proof the connection actually works: refresh if needed, then make one real call. For HubSpot that is a page
    of tickets - which may legitimately be empty, and an empty 200 is still a pass."""
    _v0()
    con = request.app.state.db
    c = store.get_connection(con, cid, user["id"])
    if not c:
        raise HTTPException(404, "connection not found")
    try:
        token = await O.access_token(con, c)
    except O.OAuthError as e:
        raise HTTPException(400, str(e))
    import httpx
    url = {"hubspot": "https://api.hubapi.com/crm/v3/objects/tickets?limit=5"}.get(c["provider"])
    if not url:
        return {"ok": True, "note": "connected; no probe defined for this provider"}
    async with httpx.AsyncClient(timeout=20) as h:
        r = await h.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "error": r.text[:300]}
    body = r.json()
    return {"ok": True, "status": r.status_code, "records": len(body.get("results", [])),
            "has_more": bool(body.get("paging")), "refreshed": bool(c.get("refreshed"))}

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
