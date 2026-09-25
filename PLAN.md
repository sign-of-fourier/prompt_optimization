# v0 plan: prove the loop, fix the contracts, throw the rest away

**Status:** planning. Nothing here is built yet. Written 2026-09-23.

v0 is a proof of concept that runs the whole product loop once, on the box we already have, with one workflow
family and one connector. It is expected to be replaced. Its job is not to scale, serve customers or make money —
its job is to settle the **contracts** that v1 has to honour, by running them against reality before anyone commits
to RDS, worker pools, Modal, connectors at scale, or a price.

The organising rule:

> **The contracts are the deliverable. The implementations are disposable.**
>
> If getting it wrong later means a *redesign* — changing what callers know, what ids mean, what a record contains —
> build it properly now. If getting it wrong later means a *rewrite* behind an unchanged interface — sqlite to
> Postgres, in-process to queued, local files to S3 — fake it now and write down the asterisk.

## Ground rules

1. **Do not break what works today.** The studio is live at impromptune.com and the marketing sites depend on
   screenshots of real runs. v0 adds alongside; it does not refactor the optimizer, the canvas or the run loop to
   suit itself.
2. **Replace components, don't patch them.** If the smallest change to an existing component turns out to be
   awkward, that component is telling you it was built for a different job. Write a new one beside it, move the
   callers, delete the old one. Do not accumulate compatibility shims in v0 — the whole point is that it is cheap
   to throw away.
3. **No customer data in v0.** v0 runs on our own data and synthetic data. The first outside dataset is the
   trigger that promotes storage, isolation and retention to the real thing (see *Migration triggers*).
4. **Every faked piece gets one interface and exactly one implementation.** Not abstraction for its own sake —
   just enough that v1 adds a second implementation instead of editing call sites.
5. **Write the asterisks down as you go**, in this file. Undocumented knowledge evaporates in six weeks, and the
   knowledge is the deliverable.

## What exists today

Worth stating plainly, because the plan is only honest if the starting line is.

**Built and working:** the graph spec and compiler (`app/models.py`, `app/compile.py`), the React Flow canvas,
point-and-click scorers (exact match, contains, token F1, regex, JSON field, numeric, LLM judge ± reference),
static validation, the pilot (baseline, non-determinism band, cost projection), the optimization loop with GEPA and
BO seats, per-run completion cache / checkpoint / event log / dollar budget, hold-out reporting, the accuracy and
compression goals, the run view with tree and front plot, multi-provider credentials with tier gating, a per-call
usage log, email/password auth, and the examples library with project export/import (bundles).

**Does not exist at all:** production serving, traces, outcomes, connectors, plugins/external steps, metering
beyond raw counters, versioning of published prompts.

**Infrastructure reality:** one EC2 box, nginx + systemd, FastAPI, **sqlite**, runs execute in-process with asyncio,
run artifacts are local files. bpto is a pinned git dependency; changes to it go upstream as a PR, then the pin
moves (see `CLAUDE.md`).

## The v0 slice

One family — **classify / route** — through all seven product steps, exactly once, with no branching:

```
connect → labelled dataset → baseline → optimize → publish a version → serve it → trace →
post an outcome → new labels → re-optimize
```

Ticket triage is the slice. The optimize half already works and is on the marketing site, so v0's new work is
bookended around a demonstrated middle. Every contract below gets exercised one time by this slice. If a contract
is not exercised by the slice, it is not in v0.

## The pieces

Each piece is independently shippable and states: the **contract** (durable, the reason the piece exists), the
**v0 build** (disposable), the **asterisk** (what we are knowingly faking), the **trigger** (what forces the real
thing), and **done when**.

---

### Piece 0 — The real cost formula

Feeds pricing and the metering unit, costs nothing, removes the largest known error in the strategy doc.

- **Contract:** how a run's call count is predicted from `rounds × parents × children × rows × nodes`, *including*
  the minibatch gate and the completion cache — the two terms that actually govern spend.
- **v0 build:** derive it from the runs already on disk (`studio/data/runs/*/status.json`), check it against the
  existing `project_cost` projection, write the corrected formula and its measured constants into this repo.
