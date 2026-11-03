#!/usr/bin/env bash
# Run once as ubuntu from a published repository checkout on Lightsail.
set -euo pipefail

[[ $(id -un) == ubuntu ]] || { echo "Run this installer as ubuntu." >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
sudo -n docker volume inspect toss-trading-live_live_data >/dev/null
sudo -n install -D -m 755 "$here/lightsail-remote.sh" \
  /usr/local/lib/toss-trading/lightsail-remote.sh

sudo -n tee /etc/systemd/system/toss-image-update@.service >/dev/null <<'UNIT'
[Unit]
Description=Toss Trading image update (%i)
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
User=ubuntu
ExecStart=/usr/local/lib/toss-trading/lightsail-remote.sh %i registry
UNIT

sudo -n tee /etc/systemd/system/toss-image-update@.timer >/dev/null <<'UNIT'
[Unit]
Description=Check for Toss Trading %i image updates

[Timer]
OnBootSec=1min
OnUnitInactiveSec=2min
RandomizedDelaySec=30s

[Install]
WantedBy=timers.target
UNIT

sudo -n systemctl daemon-reload
sudo -n systemctl enable --now \
  toss-image-update@dashboard.timer toss-image-update@live.timer
systemctl list-timers 'toss-image-update@*'
