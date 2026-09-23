# Piece 4 design — external steps, and the entitlement-routing case that proves them

Draft, 2026-09-23. Written before any code, to be argued with. Companion to `PLAN.md` Piece 4.

The contract being settled: **a node is either part of the program (a prompt, which the optimizer rewrites) or not
(an external step, fixed, part of the environment).** Everything below exists to make that sentence true in code.

---

## 1. The case: entitlement routing

Ticket triage, but the right queue depends on *who is asking*, not only on what they said.

Rows carry `ticket_id`, `message`, `customer_id`. An external step looks `customer_id` up in a records service and
returns `plan`, `tenure_days`, `open_tickets`, `contract`, `renewal_days`. The ground truth is then **joint**:

| condition | queue |
|---|---|
| bug/outage **and** `plan == enterprise` **and** `open_tickets >= 2` | `escalation` |
| bug/outage otherwise | `bug` |
| refund-shaped **and** `contract` **and** `renewal_days <= 60` | `retention` |
| refund-shaped otherwise | `refund` |
| everything else | as today: `billing`, `account`, `shipping`, `other` |

**Why this case and not a simpler one.** If the label is a function of the message alone, the enrichment is
decorative: the optimizer will correctly learn to ignore it, and we will have built plumbing and proved nothing.
Here roughly a third of rows cannot be classified correctly from the message at any prompt length, so there is a
*ceiling* without the step — and a ceiling is measurable. The headline result is two runs on the same rows, step on
and step off, plateauing in different places.

**Dataset:** 120 synthetic rows (~96 train / 24 hold-out), ours, no customer data. Labels are generated from the
table above, so the rule is known exactly and disagreement is a real error rather than an opinion.

---

## 2. Where the step runs

It runs **inside the program spec**, not as a dataset-preparation action. That is forced by Piece 2: a served
request must go down the same path as an evaluated row, so if enrichment happened only at dataset-build time,
production and evaluation would diverge the moment a request arrived. One implementation (`app/steps.py`), two
callers (the run loop, the serving path).

**Placement in v0 is pre-program only**: a step whose inputs come from the row, running once before any prompt. Its
outputs become available to every module as placeholders named `{<step_id>_<field>}` — `{records_plan}`,
`{records_open_tickets}`. Flat names, because `{a.b}` is attribute access in Python's formatter; validation rejects a
collision with a dataset column.

Mid-graph placement — a step whose input is a *prompt's* output — is out of v0 and is the documented trigger for
upstream work in bpto. Nothing here forecloses it: the same manifest describes both, only the placement changes.

---

## 3. The manifest

A step is declared as data, never as Python a caller registers — downstream users define these through a UI and
never write code. Manifests are repo files (`studio/steps/*.json`) in v0, the way `studio/examples/*.json` are.

```json
{
  "step": 1,
  "id": "records",
  "version": "1.0.0",
  "name": "Customer records",
  "blurb": "Look a customer up by id: plan, tenure, open tickets, contract status.",
  "transport": { "kind": "http", "method": "POST", "url": "...", "timeout_s": 5.0, "retries": 1 },
  "auth": { "kind": "bearer" },
  "inputs":  [ { "name": "customer_id", "type": "string", "required": true } ],
  "outputs": [ { "name": "plan", "type": "string", "enum": ["free","pro","enterprise"] },
               { "name": "tenure_days", "type": "number" }, { "name": "open_tickets", "type": "number" },
               { "name": "contract", "type": "boolean" }, { "name": "renewal_days", "type": "number" } ],
  "cacheable": true,
  "cache_key": ["customer_id"],
  "price_usd_per_call": 0.0002,
  "tunables": [ { "name": "include_history", "type": "boolean", "default": false } ]
}
```

`tunables` are declared and passed through, **not optimized** in v0 — they are the hook for the later abstraction
where the tree carries values that are not prompts.

## 4. What goes in the project spec

```python
class StepSpec(BaseModel):
    id: str                          # node id on the canvas
    manifest: str                    # "records@1.0.0"
    manifest_sha: str = ""           # filled at publish time; this is what a version pins
    inputs: dict[str, str]           # step input -> dataset column
    credential_id: str | None = None
    tunables: dict[str, Any] = {}
    enabled: bool = True

class ProjectSpec(BaseModel):
    steps: list[StepSpec] = []       # alongside `modules`
```

