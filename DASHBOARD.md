# The dashboard: what a hosted version is doing

**Status:** design, not built; clickable mock-up with sample numbers in `mockups/dashboard.html` (accepted as the layout, 2026-09-29). Written 2026-09-29 from Mark's decisions the same day. Depends on hosting
(`deploy/SERVE-BOX.md`, `studio/app/hosting.py`), which is built and accepted.

Once someone hosts a version, what matters after login is what it is doing: traffic, errors, speed, cost and
whether it is still right. Projects become the place to start or continue building. This is the observability half
of prompt ops; the other half (optimizing) is the studio as it stands.

**Observability is anchored to the dataset.** A hosted version was scored on a labelled dataset, and that score is
the reference every live number sits beside. When either side moves - the dataset gains rows (reviews, corrections,
an upload) or the program changes on the canvas - the hosted version is out of date, and the dashboard says so and
offers one **Update**: score first (cheap), optimize if it is worth it. That loop,
live traffic -> labels -> dataset -> score -> new version, is what trace-centred tools (Braintrust and the like) sell
as their core; ours closes it with the optimizer, and the Jev line stands in for labels where nobody reviews.

## Words

**Hosted version**: a version that answers traffic on the serving box, at `/serve/v/{id}/run`. GLOSSARY.md still
bans "deployment" for a *version*; it gains "hosted version" for the running thing. UI copy says "Host this
version", never "deploy".

## Who sees it

- Hosting is gated by **tier**: advanced and above (`tiers.py` gains `hosting: bool`; free and beginner are false).
  Labelled **beta** while it is new. No invitation list.
- **Landing:** after login, a user with at least one hosted version lands on the dashboard; everyone else on
  Projects, as today.
- The serving box being stopped is internal (the dev placeholder). Production is always on; the dashboard has no
  "box offline" state for customers.

## Hosting from the UI

A **Host** button on each version in the run view's Versions card. The confirmation says it is billed (AWS cost
passed through, plus a surcharge) and shows an **estimated cost per request** from the version's evaluation
(average tokens per row × model prices, loop steps included), plus the Jev line's per-request cost if it is on. Hosted versions carry a "hosted" chip and an **Unhost** action. No roll back in v1: a version's URL
contains its id, so switching versions means changing the caller's URL; a stable per-project alias is later work.

## The screen

**Top strip (account):** requests today · spend this month, actual and forecast, against the plan's cap · live hosted
versions · open alerts.

**One row per hosted version** (the whole screen for most users):

| Column | Source | Notes |
|---|---|---|
| Name, version label, project | versions | links to the drill-in |
| Requests, 24 h, with sparkline | traces | |
| Error rate | traces.error | |
| p50 / p95 latency | traces.latency_s | |
| Cost: today, month to date, forecast, per request | usage_log (purpose serve) | see *Cost* |
| Earned | version score / hold-out | what it scored in evaluation; the row shows **out of date** when the data or the program has changed since (see *Out of date, and Update*) |
| Reviewed | outcomes from spot-checks | "8 of 10 right (n=10)"; blank until reviews exist |
| Quality **(beta)** | Jev on each trace | see *Jev quality line* |
| Status | the above | healthy · errors · slow · over budget |

**Time:** 24 h / 7 d toggle, nothing else. Data is at most a minute old (the ledger pull runs every 30 s); the
screen says "updated <time>".

## Cost: actual and expected

Cost is observability, shown the way AWS shows it.

- **Actual:** today, month to date, and per request, from `usage_log` rows with `purpose="serve"` for that hosted
  version, at the customer's price (pass-through plus surcharge, once billing defines it; until then, the AWS cost).
- **Forecast (month end):** month to date + days remaining × the average daily spend of the last 7 days (fewer if the
  version is newer). Beside it, last month's total and a trend arrow (last 7 days against the 7 before). Labelled
  "forecast"; a version hosted under 3 days shows "too new to forecast".
