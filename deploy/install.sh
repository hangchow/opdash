#!/bin/bash
# Run as root after reviewing the host-specific env and firewall files.
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ $EUID != 0 ]]; then
    echo 'Run this installer as root' >&2
    exit 1
fi
getent passwd opdash >/dev/null || useradd --system --home-dir /var/lib/opdash --shell /usr/sbin/nologin opdash
getent passwd opdash-deploy >/dev/null || useradd --system --home-dir /var/lib/opdash-deploy --shell /usr/sbin/nologin opdash-deploy
install -d -o opdash -g opdash -m 0700 /var/lib/opdash
install -d -o opdash-deploy -g opdash-deploy -m 0755 /opt/opdash /opt/opdash/releases
install -d -o opdash-deploy -g opdash-deploy -m 0700 /var/lib/opdash-deploy
install -d -m 0755 /etc/opdash /usr/local/libexec/opdash
if [[ ! -f /etc/opdash/opdash.env ]]; then
    install -m 0644 opdash.env.example /etc/opdash/opdash.env
fi
install -m 0755 start.py deploy.py /usr/local/libexec/opdash/
install -m 0644 opdash-firewall.nft /etc/opdash/firewall.nft
install -m 0644 opdash-web.service opdash-deploy.service opdash-deploy.timer opdash-firewall.service /etc/systemd/system/
cat > /etc/sudoers.d/opdash-deploy <<'SUDOERS'
opdash-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl start opdash-web.service, /usr/bin/systemctl stop opdash-web.service, /usr/bin/systemctl restart opdash-web.service
SUDOERS
chmod 0440 /etc/sudoers.d/opdash-deploy
visudo -cf /etc/sudoers.d/opdash-deploy
nft -c -f /etc/opdash/firewall.nft
systemd-analyze verify /etc/systemd/system/opdash-web.service /etc/systemd/system/opdash-deploy.service /etc/systemd/system/opdash-deploy.timer /etc/systemd/system/opdash-firewall.service
systemctl daemon-reload
systemctl enable --now opdash-firewall.service
systemctl enable opdash-web.service
# UFW is already active on osaka. This adds only the dashboard's LAN access.
ufw allow in on enp9s0f0np0 from 192.168.10.0/24 to 192.168.10.1 port 18080 proto tcp comment 'opdash LAN'
echo 'Installed. Run initial deployment, then enable opdash-deploy.timer after verification.'
