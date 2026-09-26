# Beta test plan

**Status:** planning, in the backlog. Written 2026-09-26. Order of work: parallelism test → RAG → this plan.

A fixed, named set of beta users (not "anyone with an invite") runs Impromptune on house Micro/Lite with a
$1 allowance, and tells us where it breaks. Their accounts are assigned by hand; everyone else who signs up lands
on `free`.

## Decisions so far (Mark, 2026-09-26)

- **New signups default to `free`.** `beta` is assigned to named users by an admin command; there is no invite
  code and no self-serve way onto it.
- **$1 of house spend per beta user, one-time.** Testers cannot raise it. The run's `max_usd` becomes a server-set
  ceiling (clamped to what is left, never `None`), shown read-only.
- **Beta is house-only.** Adding their own model keys means they have moved to `free`, and are a real user.
- **Beta parallelism is unlimited** (no `max_q` / `max_concurrency` cap beyond a sanity ceiling), because we need
  the metrics. Measured 2026-09-26 (`studio/bench/NOTES.md`): at 64 calls in flight, a served request's p95 went
  from 0.9 s idle to 4.5-4.8 s. Unlimited parallelism next to serving needs a cap or a reserved serving quota.
- **Feedback is in-app**: a Feedback button that stores user, page, project, run and text, plus a mission
  checklist. No external form.
- **RAG is point-and-click.** The target user does not know what a knowledge base is; "bring your Bedrock KB id" is
  at most an advanced option, never the default path.

## Prerequisites this plan trips (from PLAN.md, Migration triggers)

Beta testers uploading their own data is **the first outside dataset**, and several of them at once is **the second
concurrent tenant**. Per PLAN.md those promote real pieces, not patches:

- retention and deletion policy, privacy policy (and a DPA if a tester is a company) - before the first upload;
- tenant isolation on datasets, runs and traces (today: ownership checks in SQL on one sqlite file);
- runs are in-process and share one event loop with serving. With unlimited parallelism, one tester's run can
  starve another's served request. The parallelism test should measure exactly that.

Decide per trigger: build it, or accept it for a named, small beta and write the asterisk down.

## What we ask them to test

| # | Mission | What it tells us |
|---|---|---|
| 1 | Clone the ticket-triage example → pilot → run → read the result | Can someone get to a first run unaided? Is the run report legible? |
| 2 | Their own prompt and 30-100 rows | Data mapping, validation messages, choosing a scorer - where real users stall |
| 3 | Add tokens to the objective on the same project | Does compression hold on their task (feeds promptcompression.ai) |
| 4 | Publish a version → call it → attach outcomes → promote traces to a dataset → re-run | The observability loop, and the "where do I get labelled data" answer |
| 5 | (once built) Clone the knowledge-base template (RAG-TEMPLATE.md) and attach documents as a retrieval step | Whether RAG is point-and-click enough for someone who has never heard of a knowledge base |

Passive signal alongside: `usage_log`, run `events.jsonl`, traces.

## Landing page (`impromptune.com/beta`, noindex, not in the sitemap)

Explains, then hands off to the missions. Less CTA, more "here is what it does":

1. **What it does, in one line:** you give it a prompt and examples; it rewrites the prompt and shows, with numbers,
   whether the rewrite is better.
2. **How it's different:** it searches instead of guessing, scores the whole program including external steps,
   can treat tokens as an objective, and shows what it tried and rejected. Link quantecarlo.com findings; do not
   restate figures (CLAUDE.md).
3. **"Where do I get labelled data?"** You already have outcomes (resolved tickets, CRM stages, human edits); a few
   dozen rows is enough to start; or publish the prompt you have now, log real calls, mark outcomes, and those
   become the dataset.
4. **Prompt observability without a platform:** every call to a published version is a trace (inputs, output,
   tokens, cost, latency, path); outcomes attach to traces; traces become training data. One HTTP endpoint, no
   SDK, no dashboards to adopt. What it isn't: an APM or a log warehouse.
5. **Your allowance** ($1, the in-app meter) and **how to report** (the Feedback button, the mission checklist).

