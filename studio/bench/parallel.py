"""Expansion-level parallelism, measured through the studio's own run path.

The expected outcome is bpto `experiments/2026-09-19-ladder-batch-q/NOTES.md` (pin 896418b): with the surrogate in
the expand seat (`engine="bo"`, local q-EI), q = 2 / 4 reaches GEPA q = 1's accuracy in about half / a third of the
rounds at equal rollouts, while GEPA's own q > 1 (blind Pareto draws) loses accuracy. That result is on a mock with
no latency, where rounds stand in for wall time. This harness measures what the mock could not: wall time per round,
Bedrock retries (throttling), cost per rollout, and how a served request fares while runs are going.

Two warm-ups are kept out of the comparison:
- the algorithm's: round 1 has one candidate (the root), so its q rewrites are q independent reflections of the
  root, and the GP is near-uninformative until it has a few observations. Arms get equal *rollouts* (rewrites
  proposed), and per-round timings are reported from round 2 on.
- the hosted q-EI service's (Modal, cold start on the first call): arms using `acquisition="quantecarlo"` are
  preceded by one untimed select call, and that call's latency is reported separately.

Runs in-process against the FastAPI app (the same event loop uvicorn would give it), on its own data directory and
its own account. Nothing touches the live studio's database.

    python -m bench.parallel plan                    # pilot (a few cents at most) + projected cost per arm, no runs
    python -m bench.parallel run --mock              # the whole grid on the mock client: $0, plumbing only
    python -m bench.parallel run --go                # live, sequential: the timing comparison
    python -m bench.parallel run --go --concurrent   # live, every arm of a seed at once: the load test

`ticket-triage-entitlement` needs the demo records step: set RECORDS_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("STUDIO_DATA", str(HERE / "data"))
os.environ["STUDIO_INSECURE_COOKIE"] = "1"
os.environ["STUDIO_V0"] = "1"

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app as _wrapped  # noqa: E402

app = getattr(_wrapped, "app", _wrapped)

ARMS = "gepa:1,bo:1,bo:2,bo:4"


# ---- Bedrock retries: botocore's adaptive mode retries throttles silently; count its log lines -------------------

class RetryCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.times: list[float] = []

    def emit(self, record):
        # botocore also logs "Not retrying request." on every call that succeeds; count only real retries
        msg = record.getMessage()
        if msg.startswith("Retry needed") or msg.startswith("Throttling response"):
            self.times.append(time.time())


RETRIES = RetryCounter()
for name in ("botocore.retries.standard", "botocore.retries.adaptive", "botocore.retryhandler"):
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.addHandler(RETRIES)
    lg.propagate = False


# ---- arms ---------------------------------------------------------------------------------------------------------

def parse_arms(s: str) -> list[dict]:
    out = []
    for a in s.split(","):
        engine, q, *acq = a.strip().split(":")
        out.append({"engine": engine, "q": int(q), "acquisition": acq[0] if acq else "qei"})
    return out


def arm_name(a: dict) -> str:
    return f"{a['engine']}-q{a['q']}" + (f"-{a['acquisition']}" if a["acquisition"] != "qei" else "")


def rounds_for(rollouts: int, q: int) -> int:
    """Every round proposes q rewrites: when there are fewer candidates than q (round 1 has only the root), the studio
    gives each parent extra independent reflection calls (`app/compile.py`)."""
    return math.ceil(rollouts / q)


def arm_spec(base: dict, a: dict, *, seed: int, rollouts: int, max_usd: float) -> dict:
    spec = json.loads(json.dumps(base))
    o = spec["optimizer"]
    o.update({"engine": a["engine"], "seed": seed, "rounds": rounds_for(rollouts, a["q"]), "no_improvement_rounds": None,
              "max_usd": max_usd, "parents_per_round": a["q"] if a["engine"] == "gepa" else 1})
    o["bo"] = {**o.get("bo", {}), "q": a["q"], "acquisition": a["acquisition"]}
    spec["name"] = f"bench {arm_name(a)} s{seed}"
    return spec


# ---- setup --------------------------------------------------------------------------------------------------------

async def setup(c: AsyncClient, example: str, tier: str) -> dict:
    email = f"bench-{int(time.time())}@bench.invalid"
    r = await c.post("/auth/signup", json={"email": email, "password": "bench-password"}); r.raise_for_status()
    # the bench account lives in the bench database only; its tier is the parallelism ceiling under test
    con = app.state.db
    con.execute("update users set tier=? where email=?", (tier, email)); con.commit()
    r = await c.post(f"/examples/{example}/clone"); r.raise_for_status()
    pid = r.json()["id"] if "id" in r.json() else r.json()["project"]["id"]
    p = (await c.get(f"/projects/{pid}")).json()
    spec, did = p["spec"], p["datasets"][0]["id"]
    if spec.get("steps"):
        key = os.environ.get("RECORDS_API_KEY")
        if not key:
            sys.exit(f"{example} uses the records step: set RECORDS_API_KEY")
        cid = (await c.post("/step-credentials", json={"label": "bench records", "secret": key})).json()["id"]
        for st in spec["steps"]:
            st["credential_id"] = cid
        (await c.put(f"/projects/{pid}", json=spec)).raise_for_status()
        for st in spec["steps"]:
            r = await c.post(f"/datasets/{did}/enrich", json={"step_id": st["id"]}); r.raise_for_status()
            did = r.json()["id"]
    return {"pid": pid, "did": did, "spec": spec, "email": email}


async def serving_probe_setup(c: AsyncClient, pid: str, did: str) -> dict:
    """Publish the canvas as a version and mint an API key, so the probe calls the endpoint a customer would."""
    vid = (await c.post(f"/projects/{pid}/versions", json={"label": "bench probe"})).json()["id"]
    key = (await c.post("/keys", json={"label": "bench probe"})).json()["key"]
    info = (await c.get(f"/v/{vid}")).json()
    rows = (await c.get(f"/datasets/{did}")).json()
    row = (rows.get("preview") or rows.get("rows") or [{}])[0]
    ds = next(d for d in (await c.get(f"/projects/{pid}")).json()["datasets"] if d["id"] == did)
    imap = ds.get("input_map") or {}
    # prompt placeholders come through the mapping; a step's inputs are named by the column it reads
    inputs = {k: row.get(imap.get(k, k)) for k in info["inputs"]}
    return {"vid": vid, "key": key, "inputs": inputs}


class Probe:
    """One served request every `every` seconds; latencies tagged with the wall-clock time they finished."""

    def __init__(self, c: AsyncClient, cfg: dict, mock: bool, every: float):
        self.c, self.cfg, self.mock, self.every = c, cfg, mock, every
        self.samples: list[tuple[float, float, bool]] = []
        self._task = None

    async def once(self) -> None:
        t0 = time.perf_counter()
        r = await self.c.post(f"/v/{self.cfg['vid']}/run", json={"inputs": self.cfg["inputs"], "mock": self.mock},
                              headers={"Authorization": f"Bearer {self.cfg['key']}"})
        self.samples.append((time.time(), time.perf_counter() - t0, r.status_code == 200))

    async def _loop(self):
        while True:
            try:
                await self.once()
            except Exception:
                self.samples.append((time.time(), float("nan"), False))
            await asyncio.sleep(self.every)

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()


def prewarm_hosted_qei() -> float:
    from bpto.bo import QuantecarloQEI
    import numpy as np
    t0 = time.perf_counter()
    QuantecarloQEI()(np.array([0.0, 0.2, 0.1]), np.eye(3) * 0.09, 0.2, 2)
    return time.perf_counter() - t0


# ---- one run ------------------------------------------------------------------------------------------------------

async def one_run(c: AsyncClient, s: dict, a: dict, seed: int, args) -> dict:
    spec = arm_spec(s["spec"], a, seed=seed, rollouts=args.rollouts, max_usd=args.max_usd_run)
    (await c.put(f"/projects/{s['pid']}", json=spec)).raise_for_status()
    t0 = time.time()
    r = await c.post(f"/projects/{s['pid']}/runs", json={"dataset_id": s["did"], "mock": args.mock}); r.raise_for_status()
    rid = r.json()["id"]
    trace, last = [], None
    while True:
        await asyncio.sleep(args.poll)
        live = (await c.get(f"/runs/{rid}")).json()
        st = live.get("live") or {}
        key = (st.get("round"), st.get("step"))
        if key != last:
            trace.append({"t": time.time() - t0, "round": st.get("round"), "step": st.get("step"),
                          "best": (st.get("best") or {}).get("score"), "calls": (st.get("usage") or {}).get("calls"), "usd": st.get("spent_usd")})
            last = key
        if st.get("state") in ("done", "failed", "stopped"):
            break
    t1 = time.time()
    return {"arm": arm_name(a), **a, "seed": seed, "run_id": rid, "state": st.get("state"), "error": st.get("error"),
            "t_start": t0, "t_end": t1, "seconds": t1 - t0, "trace": trace, "summary": live.get("summary") or st.get("summary"),
            "retries": sum(1 for x in RETRIES.times if t0 <= x <= t1), "rounds_planned": spec["optimizer"]["rounds"]}


# ---- analysis -----------------------------------------------------------------------------------------------------

def round_starts(trace: list[dict]) -> dict[int, float]:
    out: dict[int, float] = {}
    for e in trace:
        if e["round"] is not None and e["round"] not in out:
            out[e["round"]] = e["t"]
    return out


def seconds_per_round(res: dict) -> float | None:
    """Median wall time of rounds 2+ (round 0 is the root evaluation, round 1 the single-parent warm-up)."""
    rs = round_starts(res["trace"])
    ks = sorted(k for k in rs if k >= 2)
    gaps = [rs[b] - rs[a] for a, b in zip(ks, ks[1:])]
    return statistics.median(gaps) if gaps else None


def time_to(res: dict, target: float) -> tuple[int | None, float | None]:
    for e in res["trace"]:
        if e["best"] is not None and e["best"] >= target - 1e-9:
            return e["round"], e["t"]
    return None, None


def pct(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x == x)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def report(results: list[dict], probe: list[tuple[float, float, bool]], idle: list[float], extra: dict) -> str:
    ok = [r for r in results if r["state"] == "done"]
    base = [(r["summary"] or {}).get("best", {}).get("score") for r in ok if r["arm"] == "gepa-q1"]
    base = [b for b in base if b is not None]
    target = statistics.median(base) if base else None
    lines = [f"# Parallelism bench, {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"Target = median best train score of gepa-q1 = {target}", ""]
    if extra.get("modal_cold_s") is not None:
        lines += [f"Hosted q-EI cold start (untimed prewarm call): {extra['modal_cold_s']:.1f} s", ""]
    lines += ["| arm | runs ok | best (median) | holdout best | holdout accuracy | template tokens | rewrites | s/rewrite | s/round (r2+) | wall s | rounds to target | s to target | $ | retries | probe p50 / p95 s |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in dict.fromkeys(r["arm"] for r in results):
        rs = [r for r in results if r["arm"] == arm]
        good = [r for r in rs if r["state"] == "done"]
        best = [((r["summary"] or {}).get("best") or {}).get("score") for r in good]
        hold = [(((r["summary"] or {}).get("holdout") or {}).get("best_score")) for r in good]
        spr = [x for x in (seconds_per_round(r) for r in good) if x is not None]
        tt = [time_to(r, target) for r in good] if target is not None else []
        during = [lat for r in good for (t, lat, _) in probe if r["t_start"] <= t <= r["t_end"]]
        med = lambda xs: (round(statistics.median([x for x in xs if x is not None]), 3) if any(x is not None for x in xs) else None)
        props = [(r["summary"] or {}).get("proposed") for r in good]
        hacc = [((((r["summary"] or {}).get("holdout") or {}).get("best") or {}).get("accuracy")) for r in good]
        ttok = [((((r["summary"] or {}).get("best") or {}).get("metrics") or {}).get("template_tokens")) for r in good]
        spp = [r["seconds"] / p for r, p in zip(good, props) if p]
        lines.append(f"| {arm} | {len(good)}/{len(rs)} | {med(best)} | {med(hold)} | {med(hacc)} | {med(ttok)} | {med(props)} | {med(spp)} | {med(spr)} | {med([r['seconds'] for r in good])} | "
                     f"{med([x[0] for x in tt])} | {med([x[1] for x in tt])} | {round(sum((r['summary'] or {}).get('spent_usd') or 0 for r in good), 4)} | "
                     f"{sum(r['retries'] for r in rs)} | {pct(during, .5) and round(pct(during, .5), 2)} / {pct(during, .95) and round(pct(during, .95), 2)} |")
    lines += ["", f"Idle probe latency p50 / p95: {pct(idle, .5)} / {pct(idle, .95)} s ({len(idle)} requests)",
              f"Probe failures: {sum(1 for (_, _, good) in probe if not good)} of {len(probe)}"]
    return "\n".join(lines)


# ---- commands -----------------------------------------------------------------------------------------------------

async def cmd_plan(args):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench", timeout=600) as c:
            s = await setup(c, args.example, args.tier)
            r = await c.post(f"/projects/{s['pid']}/pilot", json={"dataset_id": s["did"], "rows": args.pilot_rows, "mock": args.mock})
            r.raise_for_status()
            pilot = r.json()
            print(f"pilot nondeterminism band: {pilot['pilot'].get('nondeterminism_band')}")
            total = 0.0
            print(f"\n{'arm':<22}{'rounds':>8}{'$ / run':>12}{'x seeds':>10}")
            for a in parse_arms(args.arms):
                spec = arm_spec(s["spec"], a, seed=0, rollouts=args.rollouts, max_usd=args.max_usd_run)
                (await c.put(f"/projects/{s['pid']}", json=spec)).raise_for_status()
                cost = (await c.get(f"/projects/{s['pid']}/cost", params={"dataset_id": s["did"]})).json()
                per = cost["usd"]["total"]
                total += per * args.seeds
                print(f"{arm_name(a):<22}{spec['optimizer']['rounds']:>8}{per:>12.4f}{per * args.seeds:>10.4f}")
            print(f"\nprojected total for {args.seeds} seeds: ${total:.3f} (+ serving probes, + BO embeddings; cap per run ${args.max_usd_run})")
            usage = (await c.get("/usage")).json()
            print(f"pilot actual spend: ${usage['totals']['usd']:.4f}")


async def cmd_run(args):
    if not args.mock and not args.go:
        sys.exit("live runs spend money: run `plan` first, then pass --go (or --mock for a $0 dry run)")
    arms = parse_arms(args.arms)
    out = HERE / "out" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    extra: dict = {}
    if any(a["acquisition"] == "quantecarlo" for a in arms) and not args.mock:
        extra["modal_cold_s"] = prewarm_hosted_qei()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench", timeout=600) as c:
            s = await setup(c, args.example, args.tier)
            probe, idle = None, []
            if not args.no_probe:
                pcfg = await serving_probe_setup(c, s["pid"], s["did"])
                probe = Probe(c, pcfg, args.mock, args.probe_every)
                for _ in range(args.idle_probes):
                    await probe.once()
                idle = [lat for (_, lat, _) in probe.samples]
                probe.samples.clear()
                probe.start()
            results = []
            for seed in range(args.seeds):
                if args.concurrent:
                    # one project per arm so the runs don't share a canvas
                    subs = []
                    for a in arms:
                        r = await c.post("/projects", json={**s["spec"], "name": f"bench {arm_name(a)} s{seed}"}); pid = r.json()["id"]
                        subs.append({**s, "pid": pid})
                    batch = await asyncio.gather(*(one_run(c, sub, a, seed, args) for sub, a in zip(subs, arms)))
                    results += batch
                else:
                    for a in arms:
                        results.append(await one_run(c, s, a, seed, args))
                        print(f"seed {seed} {arm_name(a)}: {results[-1]['state']} in {results[-1]['seconds']:.0f}s", flush=True)
                (out / "results.jsonl").write_text("\n".join(json.dumps(r, default=str) for r in results))
            if probe:
                await probe.stop()
            md = report(results, probe.samples if probe else [], idle, extra)
            (out / "summary.md").write_text(md)
            (out / "probe.json").write_text(json.dumps({"idle": idle, "during": probe.samples if probe else []}))
            usage = (await c.get("/usage")).json()
            print(md)
            print(f"\nactual spend (usage log, this bench account): ${usage['totals']['usd']:.4f}\nwritten to {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--example", default="ticket-triage-entitlement")
        p.add_argument("--arms", default=ARMS, help="engine:q[:acquisition], comma-separated")
        p.add_argument("--seeds", type=int, default=3)
        p.add_argument("--rollouts", type=int, default=24, help="parents expanded per run, equal across arms")
        p.add_argument("--tier", default="advanced", help="the bench account's tier: its max_concurrency is the ceiling")
        p.add_argument("--max-usd-run", type=float, default=0.15)
        p.add_argument("--mock", action="store_true")
        p.add_argument("--no-probe", action="store_true")
        p.add_argument("--probe-every", type=float, default=5.0)
        p.add_argument("--idle-probes", type=int, default=10)
        p.add_argument("--pilot-rows", type=int, default=12)
        p.add_argument("--poll", type=float, default=0.5)
    sub.choices["run"].add_argument("--go", action="store_true", help="allow live spend")
    sub.choices["run"].add_argument("--concurrent", action="store_true", help="every arm of a seed at once (load test)")
    args = ap.parse_args()
    asyncio.run(cmd_plan(args) if args.cmd == "plan" else cmd_run(args))


if __name__ == "__main__":
    main()
