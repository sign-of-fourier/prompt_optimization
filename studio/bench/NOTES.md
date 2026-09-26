# Parallelism bench: notes (2026-09-26)

Internal. Not yet a source for site copy: the publishable run is in the backlog (below).

Harness: `parallel.py` (in-process, own data dir, own account). bpto pin 896418b. Models: Nova Micro eval, Nova Lite
reflect. Reference result: bpto `experiments/2026-09-19-ladder-batch-q` (surrogate batch at q = 2/4 reaches q = 1's
accuracy in far fewer rounds, on a mock at 600-2000 rollouts). These runs measure our implementation, bpto against
itself; GEPA's own parallelism is not a comparison we make.

## What the runs found

| Run | Output | Finding |
|---|---|---|
| entitlement, 4 arms x 3 seeds, 24 rewrites, 16 in flight ($0.12) | `out/20260926-154900` | **Bug:** the studio's BO expanded min(q, pool) parents, so q = 4 on a one-node pool was q = 1. q2/q4 ran 6-16 rewrites instead of 24, and looked fast because they did less. Fixed in `app/compile.py` (extra independent reflection calls per parent until the round proposes q). Also: the retry counter matched botocore's "Not retrying request." - fixed. |
| compression, 4 arms x 5 seeds, 24 rewrites, 16 in flight ($0.23) | `out/20260926-161627` | Equal rewrites now. bo-q4 111 s vs bo-q1 148 s (-25%). A q4 round took 15.6 s vs 6.6 s: the 16-call cap on full evaluations was the bottleneck. Holdout only 10 rows, too coarse for accuracy claims. |
| entitlement, bo-q1 vs bo-q4 x 5 seeds, 24 rewrites, 64 in flight ($0.14) | `out/20260926-170721` | bo-q4 56 s vs bo-q1 175 s (3.1x). But q4 passed fewer gates (0-6 vs 2-6) and its train best was lower in 4 of 5 seeds: 24 rewrites is only 6 rounds at q = 4. Holdout flat for both (0.833; neither improved on it). |

Across all runs: 0 Bedrock throttling retries at 16 or 64 in flight; cost per run $0.003-0.026.

**Serving while runs go** (one process, a served request every 5 s):

| In flight | idle p50 / p95 | during runs p50 / p95 |
|---|---|---|
| 16 | 0.76 / 0.92 s | 0.60-0.77 / 1.3-1.7 s |
| 16 (2-step program) | 1.15 / 1.59 s | 0.97-1.83 / 2.35-2.47 s |
| 64 | 0.77 / 0.87 s | 0.66 / 4.5-4.8 s |

Zero failed requests in 802. The tail degrades with parallelism: unlimited-parallel runs next to serving need a cap
or a reserved serving quota (PLAN.md trigger "production request queued behind an optimization run").

Other fixes from these runs: Bedrock's intermittent `ResourceNotFoundException: Inference Profile ARN not found`
(about one in five runs, each scored an example as a failure) is now retried in `RoutingClient._complete`.

## Backlog: the publishable run

bo-q1 vs bo-q4 on entitlement, 5 seeds, 64 in flight, **72 rewrites** (18 rounds at q = 4) so q = 4 has room to
work. Report time to reach bo-q1's final train score, and holdout accuracy. Projected $0.35-0.50 (cap $1.50),
about an hour. Then this file gets the table the site links to.