- **Asterisk:** derived from two-node classify runs on Nova-class models only; long-input families and judge-heavy
  families will have different constants.
- **Trigger:** first family with a different shape (RAG, transform) gets its own measurement.
- **Done when:** the projection predicts a held-back run's call count within ~20%, and the metering unit in Piece 6
  is chosen using it.

**Result (2026-09-23, 5 finished runs on disk).** Gross model calls:

```
calls = E·S                 round 0: the root over the training set
      + R·P·mb              critic notes (feedback = critic)
      + proposed            one reflection call per child
      + proposed·mb·S       the minibatch gate
      + accepted·E·S        full evaluation, accepted children only
```

E = training rows, S = steps per example, R = rounds, P = parents per round, mb = check batch,
proposed = R·P·children, accepted = a·proposed.

Measured against runs `2d6c5d588a61`, `7fce5722f372`, `a48c0dbc4aa9`, `c74285c8ca9e`, `f4ae5c31feba`:
**mean error 3.2%** (max 7.2%) on gross calls. Completion cache then removes **8–27%** of them; billed calls are
what `usage_log` records. Acceptance rate **0.45 pooled** (14 of 31 children), range 0.00–0.60 per run;
`project_cost` currently assumes 0.30, i.e. it is conservative by roughly 1.5× on the dominant term.

The structural model is near-exact *given* acceptance; ex ante, acceptance is the only real uncertainty, and it
lands on `accepted·E·S`, which is 65–68% of all calls in these runs. **That is the term metering must price.**

Correction to the strategy doc: its `R·q·(N+M)·E·k·(1+j)` is **~2.1× high** on its own example (128,000 vs 62,240
before cache, ~2.7× after) because it gives every child a full evaluation instead of only the accepted ones. It is
conservative, not wrong by an order of magnitude — flat tiers are more viable than that figure suggests, but the
gate and the cache belong in the formula before a price is set.

**Also found (fixed 2026-09-24, see Piece 1):** a run does not store the spec it ran. The run row has a `project_id`, and the project's spec has
changed since — so an old run's numbers cannot be reproduced or attributed to a configuration. This is exactly what
Piece 1 fixes, and it is the reason Piece 1 comes before serving rather than after.

### Piece 1 — Versions

Everything downstream needs an immutable thing to point at.

- **Contract:** a **version** is an immutable, fully pinned artifact: spec + prompt templates + model config +
  plugin ids and versions + the dataset id it was scored on + the score it earned. Versions are created by
  promoting a run's node (usually the best) or the current canvas. They are never edited.
- **v0 build:** a `versions` table and a "Publish this node as a version" action in the run view. Bundles already
  serialise spec + dataset, so this is mostly reuse.
- **Asterisk:** no rollback UI, no parity gate, no approval workflow — publishing is one click and the newest
  version wins.
- **Trigger:** first deployment that matters to someone other than us.
- **Done when:** a version can be created, listed, and fetched, and re-fetching an old one reproduces the exact
  prompts that were scored.

**Result (2026-09-23).** Built, behind `STUDIO_V0=1` (off by default: nothing at impromptune.com changes until the
service sets it). `app/store.py` is Piece 7's seam and owns every new table; `app/versions.py` owns the domain;
`main.py` gains `GET /features`, `POST /projects/{pid}/versions`, `GET /projects/{pid}/versions`, `GET /versions/{vid}`;
the run view gains a Versions card and a publish button on the run header and on any evaluated node. 13 tests pass.

A version stores the **pinned** spec — templates from the promoted node, every model id spelled out (`m.model or
eval_model`, judge, critic), layout dropped — plus a **fingerprint**: sha256 over `modules, edges, entry, max_steps,
eval_model, evaluate` only. Same fingerprint, same behaviour; changing the optimizer or the canvas layout does not
move it. Publishing a node that is not the run's best records no hold-out, because that node was never scored on
those rows.

**Correction (2026-09-24).** As first built, `publish_version` pinned the *project's current* spec and substituted
only the node's templates. Publishing two versions from two earlier runs therefore recorded whatever the canvas had
drifted to in between - in the entitlement A/B both versions claimed the external step was disabled, because that
was the canvas's final state, not either run's. Prompts were pinned; everything else silently was not. Runs now
store their spec at start (`runs.spec`) and a version is built from that. Runs from before the column fall back to
the canvas, which is all that can be done for them.

