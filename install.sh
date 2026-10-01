#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
BASE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash install.sh'; exit 1; }
[[ -r /etc/os-release ]] || exit 1
source /etc/os-release
[[ "$ID" == ubuntu || "$ID" == debian ]] || { echo 'Ubuntu or Debian required'; exit 1; }
command -v systemctl >/dev/null || { echo 'systemd is required'; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'This installer requires amd64 (official MTProxy uses x86 instructions).'; exit 1; }
[[ ! -f /var/lib/vpn-manager/state.json ]] || { echo 'Already installed. Use vpnctl; keys have not been changed.'; exit 1; }
for file in manager.py bot.py refresh.py; do
  [[ -f "$BASE/$file" ]] || { echo "Missing project file: $file"; exit 1; }
done
read -rp 'Public server IPv4 or hostname: ' PUBLIC_HOST </dev/tty
read -rp 'VLESS port [8443]: ' VLESS_PORT </dev/tty
read -rp 'Telegram proxy port [9443]: ' PROXY_PORT </dev/tty
read -rp 'REALITY TLS hostname [www.microsoft.com]: ' REALITY_SNI </dev/tty
read -rsp 'Telegram bot token (hidden): ' BOT_TOKEN </dev/tty
echo
read -rp 'Your numeric Telegram user ID: ' ADMIN_ID </dev/tty
VLESS_PORT=${VLESS_PORT:-8443}
PROXY_PORT=${PROXY_PORT:-9443}
REALITY_SNI=${REALITY_SNI:-www.microsoft.com}
export PUBLIC_HOST VLESS_PORT PROXY_PORT REALITY_SNI BOT_TOKEN ADMIN_ID
python3 - <<'PY'
import os, re, socket, ssl, urllib.request, json
from pathlib import Path
for key in ('PUBLIC_HOST', 'REALITY_SNI'):
    value=os.environ[key]
    if len(value)>253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
        raise SystemExit('Invalid address: '+key)
ports=[int(os.environ[k]) for k in ('VLESS_PORT','PROXY_PORT')]
if ports[0]==ports[1] or any(not 1024<=p<=65535 for p in ports):
    raise SystemExit('Choose two different ports between 1024 and 65535')
for port in ports:
    with socket.socket() as s:
        try: s.bind(('0.0.0.0', port))
        except OSError: raise SystemExit(f'Port {port} is occupied; installation stopped')
if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', os.environ['BOT_TOKEN']):
    raise SystemExit('Invalid bot token format')
if not os.environ['ADMIN_ID'].isdigit() or int(os.environ['ADMIN_ID'])<=0:
    raise SystemExit('Telegram user ID must be a positive number')
try:
    req=urllib.request.Request('https://api.telegram.org/bot'+os.environ['BOT_TOKEN']+'/getMe')
    with urllib.request.urlopen(req, timeout=20) as r: result=json.load(r)
    if not result.get('ok'): raise ValueError()
except Exception:
    raise SystemExit('Bot authentication failed or Telegram API is unreachable')
host=os.environ['REALITY_SNI']
try:
    with socket.create_connection((host,443),timeout=15) as sock:
        with ssl.create_default_context().wrap_socket(sock,server_hostname=host) as tls:
            if tls.version()!='TLSv1.3': raise ValueError()
except Exception:
    raise SystemExit('REALITY target must be reachable with a valid TLS 1.3 certificate')
PY
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl unzip python3 sudo build-essential libssl-dev zlib1g-dev
TASK_TMP=$(mktemp -d)
trap 'rm -rf -- "$TASK_TMP"; unset BOT_TOKEN' EXIT
case $(uname -m) in
  x86_64) ASSET=Xray-linux-64.zip; HASH=23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae ;;
  *) echo 'Supported: amd64'; exit 1 ;;
esac
curl --fail --show-error --location --retry 3 --connect-timeout 15 --max-time 300 \
  "https://github.com/XTLS/Xray-core/releases/download/v26.3.27/$ASSET" -o "$TASK_TMP/xray.zip"
echo "$HASH  $TASK_TMP/xray.zip" | sha256sum -c -
unzip -q "$TASK_TMP/xray.zip" -d "$TASK_TMP/xray"
curl --fail --show-error --location --retry 3 --connect-timeout 15 --max-time 300 \
  https://codeload.github.com/TelegramMessenger/MTProxy/tar.gz/f36d8af769ffaeac36978d38c2c0f6d1104c2137 -o "$TASK_TMP/mtproxy.tar.gz"
echo "919795c416b870670841a21d1930ad97a24c7b84b9eb8c6f9e3de32f2fdf4655  $TASK_TMP/mtproxy.tar.gz" | sha256sum -c -
tar -xzf "$TASK_TMP/mtproxy.tar.gz" -C "$TASK_TMP"
make -C "$TASK_TMP/MTProxy-f36d8af769ffaeac36978d38c2c0f6d1104c2137" -j2
for account in vpn-xray vpn-proxy vpn-bot; do
  id "$account" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$account"
