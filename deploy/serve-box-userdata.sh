#!/bin/bash
# Serving box bring-up, run once by cloud-init at first boot (deploy/SERVE-BOX.md). Launch with COMMIT replaced:
#   sed "s/__COMMIT__/$(git rev-parse --short HEAD)/" deploy/serve-box-userdata.sh > /tmp/ud.sh
# No `set -x`: the serve token must not reach the console log.
set -eu
apt-get update -y
apt-get install -y python3-venv git
sudo -u ubuntu git clone https://github.com/sign-of-fourier/prompt_optimization /home/ubuntu/prompt_optimization
cd /home/ubuntu/prompt_optimization && sudo -u ubuntu git checkout -q __COMMIT__
sudo -u ubuntu python3 -m venv /home/ubuntu/venv
sudo -u ubuntu /home/ubuntu/venv/bin/pip install -q -e /home/ubuntu/prompt_optimization/studio
mkdir -p /srv/serve && chown ubuntu:ubuntu /srv/serve && chmod 700 /srv/serve
umask 077
/home/ubuntu/venv/bin/python - <<'PY'
import boto3
v = boto3.client("ssm", region_name="us-east-2").get_parameter(Name="/impromptune/serve-box/token", WithDecryption=True)["Parameter"]["Value"]
open("/etc/serve-box.env", "w").write(f"SERVE_TOKEN={v}\n")
PY
chmod 600 /etc/serve-box.env
cp /home/ubuntu/prompt_optimization/deploy/serve-box.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now serve-box
echo "SERVE-BOX BRING-UP DONE"