- **Expected per request** at hosting time comes from the evaluation's tokens; the dashboard shows actual per request
  beside it, so a version that costs more live than it did in evaluation (longer inputs, more loop steps) is visible.

## Out of date, and Update

One concept, whatever changed: *is the hosted version still the best we can do, given everything we now know?*

**What makes a hosted version out of date** (each listed under "since this was scored"):

- **new labelled traces**: "14 reviewed, 5 wrong" (spot-checks or API outcomes)
- **dataset changes**: rows uploaded, added or edited for any reason. Needs PLAN.md's content hash on datasets:
  today a version pins its dataset by id, and a dataset's rows can change under the same id.
- **program changes**: the project's canvas fingerprint (`versions.fingerprint`) differs from the hosted version's.

**The badge** on the row reads "out of date · 3 changes" and gets louder in steps: grey (something changed),
blue (enough has built up to be worth it, see *Hints*), amber (random reviews put live accuracy below Earned by more
than the noise band). Nothing runs by itself: every step costs money. (Later, advanced: "check weekly, within $X".)

**Update** is one button, two depths, cheapest first:

1. **Score** (cents): the hosted version - and the edited program, if the canvas changed - on the current data.
   Results are shown **per source, side by side, with no explanation attached**: "original rows 82% (unchanged) ·
   14 new labelled traces 58% (n=14)". A drop on new rows could be drift, the reviewer's choice of traces, label noise
   or chance, and nothing in the data tells those apart, so the screen does not guess.
2. **Optimize** (a run, cost shown first), offered from that result: starts from the hosted version's own spec (or
   the edited program's) - never by overwriting the canvas - on **the original dataset plus the new rows**, with a
   hold-out drawn from both. The winner is offered only if it beats the starting point on the same held-out rows by
   more than the noise band, with accuracy and cost per request before and after, and the rows it now gets right and
   newly gets wrong. It says "better on these rows", not "better live". Hosting it replaces the hosted version (new
   URL; no aliases in v1).

