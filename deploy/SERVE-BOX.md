# The serving box

Hosted versions (the beta upsell) run on their own EC2 instance, so a customer's request never waits behind an
optimization run on the studio box. Code: `studio/app/serve_box.py` (the box), `studio/app/hosting.py` (the studio's
half). Design and invariants are in those docstrings; this file is the runbook.

## Shape

    internet -> studio box nginx -> /serve/v/{vid}/run -> serving box :8200 (private address only)
    studio box -> serving box /internal/*  (SERVE_TOKEN): push versions and key hashes, pull the ledger

- Customer data on the box: `/srv/serve/tenants/<user_id>/` only. `keys.json` beside it holds key hashes.
- Model access: an instance role allowed `bedrock:InvokeModel` and nothing else. No provider key on the box.
- The studio keeps the one ledger: it pulls each customer's `ledger.jsonl` into `traces` and `usage_log` every 30 s,
  which is what AWS pass-through billing will read.

## Bring-up

1. Security group `serve-box`: inbound 8200 from the studio box's security group only; 22 from nowhere (use SSM or
   a temporary rule to set up). Outbound: everything (Bedrock, pip).
2. IAM role `serve-box` with an inline policy allowing `bedrock:InvokeModel` on the house models; instance profile.
3. Instance: Ubuntu, t3.small, same region/VPC as the studio box, the role and security group above.
4. On the box: `git clone https://github.com/sign-of-fourier/prompt_optimization` at the commit being deployed,
   `python3 -m venv ~/venv && ~/venv/bin/pip install -e prompt_optimization/studio`, create `/srv/serve` (owned by
   ubuntu), write `/etc/serve-box.env` with `SERVE_TOKEN=...`, install `deploy/serve-box.service`.
5. On the studio box: `SERVE_BOX_URL=http://<private ip>:8200` and the same `SERVE_TOKEN` in `studio/.env`, enable
   the `/serve/` location in `deploy/nginx.conf`, restart the studio and nginx.

## Acceptance

Publish the amount-due loop example, host it, and call `https://impromptune.com/serve/v/<vid>/run` with a key:
the answer matches the evaluated row byte for byte; latency holds while an optimization run is going on the studio
box; a revoked key is refused; an unhosted version is 404; the calls appear in the studio's traces and usage.

## Tear-down

Terminate the instance, delete the security group, the instance profile and the role. Remove SERVE_BOX_URL from
`studio/.env` and the `/serve/` location, and restart: serving falls back to in-process, nothing else changes.
