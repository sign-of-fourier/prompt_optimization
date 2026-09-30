# Zendesk triage: plan and status

Use case 1: optimize a Zendesk triage prompt (group now; priority and a bounded set of tags later) against where the
team's own routing decisions ended up, **shadow mode first**. Every human routing decision becomes a label; the
optimized prompt writes its pick to a hidden field until the team has seen where it agrees with them. Periodic
re-optimization is the subscription.

It paused the dashboard's Update / weights design (DASHBOARD.md, "Update: guided feedback"). Weights should be designed
once, together with the label tiers below.

## Status (2026-09-30)

| step | what | where | state |
|---|---|---|---|
| 1 | hold-out by date (`split="time"`, `time_column`) + per-class hold-out report | `studio/app/splits.py`, RunView `PerClass` | live (`f891ac7`, deployed with `99c152d`) |
| 2 | synthetic Zendesk export with assignment history | `zendesk_synth/` (gitignored), `generator.py`, `summary.md` | done, local |
| 3 | label builder | `studio/app/zendesk_labels.py`, `zendesk_synth/grade.py` | pushed (`413c270`) |
| 4 | routing review ("roast report") | `studio/app/zendesk_roast.py`; [synthetic review](https://claude.ai/artifact/3Dv25SHSYmA8vZoY5skzDt) | pushed (`d23cdb2`) |
| 5 | real connector on a trial account | not started | **next** |

To rerun steps 2 to 4 on the synthetic data:

```bash
cd zendesk_synth && python generator.py && python summarize.py
cd ../studio && python -m app.zendesk_labels ../zendesk_synth/export ../zendesk_synth/labels.jsonl --all
python -m app.zendesk_roast ../zendesk_synth/labels.jsonl ../zendesk_synth/review.html --company Fernhollow
cd ../zendesk_synth && python grade.py
```

## What the synthetic data taught us (grade.md, summary.md)

These numbers come from a generator and a builder we wrote ourselves, so they test the plumbing, not reality.

- Kept labels (strong + medium): 96.1% agree with the sealed truth. The truth is readable only by `grade.py`.
- **Corrected labels are not the cleanest.** "Corrected within 24 h" is 94.6% right, while "first assignment stood" is
  96.4%. Corrections sometimes land in a second wrong group, and workload moves look like corrections. Corrected
  tickets are the hard cases: use them for error analysis and the review, not for extra weight.
- **The human benchmark measured against labels is an upper bound.** It shows 84.0%, against an actual 81.9%, because
  an unfixed misroute agrees with its own label. The review says so.
- "Contested" tickets (3+ groups) were 97.8% right but are excluded. Leave the rule alone until real data says
  otherwise.
- The review finds both hidden rules from history alone: Escalations follows orgs with 2+ open tickets, and Retention
  follows renewal within 60 days. This is the pitch: a step can look these facts up, a person reading the message
  can't.
- Fix before showing a prospect: General shows 100% right first time, an artifact of excluding moves into the
  catch-all and tickets answered on the spot. The page should explain it or leave the row out.
- The review's copy states facts about how it was made, not findings. Check it against the site-numbers rule
  (CLAUDE.md) before it is used outside.

## Step 5: the connector

### Mark (2026-10-01)

1. **Start a Zendesk Suite trial** (14 days, full API). Then create an OAuth client in Admin Center → Apps and
   integrations → APIs → OAuth Clients. Claude supplies the redirect URL (under `/api/`, like the other providers).
2. **Apply for the sponsored developer account** (the long-lived `d3v-` sandbox) in parallel. The trial expires; demos
   need an account that lasts.
3. Share the subdomain. Never paste the client secret into chat: it goes in `studio/.env` (gitignored), or wherever the
   OAuth provider credentials already live.

### Claude

- **Sign-in:** an OAuth authorization-code flow, read scope first. Write access, for the hidden prediction fields, only
  when shadow mode starts. Tokens are stored encrypted, the same way the studio already stores provider keys.
- **Backfill:**
  - the cursor-based incremental ticket export, plus each ticket's audits (or the incremental ticket-events export, if
    it carries enough);
  - users, organizations, groups, ticket fields and triggers;
  - retry, and respect 429s with Retry-After. The incremental exports have their own lower rate limit; check the
    current figure in Zendesk's docs rather than assuming it.
- **Output:** exactly the `export/` layout the synthetic data uses, so `zendesk_labels` and `zendesk_roast` run
  unchanged. Customer data goes under `tenants/<user_id>/`, the same partition as hosting.
- **PII:** mask requester names, emails and phone numbers in the text before anything is written. Keep ids.
- **Seeding the trial:** push a few hundred synthetic tickets. The Ticket Import API can backdate `created_at` and
  comments but not a history of group changes. So create the tickets, then have a script make the triage moves and
  corrections live, on a compressed timeline, so the audits contain real reassignments.
- **Done when:** a trial-account backfill produces labels and a review page with no hand edits, and replaying the
  audits matches the ticket export for every ticket.

### Open questions for step 5

- Where it appears in the studio: a "Connect Zendesk" data source next to uploads? Who can use it (tier, beta list)?
- Customer data: PLAN.md ground rule 3. Retention period, deletion on disconnect, and what the consent screen says.
- Real data has no sealed truth. The label tiers get checked by a random review sample, like the dashboard's.
- Rule-routed tickets are kept today (the "first assignment stood (rule)" tier, 96.9% right on synthetic data). The
  original plan excluded them, which would cut yield by about a third. Decide on real data.

## After step 5

1. Offline optimization on the labels: `split="time"` with the most recent weeks held out. Two objectives: maximize
   accuracy, and minimize tokens with accuracy at least the human first-assignment baseline. Add the per-class report
   to the review ("what the optimized prompt would have done").
2. Steps that look up the org's plan, open tickets and renewal date: the facts the hidden rules need.
3. Shadow mode for two weeks: webhook ingestion (idempotent), predictions to an `imp_pred_*` hidden field, every human
   routing decision becomes a label.
4. Confidence-gated live writes, with a permanent random shadow slice against automation bias.
5. Later: RDS or Modal only when volume forces it, not in phase 1.

## Paused, waiting on this direction

- Dashboard steps 4–7: the out-of-date badge / Update Score, alerts, the Jev quality line, Update Optimize.
- Weights and guided feedback (DASHBOARD.md), the probability output type with a Brier scorer, cross-validation
  ideas, the max-security tier.