Asterisks, both of which have to close before the pin is honest:

- **The dataset is pinned by id, and a dataset is mutable** — its rows file can be replaced under the same id. A
  version's `dataset_id` therefore names the dataset, not its contents. Needs a content hash (Piece 3, where
  promoted traces start producing datasets).
- **External steps are not pinned** because they do not exist yet; Piece 4 puts a manifest id and version into
  `spec` the same way a model id goes in, and `BEHAVIOUR` picks them up for free.

### Piece 2 — Serving

The part that does not exist at all, and the one that constrains everything else.

- **Contract:** **one compile path for evaluation and production.** A served request and an evaluated row go
  through the same `compile.py` → bpto program execution. This is what makes "the prompt that was scored is the
  prompt that runs" true rather than aspirational, and it is the invariant that lets v1 split eval and production
  into separate pools without changing semantics.
- **v0 build:** `POST /v/{version_id}/run` in the same FastAPI process: takes the input fields the spec declares,
  runs the pinned version, returns the output fields plus a trace id.
- **Asterisk:** synchronous, in-process, no queue, no worker pool, no reserved quota, no autoscale, no auth beyond
  a workspace API key.
- **Trigger:** a production request that has to wait behind an optimization run — that is the moment the pools split.
- **Done when:** the triage version answers a ticket over HTTP, and the answer is byte-identical to what the
  evaluator produced for the same input.

**Result (2026-09-23).** Built, behind the same `STUDIO_V0=1`. `app/serving.py` holds the whole path:
`required_inputs(spec)` (the placeholders a caller must supply — the same forward walk `validation.check_mapping`
does, without a dataset) and `serve()`, which builds the task through `compile.build_task` and executes it with
`bpto.run_program` — **the same two calls the evaluator makes**. There is no second execution path to keep in sync.

`POST /v/{vid}/run` takes `{"inputs": {...}}` and returns output, parsed fields, the path through the graph, a trace
id and usage. `GET /v/{vid}` publishes the contract (input names, output names, url) and the run view prints the
matching curl. Auth is a workspace API key (`Authorization: Bearer imp_…`, sha256 at rest, shown once, revocable);
the session cookie is deliberately rejected there, since the caller is a machine. Served calls go through
`usage_logger` with `purpose="serve"`, so house spend on production sits in the same ledger as runs — Piece 6 meters
one place, not two.

The done-when is asserted, not asserted-to: the test serves a training row and compares the answer byte for byte
against the output that row's evaluation recorded in the run tree. Same prompts, same executor, same bytes.

Traces are written here rather than in Piece 3, because a trace id that points at nothing is not a trace. The table
carries the full contract shape (inputs, output, parsed, path, metrics, tokens, cost, latency, error) and a failed
request records one too — a failure is evidence about a version. Piece 3 adds outcomes against `trace_id` and
promotion to a dataset.

Asterisks: synchronous and in-process (an optimization run and a served request share one event loop — that is the
trigger in the table); no completion cache on the serving path, so a repeated request costs again; no per-version
rate limit; one key scope (a key can call every version in its workspace).

### Piece 3 — Traces and outcomes

The flywheel, and the contract most expensive to retrofit.

- **Contract:** every served request emits a **trace** (`trace_id`, `version_id`, inputs, output, per-node metrics,
  tokens, cost, latency, timestamp). An **outcome** is a later event posted against a `trace_id` (the agent's
  correction, a reopen, a CSAT, a closed deal). A trace plus an outcome is a **label**, and promoting a set of them
  produces a dataset identical in shape to an uploaded one.
- **v0 build:** a `traces` table, an `outcomes` table, `POST /traces/{id}/outcome`, and a "promote traces to
  dataset" action that produces a normal dataset.
- **Asterisk:** sqlite, no EventBridge, no connector-driven outcome capture, no sampling, no retention policy.
- **Trigger:** first real traffic, or the first outcome that has to arrive from a third-party system.
- **Done when:** a served request → a posted outcome → a promoted dataset → a re-optimization run that improves on
  the deployed version, end to end.

