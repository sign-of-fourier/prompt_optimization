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
3. Instance: Ubuntu 24.04, t3.micro (0.5 GB nano risks the pip install), same region/VPC as the studio box, the role
   and security group above, private address 172.31.3.108, user data `deploy/serve-box-userdata.sh` with the commit
   filled in (step 4 is what that script does).
4. On the box: `git clone https://github.com/sign-of-fourier/prompt_optimization` at the commit being deployed,
   `python3 -m venv ~/venv && ~/venv/bin/pip install -e prompt_optimization/studio`, create `/srv/serve` (owned by
   ubuntu), write `/etc/serve-box.env` with `SERVE_TOKEN=...`, install `deploy/serve-box.service`.
5. On the studio box: `SERVE_BOX_URL=http://<private ip>:8200` and the same `SERVE_TOKEN` in `studio/.env`, enable
   the `/serve/` location in `deploy/nginx.conf`, restart the studio and nginx.

## Stopping and starting (the dev placeholder)

A stopped instance costs only its disk and keeps its private address and its disk, so hosted versions and key
hashes survive a stop; nginx and SERVE_BOX_URL stay valid. While stopped, hosted versions answer 502 and the
studio's ledger pull logs a failure every 30 s (harmless; the cursor makes the next pull catch up).

    aws ec2 stop-instances  --region us-east-2 --instance-ids i-08956f13a662671a2    # the t3.micro, replaced 2026-09-29 at 223b040
    aws ec2 start-instances --region us-east-2 --instance-ids i-08956f13a662671a2   # healthy about a minute later

## Replacing the box (how its code changes)

The box has no SSH and no SSM: its code changes only by replacement, and a new box starts empty. Its copies of hosted
versions are rebuilt from the studio's records, and the old ledger is pulled first so nothing is lost:

1. `cd studio && python -m app.hosting pull` on the studio box.
2. Terminate the instance; launch a new one with the same role, security group and bring-up script (pointed at the
   new commit), and `--private-ip-address` set to the old one so nginx and `SERVE_BOX_URL` stay as they are.
3. When `/health` answers: `python -m app.hosting resync` (re-pushes every hosted version and its owners' key hashes,
   and restarts the ledger cursor).

Requests in the minute between terminate and resync get a 502 from nginx.

## Acceptance

Publish the amount-due loop example, host it, and call `https://impromptune.com/serve/v/<vid>/run` with a key:
the answer matches the evaluated row byte for byte; latency holds while an optimization run is going on the studio
box; a revoked key is refused; an unhosted version is 404; the calls appear in the studio's traces and usage.

## Tear-down

Terminate the instance, delete the security group, the instance profile and the role. Remove SERVE_BOX_URL from
`studio/.env` and the `/serve/` location, and restart: serving falls back to in-process, nothing else changes.

## Result (2026-09-29, t3.small in us-east-2, commit e2cb97a)

Live, through `https://impromptune.com/serve/`: the amount-due loop example, baseline run, published, hosted.
- 4/5 served answers byte-identical to the evaluated rows, including two that looped three times. The fifth took a
  different path; the same row run three times through the same code on the studio box gave 204.92, 194.88 and
  204.92 by two paths, so that is Nova at temperature 0, not the box (offline tests show identical code paths).
- Median latency 1.31 s idle, 1.22 s while a 3-round optimization run was going on the studio box.
- 25 hosted calls -> 25 studio traces with cost; revoked key 401, another key 200, unhosted 404.
- Found on the way: loop versions could not be served at all (a first-visit placeholder counted as missing), in
  the studio's in-process serving too; fixed in e2cb97a. The box's code changes only by replacement (no SSH/SSM),
  hence `hosting resync`.
