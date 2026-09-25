# What is here to stay, and what is scaffolding

Written 2026-09-25. Companion to `PLAN.md`, which says v0's implementations are disposable — this says *which ones*,
so nobody has to guess in six weeks whether a file is load-bearing.

Three categories:

- **Keep** — maintained, expected to outlive v0. Breaking one is a bug.
- **Scaffolding** — deliberately disposable, with the trigger that retires it. Not a bug to delete once that fires.
- **Ad hoc** — a one-off from a single investigation. Nobody maintains it; assume it is stale.

---

## Tests — all keep

Every test is offline (`MockClient`, or a real HTTP server on a loopback socket). None costs money. `cd studio &&
python -m pytest -q`, 29 tests.

| file | what it protects | keep because |
|---|---|---|
| `studio/tests/test_compile_and_validate.py` | spec → bpto compilation, graph and mapping validation, the pilot, cost projection, version pinning and fingerprints | the contract between the canvas and bpto; a change here is a change to what the product means |
| `studio/tests/test_api.py` | the whole HTTP surface in one flow: auth, projects, datasets, validation, runs, bundles, credentials, tiers, versions, serving, traces, outcomes, promotion | the only thing that catches a route breaking another route |
| `studio/tests/test_steps.py` | external-step transport, 404-means-missing, schema conformance, single-flight caching, frozen enrichment, and the validation checks | the checks give users advice; a regression here gives *wrong* advice, which is worse than none |
| `studio/tests/test_oauth.py` | single-use `state`, expiry, encryption at rest, refresh before expiry, reconnect-replaces, provider errors | security properties, where the failure is silent and the blast radius is someone's account |

Two of these earned their keep by catching real defects while being written: the per-field signal check called the
load-bearing field noise, and the leave-one-out statistic rewarded a pure-noise column until it was cross-validated.

## Product — keep

| path | what it is |
|---|---|
| `studio/examples/ticket-triage.json` | the tutorial project: 2 prompts, 50 rows |
| `studio/examples/ticket-triage-compress.json` | the compression walkthrough the marketing site screenshots |
| `studio/examples/ticket-triage-entitlement.json` | the external-step case: 1 prompt, 1 step, 120 rows |
| `studio/sample/tickets.jsonl` | 50 labelled tickets, referenced by the first two examples and downloadable from the Data tab |
| `studio/sample/tickets-entitlement.jsonl` | 120 rows whose labels depend jointly on the message and the customer record |
| `studio/steps/records.json` | the step manifest the entitlement example points at |

## Scaffolding — disposable, with triggers

| path | what it is | retire when |
|---|---|---|
| `steps/records/service.py` | the demo external endpoint: synthetic records, bearer auth, injected latency | a real third-party endpoint replaces it. Until then it doubles as the reference implementation for anyone writing a plugin |
| `steps/records/records.json` | 70 synthetic customers | with the service |
| `steps/records/generate.py` | regenerates both the records and the entitlement dataset, deterministically | never, while the entitlement example exists — it is how the dataset's ceiling stays provable rather than asserted |
| `deploy/records.service` | systemd unit for the demo endpoint | with the service |
| `studio/app/mock.py` | the schema-generic offline model | never, while tests must not hit the network |

The whole of `studio/app/` is v0 by `PLAN.md`'s ground rules and expected to be replaced piecemeal; the table above
only names things that are scaffolding *even by v0 standards*.

## Ad hoc — not maintained

One-off scripts written to answer a single question live in the session scratchpad **outside this repository**
(`/tmp/claude-*/scratchpad/`) and are not committed. Recent ones: an end-to-end clone-enrich-validate walk, a
blind-arm comparison, a zero-round baseline check, and the per-class confusion analysis.

That last one is the pattern worth noticing: **when an ad-hoc script keeps getting re-run, it wants to be a
feature.** The confusion analysis was scripted three times before it became the per-class table in the run view.
The rule: script it once to answer a question; if you script it twice, put it in the product.

`studio/data/` (sqlite, datasets, run artifacts) is local state, gitignored, and neither keep nor scaffolding —
it is the record of what was actually run, and the runs referenced in `PLAN.md`'s Piece 0 measurements live there.

## Credentials, and where they belong

Worth writing down because it was got wrong once: `HUBSPOT_TOKEN`, a personal Service Key, sat in the server-wide
`.env`, where it would have been every user's credential.

| kind | scope | where |
|---|---|---|
| Model provider keys (house) | the server | `.env` — these are ours, spent on behalf of paid tiers |
| Model provider keys (a user's own) | per user | `credentials` table, Fernet-encrypted |
| External step keys | per user | `step_credentials` table, Fernet-encrypted |
| OAuth **client id / secret** | the server | `.env` — this identifies *the studio* to the provider, and is correctly shared |
| OAuth **access / refresh tokens** | per user | `connections` table, Fernet-encrypted |
| Workspace API keys (inbound) | per user | `api_keys` table, sha256 only — never recoverable |

The rule: a credential that identifies **the platform** belongs in `.env`; a credential that belongs to **a person**
belongs in a table, encrypted, scoped by `user_id`. Anything personal in `.env` is a bug.

`STUDIO_SECRET` is the Fernet key for all of the above. `.env` briefly held **two different values** of it;
`load_env` uses `setdefault` so the first won, and anything encrypted under the second would have been unreadable.
Only one encrypted row existed and the winning key decrypted it, so nothing was lost — and `credentials._fernet()`
now reads `.env` before generating, so a script that skips `load_env` can no longer append a second key.