Describe only what exists: publish + HTTP endpoint + traces + outcomes. No chat page, no access modes, no
deployments dashboard - those are designed, not built. GLOSSARY words apply (prompt vs step; no bare "credential").

## Caps and limits

**The ledger.** A `house_credit` table: one row per grant (`beta` $1.00 now; a Stripe top-up or subscription
allowance later). Remaining = sum(grants) − house `usd` in `usage_log`. Stripe records what was paid for; it does
not stop spend in real time, so enforcement stays here.

**Enforcement.** One per-user parent `bpto.Budget(max_usd=remaining)`, attached to every client that spends house
money: run, pilot, serve, and the BO embedder. bpto checks the parent before each call and charges it after, so
concurrent runs share one meter. (Holds while the service is one uvicorn process; more workers means checking the
DB instead.) Worst-case overshoot = the calls in flight when the cap is hit.

**Holes to close** (all spend house money today without a dollar cap):

1. `POST /v/{vid}/run` with an API key - loopable; capped only at `max_steps + 2` calls per request. Add the parent
   budget and a requests-per-minute limit.
2. Pilot `rows` is unbounded; its call cap scales with it. Clamp it.
3. Concurrent runs across projects each get their own `Budget`. The parent budget fixes spend; parallelism itself
   stays unlimited for beta.
4. Per-run `max_usd` is user-set and may be `None`. Clamp server-side.
5. `runs.py` builds `BedrockEmbedder` without a budget: its Titan calls reach neither `Budget` nor `usage_log`.
6. Signup: no email verification and `DEFAULT_TIER=beginner`. Fixed by the `free` default.

**Upload limits** (proposed numbers, to settle): there are none today beyond nginx's 50 MB body.

| What | Proposed cap |
|---|---|
| Dataset rows | 2,000 |
| Dataset file | 5 MB |
| One cell | 20,000 characters |
| Retrieval documents (RAG) | 50 files, 10 MB total, 2,000 chunks (built; 20 files was proposed, but our own knowledge-base template has 32) |

Capping these also caps embedding spend and the size of any single call.

**Backstops outside the app:** a global daily house ceiling (`STUDIO_HOUSE_USD_DAILY`) and an AWS Budgets alarm on
the Bedrock account.

## Regulated data: out of scope for this iteration

Beta terms say plainly: no patient data, no personal data you are not allowed to share with a vendor. The answer to
"I can't use your tool, it's not secure" (a clinic, 2026-09-26) is scoped later; its shape, so it doesn't evaporate:

- **The goal:** once uploaded, data can be made unviewable to people who build on it (an agency or vendor setting up
  the clinic's prompts), while the clinic's own authorised staff can still see and label it.
- **Sealed has to cover every copy**, not just the document browser: frozen dataset columns, traces, the run cache
  (it stores every prompt sent), event logs, exports. Otherwise "not viewable" is a UI claim.
- **The optimizer can work on data no builder sees**: reflection is a model reading rows, humans see scores. But a
  reflected prompt can copy a specific patient's details into its rules, and the builder does see prompts, so
  rewrites of sealed data need a PII check before they are shown.
- **Compliance is more than a toggle**: a BAA (HIPAA) or DPA (GDPR special-category data) with us and every
  subprocessor, including the model provider (check which Bedrock models and regions are HIPAA-eligible),
  encryption, access logs, retention and deletion, breach notification.
- **The strongest answer is probably deployment in the customer's own cloud account**, so the data never leaves it.
  That is PLAN.md's open decision "hosted multi-tenant vs customer-account deployment"; a clinic is the case that
  decides it.

## Open

- **Tiers after beta.** Draft: free (own keys, no parallelism), beta (above), beginner and advanced (house spend
  from a subscription allowance or credits). Does free get serving? Does a tester who moves to free keep the rest
  of the $1?
- **Pricing model: subscription vs tokens.** Needs the cost analysis: the distribution of spend per user per
  week, which beta produces. A run is bursty (most of its spend lands in minutes), so any windowed allowance must
  fit at least one typical run, and the pre-run projection (PLAN.md Piece 0) should refuse a run that won't fit
  the window rather than stop it halfway.
- **Upload limit numbers** above.
