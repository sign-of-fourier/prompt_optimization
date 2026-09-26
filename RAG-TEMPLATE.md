# RAG library template: a made-up company's knowledge base

**Status:** scoping. Written 2026-09-26. Order: parallelism bench → this → BETA.md.

One library template that shows retrieval earning its place: questions about a fictional company that no model and
no search engine can know, answered from that company's documents. It ships already embedded, so a beta tester
clones it and runs it on house Micro/Lite without spending anything on embeddings.

## The demo arc

1. **Without the retrieval step:** the pilot scores low. Nova answers confidently from what companies usually do,
   and gets it wrong.
2. **With the retrieval step:** the score jumps. That jump is the reason RAG exists, measured, not asserted.
3. **Optimize:** the answer prompt learns to use the retrieved text, to prefer the current policy over the old one,
   and to say "not covered" instead of inventing an answer.
4. **Compress:** retrieved chunks are most of the tokens in every call; the compression goal applies directly.

## The company

One company across the library: the same one whose tickets the triage examples route (plans free / pro /
enterprise, queues billing, shipping, refund, bug, account, retention, escalation). Triage the ticket, then answer
it from the knowledge base. Name to be picked by Mark, checked against existing trademarks before it ships.

**Kept messy on purpose (Mark, 2026-09-26):** the existing tickets mix a SaaS inbox product (timezone bugs, VAT
invoices) with physical goods that ship. The knowledge base keeps both, so shipping docs and software docs compete
in retrieval the way they would at a real company that sells devices and subscriptions.

## Content: the canon comes first

The review burden is the real cost, so everything is generated from one reviewed source of truth.

1. **Canon** (`canon.yaml`, ~150 facts). Every name, amount, quota, limit, date and version, each with an id:
   `refund.window_days: 45`, `plan.pro.seats: 10`, `sla.enterprise.first_response_hours: 2`,
   `policy.refund.v1.effective: 2025-03-01`. **This is the thing Mark reads line by line** (~1 hour).
2. **Counter-prior facts** (~15, tagged in the canon). Values that deliberately differ from what companies usually
   do: a 45-day refund window rather than 30, refunds only as account credit on annual plans, a seat limit that
   counts guests. These make the "without retrieval" score fail visibly instead of looking plausible.
3. **Documents** (~30, ~20k words, ~150-250 chunks), written from the canon. Mix:
   - help-centre articles (how to do X);
   - policy pages (refunds, data retention, acceptable use);
   - one plan-comparison table (tables are where chunking breaks, so it has to be there);
   - dated release notes / changelog (date questions);
   - a **superseded policy**: refund policy v1 and v2, both present, with effective dates. Retrieval returns both;
     the prompt has to learn to use the current one;
   - an internal escalation runbook.
4. **Mechanical check** (script, before any human reads the docs): every number, date, amount and proper name in a
   document must match a canon fact; anything that doesn't is flagged. Every Q&A answer must trace to a canon
   fact id and appear in its source document.
5. **Human read** of the documents (~20k words, ~1.5-2 hours) plus a spot-check of the Q&A.

## The dataset

~120 questions, short exact answers (a number, a name, yes/no, a date) so exact-match / numeric / contains scorers
work and no LLM judge is needed.

| Kind | Share | Example shape |
|---|---|---|
| single-fact lookup | ~40% | "How many seats does Pro include?" |
| counter-prior | ~20% | "How long do I have to ask for a refund?" |
| multi-document | ~15% | a policy page plus the plan table |
| dated / superseded | ~10% | "Which refund policy applied to an order on 2025-02-10?" |
| plan-conditional | ~10% | needs the customer's plan (later: the records step) |
| not in the knowledge base | ~5-10% | correct answer is "not covered"; tests invention |

Each row carries its source document ids and canon fact ids: provenance for review, and a retrieval hit-rate
alongside accuracy. Hold-out 20% as usual.

**Closed-book check before publishing:** run the dataset without the retrieval step. Any question Nova answers
correctly from priors is either rewritten or kept deliberately as a control, and labelled as such.

## What has to be built (mini RAG)

| Piece | Notes |
|---|---|
| Corpus storage | a `corpora` table plus chunk/vector files behind the storage seam (PLAN.md Piece 7) |
| Chunker | markdown-heading-aware, ~300 tokens with overlap; tables kept whole |
| Embedder | Titan v2 through bpto's `BedrockEmbedder`, **with a Budget** so it hits the per-user cap and `usage_log` |
| Retrieval step | a new step kind that runs in-process (not HTTP): input `query` from a row column, outputs `context` (top-k text) and `sources`; k is a step setting, not optimized. Frozen onto rows at fetch time for evaluation, live at serving, like every step |
| Versioning | a published version pins the corpus id, so re-uploading documents never changes what a version answers from |
| Bundles | the template carries documents plus vectors (embed model id and dims recorded); cloning does not re-embed |
| UI | a Documents area on the Data tab; a "Search your documents" step node |
| Limits | BETA.md's upload caps (20 files, 10 MB, ~2,000 chunks) |
| Tests | offline, `HashEmbedder` under the mock |

Rough effort: 3-4 days of engineering; the content written by Claude in-session, plus Mark's review (~3 hours).
Bedrock spend (house account): embedding the corpus once (Titan) plus the closed-book check, pilots and a few
optimization runs on Micro/Lite, well under a dollar; cost stated before any live run. Authoring is not a Bedrock
cost.

## Open

- Plan-conditional questions: pull the plan from the records step (reuses the entitlement example, adds the demo
  service and its key as a dependency), or put `plan` in the row for v1?
- Where the content lives while it's being reviewed: gitignored like `lead_qual_synth/` until approved, then into
  `studio/examples/` and `studio/sample/`.