done
install -d -m 755 /usr/local/lib/vpn-manager /etc/vpn-manager
install -d -m 700 /var/lib/vpn-manager
install -d -m 700 -o vpn-bot -g vpn-bot /var/lib/vpn-bot
install -m 755 "$TASK_TMP/xray/xray" /usr/local/lib/vpn-manager/xray
install -m 644 "$TASK_TMP/xray/geoip.dat" "$TASK_TMP/xray/geosite.dat" /usr/local/lib/vpn-manager/
install -m 755 "$TASK_TMP/MTProxy-f36d8af769ffaeac36978d38c2c0f6d1104c2137/objs/bin/mtproto-proxy" /usr/local/lib/vpn-manager/mtproto-proxy
install -m 755 "$BASE/manager.py" /usr/local/sbin/vpnctl
install -m 644 "$BASE/bot.py" "$BASE/refresh.py" /usr/local/lib/vpn-manager/
python3 - <<'PY'
import json, os
from pathlib import Path
Path('/etc/vpn-manager/bot.json').write_text(json.dumps({
    'token':os.environ['BOT_TOKEN'], 'admin_ids':[int(os.environ['ADMIN_ID'])]}))
PY
chmod 600 /etc/vpn-manager/bot.json
chown vpn-bot:vpn-bot /etc/vpn-manager/bot.json
unset BOT_TOKEN
cat > /etc/systemd/system/vpn-xray.service <<'UNIT'
[Unit]
Description=VPN Manager Xray REALITY
After=network-online.target
Wants=network-online.target
[Service]
User=vpn-xray
Group=vpn-xray
Environment=XRAY_LOCATION_ASSET=/usr/local/lib/vpn-manager
ExecStart=/usr/local/lib/vpn-manager/xray run -config /etc/vpn-manager/xray.json
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/vpn-mtproxy.service <<'UNIT'
[Unit]
Description=VPN Manager Telegram MTProxy
After=network-online.target
Wants=network-online.target
[Service]
User=vpn-proxy
Group=vpn-proxy
WorkingDirectory=/etc/vpn-manager
EnvironmentFile=/etc/vpn-manager/mtproxy.args
ExecStart=/usr/local/lib/vpn-manager/mtproto-proxy --disable-tcp -H ${PORT} -S ${SECRET} --aes-pwd /etc/vpn-manager/proxy-secret /etc/vpn-manager/proxy-multi.conf -M 1
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/vpn-bot.service <<'UNIT'
[Unit]
Description=VPN Manager Telegram Admin Bot
After=network-online.target vpn-xray.service vpn-mtproxy.service
Wants=network-online.target
[Service]
User=vpn-bot
Group=vpn-bot
ExecStart=/usr/bin/python3 /usr/local/lib/vpn-manager/bot.py
Restart=on-failure
RestartSec=10
UMask=0077
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/vpn-bot /var/lib/vpn-manager /etc/vpn-manager
# sudo is allowed only for the fixed vpnctl commands in /etc/sudoers.d/vpn-manager.
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/vpn-expire.service <<'UNIT'
[Unit]
Description=Remove expired VPN users
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/vpnctl expire
UNIT
cat > /etc/systemd/system/vpn-expire.timer <<'UNIT'
[Unit]
Description=Check VPN user expiry each minute
[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=5
[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/vpn-refresh.service <<'UNIT'
[Unit]
Description=Refresh Telegram upstream proxy configuration
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/vpn-manager/refresh.py
UNIT
cat > /etc/systemd/system/vpn-refresh.timer <<'UNIT'
[Unit]
Description=Daily Telegram upstream refresh
[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=1800
[Install]
WantedBy=timers.target
UNIT
cat > "$TASK_TMP/sudoers" <<'SUDO'
vpn-bot ALL=(root) NOPASSWD: /usr/local/sbin/vpnctl status, /usr/local/sbin/vpnctl list, /usr/local/sbin/vpnctl proxy, /usr/local/sbin/vpnctl add *, /usr/local/sbin/vpnctl link *, /usr/local/sbin/vpnctl delete *, /usr/local/sbin/vpnctl restart xray, /usr/local/sbin/vpnctl restart proxy
SUDO
visudo -cf "$TASK_TMP/sudoers"
install -m 440 "$TASK_TMP/sudoers" /etc/sudoers.d/vpn-manager
systemctl daemon-reload
vpnctl init "$PUBLIC_HOST" "$VLESS_PORT" "$PROXY_PORT" "$REALITY_SNI"
python3 /usr/local/lib/vpn-manager/refresh.py --initial
systemctl enable --now vpn-xray vpn-mtproxy vpn-expire.timer vpn-refresh.timer
sleep 2
systemctl is-active --quiet vpn-xray vpn-mtproxy
systemctl enable --now vpn-bot
echo "Installed. Allow TCP $VLESS_PORT and $PROXY_PORT in your server/provider firewall."
echo 'Keep your SSH port allowed. Send /start to your bot.'
echo 'Create the first user: vpnctl add phone 30'
echo 'Telegram proxy link: vpnctl proxy'
