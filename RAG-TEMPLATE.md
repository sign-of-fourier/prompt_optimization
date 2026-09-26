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
| Corpus storage | **built 2026-09-26**: `corpora`, `corpus_documents` (immutable; same name + new bytes = replace), `corpus_chunks` in `store.py`; originals as blobs via `store.blob_path`; V0-gated `/corpora` endpoints, all-or-nothing uploads under the limits. Vectors still to come |
| Chunker | **built 2026-09-26** (`app/corpora.py`, `md-v1:300/40`): heading path on every chunk, no chunk spans sections, tables and code whole (a table over 900 tokens splits by rows, header repeated), sentence overlap. The 32 kb docs make 96 chunks, median 35 tokens |
| Embedder | **built 2026-09-26** (`app/embedding.py`): `MeteredEmbedder` wraps any bpto embedder with its own `Budget` and one `usage_log` row per `embed()`; Titan v2 at 1024 dims live, `HashEmbedder` under the mock; vectors per (document, model) as float32 blobs. Upload embeds automatically; `POST /corpora/{id}/embed` retries. The BO embedder in `runs.py` now goes through it too (metered, capped at the run's `max_usd`). Live check: the 32 kb docs cost $0.0001 (5,024 tokens). Per-user parent Budget still BETA.md's |
| Retrieval step | **built 2026-09-26**: `steps/retrieval.json` ("Search your documents"), transport kind `local` (the caller supplies the function; `corpora.make_retriever`, cosine over normalised vectors). Input `query`; outputs `context` (top-k passages, each tagged `[document > section]`) and `sources` (`record_only`: kept on rows and traces, exempt from the unused-output warning). Tunables `corpus` (required) and `k` (default 4). Frozen by fetch-and-freeze, live at serving; the query's embedding is metered and added to the trace's usd. The label-statistics check skips it (a passage predicting the answer means retrieval worked) and points to the step on/off pilot |
| Versioning | **built 2026-09-26**: publish writes the document ids into the step's `tunables.documents` (so they join the fingerprint). Replacing a pinned file retires the old copy (kept for the version, gone from the set); an explicit delete is a real delete, names the versions affected, and those versions then refuse to run (502, recorded as a trace) instead of answering from less. Asterisk: pinned at publish, not at the fetch-and-freeze the run was scored on |
| Bundles | **built 2026-09-26**: a bundle's `corpus` carries document texts plus paid-for vectors per model (base64 float32; hash vectors are recomputed, free); library entries point at `sample/<name>.corpus.json`, built by `studio/tools/build_corpus.py`. Import re-chunks and reuses vectors only when the chunker version and chunk counts match, and points every search step at the new set. The kb corpus is built (`kb_synth/kb.corpus.json`, 534 KB, $0.0001) and waits for the content review before moving to `sample/` |
| UI | **built 2026-09-26**: Documents card on the Data tab (sets, multi-file upload, searchable/pending with a priced retry, a browser showing each file as its passages, delete that names affected versions); a "Search documents" palette item that pre-maps the query; step panel picks the set and k and offers to add `{<id>_context}` to the entry prompt. Clicked through in headless Chromium under the mock. The sealed (no-browse) option is not built |
| Limits | built: 50 files (raised from 20: our own template has 32), 10 MB, 2,000 chunks per set; `.md`/`.txt` only |
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