**Provenance, and what counts as an estimate.** Every label records how it arrived: *random review* (the review
queue's random sample), *picked* (a user opened a trace and labelled it), *API*, *uploaded/edited*. Only random
reviews estimate live accuracy ("about 76% of live requests right, n=30"); the rest are training examples and shown
as counts. Comparing two prompts on the same rows survives an unknown bias far better than any absolute number,
which is why Update's verdict is relative.

**Bias is allowed, and taught, not enforced.** We cannot force an experimental design, and do not try. The review
queue defaults to random and says why in one line; the docs say it plainly: label a random sample of real traffic.
Labelling only failures still works - the dataset swings toward each problem as it appears and becomes
representative over time - it just makes the path to a good prompt slower. Weighting rows by provenance is later
work.

**Hints** (never blocking: people update early just to see):

- **wrong answers found** drive the suggestion, since reflection learns from failures: "about 10 wrong answers make
  a run worth it" (proposal);
- **total labels** decide whether a before/after comparison means anything: 30 at least, about 10 per class seen for
  more than two classes (proposal);
- **many tiny classes** (most with under 3 labels): consider grouping them - no amount of waiting fixes a class that
  almost never occurs.

## Alerts (fixed rules, no configuration in v1)

- error rate over 5% in the last hour, with at least 20 requests
- p95 latency over twice last week's p95
- forecast above the plan's cap
- loops: over 10% of requests hitting the step cap (`capped` metric)
- routers: any route's share moved by more than 15 points against last week (input drift, no labels needed)
- reviewed accuracy below the earned score by more than the noise band, once there are 20+ reviews

Shown on the row and counted in the top strip; email later.

## Drill-in (one hosted version)

- **Traces:** recent requests (input snippet, output, path, latency, cost, error, review), filterable by errors,
  reviewed, and route.
- **Trace detail:** each prompt's and step's input and output, from the trace's path and step outputs.
- **Review 10 traces:** input and output side by side; the user marks right or wrong, or types the right answer. Each
  becomes an outcome on that trace, recorded with how the trace was chosen. The sample is **random recent traces**
  by default, with one line saying why (only a random sample estimates live accuracy); a "suspicious first" toggle
  (errors, capped loops, low Jev quality) helps find problems, and its reviews count as training examples, not as
  an estimate.
- **Update:** see *Out of date, and Update*.
- **Version history:** read-only in v1.

## Jev quality line (beta, advanced, billed)

A label-free signal, because most traces never get reviewed. Jev answers one yes/no question per trace with a
probability, e.g. "Is the answer supported by the input and does it answer what was asked?". Shown as a sparkline
and a daily average, labelled **Beta**.

- Why it may work: Jev is repeatable and graded, and fast enough for every trace (bpto
  `experiments/2026-09-27-jev-judge-pilot`). Why it may not: that pilot tested comparison against a reference, not
  this; "supported by the input" does not catch a wrong sum or a plausible wrong queue.
- So it validates itself on the page: where reviews exist, the column shows how often Jev agreed with them ("agrees
  with 17 of your 20 reviews"). A project where it disagrees shows that, rather than a confident line.
- An **advanced-tier** feature, switched on per hosted version, and **billed**: its calls are part of the customer's
  pass-through bill plus surcharge, shown as their own line under Cost. Needs Jev's real price first (the pilot did
  not check OpenRouter's). Behind the server's Jev switch and key; its calls have their own cap.

## Data shape

Aggregates (counts, latency percentiles, spend, route shares, Jev averages) are computed from the rows into a small
per-hosted-version, per-hour table. The row screens read aggregates; only the drill-in reads trace contents. Keeping
the two apart now is what makes the max-security tier possible later (trace contents stay on the tenant's box; the
studio only ever holds aggregates).

## Built so far

- **Step 1 (2026-09-29):** tier gate, Host / Unhost with the per-request estimate, hosted chip. The estimate matched the
  first live request to three significant figures ($0.0000147).
- **Step 2 (2026-09-29):** `GET /dashboard?window=24h|7d` (`studio/app/dashboard.py`) and the Dashboard view; landing
  on it when anything is hosted; a row opens the project's Serving tab (the drill-in is step 3). Asterisks:
  - computed on request from `traces`, not an hourly rollup table - `summary()` is the one entry point a table replaces;
  - cost is each trace's own `usd` (usage_log rows do not name the version), as AWS cost: no surcharge exists yet;
  - no plan has a spending cap yet, so the strip shows spend and forecast without a cap bar;
  - the account forecast appears only when every hosted version is 3+ days old;
  - Reviewed, Quality (Jev), the out-of-date badge and alerts are not shown until their steps exist; the one status
    rule is errors over 5% with 20+ requests, else healthy, or idle with no traffic.

## Not in v1

Customer-side outcome connectors (helpdesk, CRM) - a separate lift, PLAN.md Piece 5; the outcomes API stays
documented for developers. Roll back and stable aliases. Custom charts, queries, user-built dashboards. Email and
integrations for alerts.

## Open

1. The hints' numbers (10 wrong; 30 labels; 10 per class) are proposals, to settle against a real run.
2. Jev's price on OpenRouter, before the quality line can be billed.
3. Dataset content hashing (PLAN.md asterisk) is a prerequisite for "dataset changed".

## Build order

1. `tiers.hosting`, the Host / Unhost buttons (with the per-request estimate) and the hosted chip; landing logic.
2. Hourly aggregates from traces + usage_log; the row screen and top strip; cost actual and forecast.
3. Drill-in: traces, detail, review queue (outcomes), version history.
4. Out of date: dataset content hash, provenance on labels, the change list and badge, Update's Score step.
5. Alerts.
6. Jev quality line (beta, advanced, billed), with its agreement-with-reviews figure.
7. Update's Optimize step, with its hints.
