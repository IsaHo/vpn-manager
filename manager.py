#!/usr/bin/python3
"""Root-only, fixed-command manager. No user-supplied shell commands."""
import fcntl
import grp
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

ROOT = Path('/var/lib/vpn-manager')
STATE = ROOT / 'state.json'
CONFIG = Path('/etc/vpn-manager/xray.json')
CONFIG_GROUP = 'vpn-xray'
XRAY = '/usr/local/lib/vpn-manager/xray'
SERVICES = {'xray': 'vpn-xray', 'proxy': 'vpn-mtproxy'}


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=30, **kwargs).stdout.strip()


def atomic(path, data, mode=0o600, group=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if group:
            os.fchown(fd, 0, grp.getgrnam(group).gr_gid)
        with os.fdopen(fd, 'w') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_host(value):
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        if len(value) > 253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
            raise ValueError('Use a public IPv4 address or hostname')
    return value


def config(state):
    now = int(time.time())
    clients = [{'id': u['id'], 'email': u['name'], 'flow': 'xtls-rprx-vision'}
               for u in state['users'] if not u['expires'] or u['expires'] > now]
    return {
        'log': {'loglevel': 'warning'},
        'inbounds': [{'tag': 'vless', 'listen': '0.0.0.0', 'port': state['vless_port'],
            'protocol': 'vless', 'settings': {'clients': clients, 'decryption': 'none'},
            'streamSettings': {'network': 'tcp', 'security': 'reality',
                'realitySettings': {'show': False, 'target': state['sni'] + ':443',
                    'serverNames': [state['sni']], 'privateKey': state['private_key'],
                    'shortIds': [state['short_id']]}}}],
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom'},
                      {'tag': 'blocked', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [
            {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'}]}}


def apply(state, restart=True):
    payload = json.dumps(config(state), indent=2)
    candidate = CONFIG.with_suffix('.candidate.json')
    atomic(candidate, payload, 0o640, CONFIG_GROUP)
    try:
        run([XRAY, 'run', '-test', '-config', str(candidate)],
            env={**os.environ, 'XRAY_LOCATION_ASSET': '/usr/local/lib/vpn-manager'})
    finally:
        candidate.unlink(missing_ok=True)
    old_config = CONFIG.read_text() if CONFIG.exists() else None
    old_state = STATE.read_text() if STATE.exists() else None
    atomic(CONFIG, payload, 0o640, CONFIG_GROUP)
    atomic(STATE, json.dumps(state, indent=2))
    try:
        if restart:
            run(['systemctl', 'restart', SERVICES['xray']])
            run(['systemctl', 'is-active', SERVICES['xray']])
    except Exception:
        if old_config is not None:
            atomic(CONFIG, old_config, 0o640, CONFIG_GROUP)
        if old_state is not None:
            atomic(STATE, old_state)
        if restart and old_config is not None:
            run(['systemctl', 'restart', SERVICES['xray']])
        raise


def link(state, user):
    query = urllib.parse.urlencode({'encryption': 'none', 'security': 'reality',
        'sni': state['sni'], 'fp': 'chrome', 'pbk': state['public_key'],
        'sid': state['short_id'], 'type': 'tcp', 'flow': 'xtls-rprx-vision'})
    return f"vless://{user['id']}@{state['host']}:{state['vless_port']}?{query}#{urllib.parse.quote(user['name'])}"


def initialize(host, vless_port, proxy_port, sni):
    if STATE.exists():
        raise ValueError('Already installed; existing keys were preserved')
    validate_host(host)
    validate_host(sni)
    vp, pp = int(vless_port), int(proxy_port)
    if not (1024 <= vp <= 65535 and 1024 <= pp <= 65535 and vp != pp):
        raise ValueError('Choose different ports between 1024 and 65535')
    keys = run([XRAY, 'x25519'])
    private_match = re.search(r'PrivateKey:\s*(\S+)', keys)
    public_match = re.search(r'(?:Password \(PublicKey\)|PublicKey|Password):\s*(\S+)', keys)
    if not private_match or not public_match:
        raise ValueError('Unexpected Xray key output')
    private, public = private_match.group(1), public_match.group(1)
    state = {'host': host, 'vless_port': vp, 'proxy_port': pp, 'sni': sni,
        'private_key': private, 'public_key': public, 'short_id': secrets.token_hex(8),
        'proxy_secret': secrets.token_hex(16), 'users': []}
    apply(state, restart=False)
    atomic(Path('/etc/vpn-manager/mtproxy.args'),
           f"PORT={pp}\nSECRET={state['proxy_secret']}\n", 0o640)
    return 'Initialized'


def dispatch(args):
    if not args:
        raise ValueError('A command is required')
    cmd, *rest = args
    if cmd == 'init' and len(rest) == 4:
        return initialize(*rest)
    state = json.loads(STATE.read_text())
    if cmd == 'status' and not rest:
        lines = []
        for name, service in SERVICES.items():
            result = subprocess.run(['systemctl', 'is-active', service], capture_output=True,
                                    text=True, timeout=10)
            lines.append(f'{name}: {result.stdout.strip()}')
        lines.append(f"users: {len(state['users'])}")
        return '\n'.join(lines)
    if cmd == 'list' and not rest:
        return '\n'.join(f"{u['name']} expires={u['expires'] or 'never'}" for u in state['users']) or 'No users'
    if cmd == 'add' and 1 <= len(rest) <= 2:
        name = rest[0]
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', name):
            raise ValueError('Name: 1–32 letters, digits, _ or -')
        days = int(rest[1]) if len(rest) == 2 else 30
        if not 1 <= days <= 3650:
            raise ValueError('Days must be between 1 and 3650')
        if any(u['name'] == name for u in state['users']):
            raise ValueError('Name already exists')
        if len(state['users']) >= 500:
            raise ValueError('User limit reached (500)')
        user = {'name': name, 'id': str(uuid.uuid4()), 'expires': int(time.time()) + days * 86400}
        state['users'].append(user)
        apply(state)
        return link(state, user)
    if cmd in ('delete', 'link') and len(rest) == 1:
        user = next((u for u in state['users'] if u['name'] == rest[0]), None)
        if user is None:
            raise ValueError('User not found')
        if cmd == 'link':
            if user['expires'] and user['expires'] <= time.time():
                raise ValueError('User expired')
            return link(state, user)
        state['users'].remove(user)
        apply(state)
        return 'Deleted'
    if cmd == 'proxy' and not rest:
        return 'https://t.me/proxy?' + urllib.parse.urlencode({
            'server': state['host'], 'port': state['proxy_port'], 'secret': 'dd' + state['proxy_secret']})
    if cmd == 'restart' and len(rest) == 1 and rest[0] in SERVICES:
        run(['systemctl', 'restart', SERVICES[rest[0]]])
        return run(['systemctl', 'is-active', SERVICES[rest[0]]])
    if cmd == 'expire' and not rest:
        active = [u for u in state['users'] if not u['expires'] or u['expires'] > time.time()]
        if len(active) != len(state['users']):
            state['users'] = active
            apply(state)
        return 'Expiry checked'
    raise ValueError('Unknown command or invalid arguments')


def main():
    if os.geteuid() != 0:
        sys.exit('Run through sudo')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / 'manager.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            print(dispatch(sys.argv[1:]))
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            if isinstance(e, subprocess.SubprocessError):
                print('Service operation failed; check sudo journalctl -u vpn-xray', file=sys.stderr)
            else:
                print(str(e), file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