**Result (2026-09-23).** Built, same flag. `outcomes` joins `traces` in `store.py`; `app/labels.py` turns the pair
into rows. `POST /traces/{tid}/outcome` accepts **either** an API key or a studio session — unlike serving, both are
legitimate, because a correction is as often typed by a person as posted by a ticketing system. `POST
/projects/{pid}/datasets/from-traces` writes an ordinary row in `datasets`: same table, same mapping, same Run
button, so nothing downstream knows whether the rows came from a CSV or from production. Every promoted row keeps
`trace_id`, `version_id`, `captured_at` and `label_source` as metadata — they are not placeholders, so they never
reach a prompt, but they are what makes a label auditable. A trace corrected twice keeps the last answer.

One thing the plan did not name but the loop needs: **`POST /projects/{pid}/versions/{vid}/restore`**, which puts a
published version back on the canvas (keeping the canvas's name and node positions). Without it "re-optimize" starts
from whatever the canvas drifted to, which forks rather than iterates. The new run's root is then literally the
deployed prompt, which is also what makes "did it improve on what is deployed" a question the run answers by itself.

The UI moved with the contract rather than growing into the Runs tab: a **Serving** tab holds versions (with their
curl and "load onto canvas") and traces (with inline correction and "promote corrections to a dataset"). Publishing
stays in the run view, where the node is.

The end-to-end is a test, not a claim: serve six requests → correct them → promote → restore the version → run on
the promoted dataset → done, with the run's best at or above its root. What the test cannot show is *improvement on
real data* — under the mock, "better" is arithmetic. That single real run is the first spend this plan needs, and it
is the honest end of the slice.

Asterisks: sqlite, no EventBridge, no connector-driven capture, no sampling, no retention policy, and outcomes are
trusted as posted (no reviewer, no agreement check between two humans correcting the same trace).

### Piece 4 — External steps

The generic step API from the strategy doc, and the abstraction discussed for RAG, MCP and third-party plugins.

- **Contract:** a node is either **part of the program** (a prompt, which the optimizer rewrites) or **not** (an
  external step, fixed, part of the environment). An external step declares: input schema, output schema,
  transport, **`cacheable`**, the **metrics it reports back** (latency, cost, hit/miss — these join the metric
  vector so the objective can weigh them), timeout and retry budget, the credential it needs, and a **version**.
  Failure degrades one row; it never kills a run. Caching rule: upstream of the optimization target it is cached
  per example, downstream it reruns per candidate.
- **v0 build:** HTTP POST, JSON in / JSON out, manifests as files in the repo the way `studio/examples/` bundles
  are. **Placement: pre-program only** — a step whose inputs depend only on the dataset row runs once per row as
  dataset enrichment, cached forever, and needs no change to bpto. One first-party example step and one real
  external endpoint.
- **Asterisk:** no registry, no third parties, no sandbox, no subprocess/CLI transport, no mid-graph placement.
  Tunable (non-text) parameters are declared in the manifest but not yet optimized.
- **Trigger:** the first step whose input depends on a *prompt's output* (query rewriting for RAG) — that one needs
  bpto's executor to host non-prompt steps, which is a PR upstream and a pin bump, not a local fork.
- **Done when:** an external step runs inside a scored pipeline, its metrics appear in the metric vector, its
  results cache, and a deliberate failure marks one row bad without ending the run.

**Result (2026-09-23).** Built, same flag, 20 tests. `app/steps.py` is the manifest, the transport and enrichment;
`app/step_validation.py` is the four-tier check; `steps/records/` is the demo endpoint, in its own process on its
own port with its own key. A step is declared in `ProjectSpec.steps`, pinned by `manifest_sha` in a version, and
`versions.BEHAVIOUR` picks it up so the fingerprint moves when a step changes - the free extension Piece 1
predicted.

**The run loop needed no changes at all**, which is the payoff of freezing: enrichment materialises onto the dataset
as ordinary columns, so evaluation is exactly what it was. Serving calls the step live and records what it returned
on the trace. `bpto` needed no changes either - pre-program placement never touches its executor.

Two things worth keeping:

- **A marginal signal test is not enough, and the entitlement case proved it.** Written one field at a time, the
  check reported "rec_plan does not predict the label better than chance (p=0.47)" - about the field the whole case
  depends on. The label is a function of the ticket text *and* the record together, and a single-field test cannot
  see an interaction. The fix is a joint test over the step's fields (45% against a 17% baseline, p=0.005) with the
  per-field result demoted to `info`. A check that confidently says "delete this" about the load-bearing field is
  worse than no check.
- **Enrichment is single-flight.** Rows are fetched concurrently, so the cache has to hold a promise per key, not a
  result; otherwise a hundred tickets from twenty customers make a hundred calls. 120 rows, 60 distinct customers,
  60 calls.

The demo dataset states its own headline: **accuracy ceiling without the record is 83%, with it 100%**, computed
from the labelling rule rather than asserted. The run that measures whether optimization reaches it is the one real
spend, about three cents for the pair, and it has not been run.

Asterisks unchanged from EXTERNAL-STEPS.md §14, plus: the records service runs under its own systemd unit
(`deploy/records.service`) on loopback, so its latency is honest-ish (60-120ms injected) but not WAN.

### Piece 5 — A label source

The step where products like this die. Prove it early, on one connector.

- **Contract:** a connector produces a **labelled dataset**, not just a trigger. Rows carry `source_ref` (the
  record in the source system), `captured_at`, the label and its provenance, and a stable join key that lets a
  later outcome attach to the same entity.
- **v0 build:** one connector (ticketing), pasted API token, manual "pull now", mapping UI for which field is the
  label.
- **Asterisk:** no OAuth application, no webhooks, no incremental sync cursor, no rate-limit handling, one
  connector only. **OAuth app registration and Google restricted-scope verification take weeks of calendar time —
  start that process during v0 even though v0 does not use it.**
- **Trigger:** second connector, or any connector touching data we do not own.
- **Done when:** "connect → labelled dataset → baseline score" runs without hand-editing a file, and the labels are
  good enough to optimize against. *If they are not, that finding is more valuable than the rest of v0.*

**Status (2026-09-24): parked, and the reason is the finding.** We have a HubSpot portal with a Service Key
(`crm.objects.tickets.read`) and it holds two contacts and no tickets. That settles what a free vendor account can
and cannot buy: it proves auth, pagination, rate limits, property discovery and field mapping - and it cannot say
anything about whether real dispositions are good enough to optimize against, because there are none.

The tempting fix is to push our synthetic tickets into HubSpot and pull them back out. That tests the connector
while *looking* like a test of the data, which is worse than not testing it. So Piece 5 splits in two:

- **the plumbing** - OAuth is built (2026-09-25), see below; the pull itself is next and claims nothing about labels;
- **the finding** - needs a few hundred rows of somebody's real ticket text with the queue it actually ended up in.
  Parked until that data exists, either from a design partner or from a separate synthetic-data process that is
  honest about being synthetic.

**OAuth un-faked (2026-09-25).** The v0 asterisk said "pasted API token"; that is now a real authorization-code
flow in `app/oauth.py`, with a `connections` table in the storage seam and a consent screen the user sees and can
revoke from either side. **No data was needed to build or test it** - authorization is a protocol, and an empty
portal proves it exactly as well as a full one, which is why this was worth doing while the label question is
parked. Providers are data (a dict entry), not a code path.

Three things it gets right, because each would be a redesign rather than a rewrite: `state` is single-use,
short-lived and server-side, so a replayed callback finds nothing (a signed cookie cannot be *consumed*); tokens are
encrypted at rest with the model-credential Fernet key and never appear in any listing; and refresh happens inside
`access_token()` before a caller ever holds an expired one - not optional garnish, since HubSpot access tokens last
30 minutes. Eight tests cover it against a real OAuth server on a real socket. The only untested inch is HubSpot's
own consent screen, which needs a registered app.

Note for whoever builds it: HubSpot disabled new legacy private app creation (28 Sep 2026 for new accounts,
26 Oct 2026 for existing), so the credential is a **Service Key** (`Authorization: Bearer`, Settings → Integrations
→ Service Keys), not a private-app token. Reading *other people's* portals still needs a Projects-based public app
and OAuth, which is the long-lead item worth starting early.

### Piece 6 — Metering and caps

- **Contract:** a defined **credit unit**, charged at exactly **one** place in the code, with a per-run cap and a
  per-workspace cap, enforced for evaluation calls, production calls, external-step calls and judge calls alike.
  Free tier runs on the user's own credentials; paid tiers spend ours.
- **v0 build:** counters and hard stops on top of the existing `usage_log`, using Piece 0's formula for the
  pre-run projection.
- **Asterisk:** no billing integration, no invoicing, no overage, no self-serve upgrade.
- **Trigger:** the first account that pays.
- **Done when:** a run that would exceed the cap is refused *before* it spends, with a message saying what it would
  have cost.

### Piece 7 — The storage seam

Not a rewrite. A discipline plus a small amount of new code.

- **Contract:** all database access goes through one layer, no sqlite-specific SQL in feature code, and every blob
  (datasets, run artifacts, traces) is addressed by an indirection rather than a hard-coded path.
- **v0 build:** the seam exists for the *new* tables and files (versions, traces, outcomes). Existing storage is
  left exactly as it is.
- **Asterisk:** sqlite and local files throughout; two storage styles coexist during v0.
- **Trigger:** second concurrent tenant (Postgres) or first dataset too large to sit on the box (S3).
- **Done when:** the new tables can be pointed at a different backend by changing one module.

---

## Not in v0

Stated so the scope cannot drift: the builder agent, Bedrock Flows export and its parity gate, Modal / GPU, RDS,
separate worker pools, a second connector, OAuth, billing, RAG and Knowledge Bases, transform / assess / generate
families, multi-turn, tool-using agents, and any third-party plugin registry.

Two of these deserve a note. **The builder agent writing and deploying Lambdas** is the riskiest item in the
strategy doc — generated code executing in our account, per tenant — and v0 does not touch it; the external-step
contract (they host it, we call it) covers the same need safely. **Modal / GPU for qEI** should be re-examined
before v1 commits to it: the BO seat fits a GPR over tens to hundreds of tree nodes, which is CPU-trivial, so the
question is whether the *embedder* needs a GPU or whether a small CPU model or a hosted embedding API removes that
box from the architecture entirely.

## Migration triggers

The single list of what promotes a fake to the real thing. When one fires, that piece gets rebuilt — not patched.

| Trigger | Promotes |
|---|---|
| First outside dataset | Storage, tenant isolation, retention and deletion policy, DPA and privacy policy |
| Second concurrent tenant | sqlite → Postgres; in-process runs → queue and separate eval/production pools |
| Production request queued behind an optimization run | Worker pools with a reserved production quota |
| First paying account | Billing on top of Piece 6's credit unit |
| First step whose input is a prompt's output | Mid-graph external steps — a PR upstream in bpto, then a pin bump |
| Second connector | Connector framework, OAuth, incremental sync |
| Dataset too large for the box | Blob storage → S3 |
| First deployment someone else depends on | Version rollback, approval, and the parity gate |

## Open decisions

These block specific pieces and are not mine to make.

- **Metering unit** — credits, calls, or tokens? Blocks Piece 6. Piece 0 gives the numbers to choose with.
- **Which connector** for Piece 5 - and the dispositions question is now answered: no, we do not have an account
  with usable history. Piece 5's finding is parked until real data exists; its plumbing can be built against
  HubSpot whenever it is wanted.
- **Hosted multi-tenant vs customer-account deployment** — which shape v1 targets; changes what Piece 2 hardens into.
- **Is the tier model still** free = own credentials, no parallelism; beginner = some house spend; advanced = more?
  Piece 6 encodes it.
- **How much of v0 is allowed to be visible** on impromptune.com while it is explicitly disposable.

## Order

Piece 0 first (it is nearly free and it feeds Piece 6). Then **Piece 1 → 2 → 3**, which is the spine: versions,
serving, traces. Pieces 4 and 5 can be built in parallel with the spine by anyone not working on it; Piece 6 needs
Piece 0; Piece 7 is a rule from the first new table onwards.

The slice is done when a ticket arrives from the connector, is answered by a served version, the answer is
corrected, and that correction becomes a label that improves the next version — once, on one box, with all of the
asterisks above intact.