`versions.BEHAVIOUR` gains `"steps"`, so the fingerprint moves when a step is added, removed or repointed — the
free extension predicted when Piece 1 was written. A published version pins `manifest_sha`, not just the id and
version string: "the step it was scored against" has to mean the exact declaration.

## 5. Frozen for training, live for serving

Two different sources, deliberately, announced to the user:

- **Evaluation reads a frozen snapshot.** The step's outputs are materialised onto the row **once, at capture
  time**, with `captured_at`, exactly like the label is. Every later run reads those values.
- **Production calls live.** A record lookup at serve time must return today's answer. That is the point of a step.

The reason is not cost, it is correctness. A row is labelled `escalation` because `open_tickets` was 2 *when the
ticket arrived*. Re-enrich it six weeks later, `open_tickets` is 0, and the prompt is now being optimized to answer
`escalation` from a record that no longer justifies it. The label and its features have come apart, and no number
in the run would show it. Freezing keeps the training set still; the alternative is optimizing against a moving
target.

The program is identical on both paths — one declaration, one implementation, which is what Piece 2 requires. Only
the source differs, and it is declared rather than incidental. Two alerts make the difference visible instead of
silent:

- the run reports the **age** of the dataset's frozen enrichment, and warns past a threshold;
- serving reports **drift**: how far live values have moved from the distribution the version was trained on.

The consequence for tracing is a requirement, not a caveat:

> **Every trace records each external step's inputs and outputs.** Without it a production answer cannot be
> explained after the fact, because the data that produced it has moved on.

That is a new `steps` column on `traces`.

## 6. Metrics — and what the optimizer can actually do with them

The step reports `latency_s`, `usd`, `cached`, `fail`, joined into the metric vector as `step.<id>.<name>`,
matching the existing `tokens_per_module.*` and `parse_fail.*` convention. Nothing is scalarized inside the step.

But here is the part worth arguing about, because it contradicts the loose way I described it earlier:

**Pre-program step cost is not optimizable within a run.** Every candidate prompt sees the same enriched rows, so
the fetch cost and latency are the same constant for all of them. Weighing that constant in the objective shifts
every score equally and changes no ranking. It is *reported*, not *traded off*.

What **is** optimizable, and genuinely interesting: the enriched fields are placeholders, so a prompt that pastes
`{records_tenure_days}` into the context pays tokens for it. Under a compression objective the optimizer will
discover **which enriched fields are worth their tokens** — pulling five fields and using two is a finding. That is
the within-run trade-off; the fetch cost only becomes a live decision with mid-graph placement, where a prompt can
choose whether to call at all.

## 7. Failure

A timeout, a 5xx, or a response that does not match the declared output schema marks **that row** with an error and
the run continues; the row contributes `step.<id>.fail = 1`. If more than 25% of rows fail, the run aborts with the
endpoint's error rather than optimizing against a broken environment — an optimizer handed a dead dependency will
happily learn to work around it.

At serve time a failure is already a 502 with a recorded trace; that stays.

## 8. Validating a step

Firing junk at an endpoint and checking it returns 200 tells you the server is up and nothing else. The failures
that cost money are about the **relationship between the step's output and the label**, and about **stability** —
and almost none of that needs a model call. It is arithmetic over columns plus about forty HTTP calls.

Findings use the existing `Issue(level, stage, where, message, data)` shape, so they render in the validation panel
already built and an `error` blocks Run the way a mapping error does today.

**Tier 0 — static, free, no network.** Manifest parses and declares a version; every required input is mapped;
`cache_key` is a subset of the inputs; output names collide with neither dataset columns nor another step's
outputs; every placeholder a prompt uses exists in (columns ∪ step outputs); a credential is selected. All `error`:
none of it is a judgement call.

**Tier 1 — one or two calls.** Reachability and auth. The response conforms to the declared output schema — types,
enums, required fields. And the one everybody forgets: **how does it say "no record"?** 404, 200 with nulls, an
empty object — that decides whether a missing customer is a failed row or a legitimate null, and production is full
of them. Ask it for a deliberately unknown id and record the answer as part of the step's known behaviour.

**Tier 2 — a sample of ~20 rows. The tier that matters.**

| check | what it catches | level |
|---|---|---|
| **Coverage** — fraction of rows that get a record at all | 40% empty means the joint rule is unlearnable for those rows, and you should know before spending | error below a floor, else warn |
| **Stability** — call the same key twice, compare | if it differs, caching is invalid and evaluation is not reproducible | error |
| **Freshness skew** — frozen snapshot vs live for the same keys | how much has moved, and how old the snapshot is: §5's alert as a number | warn |
| **Signal** — each field against the majority-class baseline, permutation-tested, **and all of the step's fields together** | a step that carries nothing is decorative, proven for zero tokens. The joint test is not optional: see below | info per field, warn only if the joint test also finds nothing |
| **Degeneracy** — constant across all rows, or unique per row | carries no information and burns tokens on every call | warn |
| **Leakage** — one field predicts the label almost perfectly | usually the answer smuggled in (someone enriched with `assigned_queue`) | error |
| **Redundancy** — leave-one-out: joint accuracy with each field removed | a field the others already imply. This is the check that answers "can we drop it" directly | warn, listing the droppable fields |
| **Payload waste** — outputs no prompt references | no extra call and no tokens, but data you are holding for no reason - a privacy finding, not a cost one | warn |

The signal test is the one that distinguishes a serious tool: shuffle the field a couple of hundred times, see
where the real entropy reduction falls. It is also the check that would have caught the lazy version of this very
demo — bolt a lookup on, hope it matters.

**Tier 3 — the existing pilot, with the step on.** Twelve rows, real added latency and cost per row, folded into
the run's cost projection so the number the user approves includes enrichment.

Cost: Tiers 0 and 2 are **zero model calls**. Tier 1 is two HTTP calls, Tier 2 about forty. Only Tier 3 spends, and
it is the pilot that already runs.

## 9. Credentials

A step credential is a label and a secret — a different shape from a model credential (provider, config, model
list). It gets its own table in `store.py` rather than bending `credentials.py`, reusing that module's Fernet key
for encryption. Per `PLAN.md` ground rule 2: a component built for a different job gets a sibling, not a patch.

## 10. The demo endpoint

A standalone service in `steps/records/`: synthetic records, one `POST /records`, checks a bearer key, declares a
price, and sleeps a realistic 60–120 ms so the recorded latency is not a loopback fantasy. Its own process, its own
port, its own systemd unit. **Not** under `studio/app/` — if the studio is the endpoint, the boundary is fiction.

The acceptance test for the whole piece: *point the manifest at a real CRM's URL and nothing in `studio/app/`
changes.* Any hardcoded URL, bypassed credential or special case in the studio means the piece failed.

It doubles as the reference implementation handed to anyone who wants to offer a plugin: a working example and its
manifest, which cannot drift from reality the way prose does.

## 11. Canvas

A Steps palette listing available manifests. Dragging one on gives a visually distinct node — square, muted,
labelled "external · not optimized" — with a panel to map its inputs to columns, pick a credential, and see the
placeholder names its outputs provide. Static validation gains: unmapped required input, missing credential, output
name collision, and a step whose outputs no prompt uses (warn — you are paying for data nobody reads).

## 12. Done when

1. A manifest loads, lists, and validates; an unmapped input is a blocking error.
1b. A step wired to a field with no relationship to the label is flagged before any run starts, and a field that
   leaks the label blocks Run outright.
2. Two candidates in one run cause **one** fetch per row, not two (asserted against a counting stub).
3. A deliberate 500 on one row errors that row, the run finishes, and `step.records.fail` shows up on it.
4. A served request calls the step live, `required_inputs` asks for `customer_id` and `message` but not
   `records_plan`, and the trace records what the step returned.
5. Publishing pins `manifest_sha`; editing the manifest changes a new version's fingerprint.
6. **The headline:** two runs on the same 120 rows, step on and step off, plateau at different accuracies.

## 13. Cost

Item 6 is the only real spend. Using Piece 0's formula (E=96, S=2, R=10, mb=5, acceptance 0.45, ~15% cache):
roughly **1,200 gross / 1,000 billed calls per run, about $0.015** on Micro + Lite — so **about three cents for the
pair**. Enrichment calls are free (our own service). Everything in 1–5 runs on the mock at zero cost.

## 14. Asterisks

Pre-program placement only · HTTP POST JSON only, no CLI, no MCP · manifests are repo files, no registry and no
third-party submission path · tunables declared but not optimized · evaluation caches while production does not,
so the environment drifts between them and traces are the only record · loopback latency is not WAN latency ·
validation samples 20 rows, so coverage and signal carry sampling error at small n · no sandbox and none needed — we call out, nobody's code runs on our box, which is exactly the safety property the
builder-agent approach in the strategy doc gives up.
