#!/usr/bin/python3
"""Fetch Telegram upstream data with TLS verification; preserve files on failure."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path('/etc/vpn-manager')


def refresh(initial=False):
    with Path('/var/lib/vpn-manager/refresh.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = {}
        for name, endpoint in [('proxy-secret', 'getProxySecret'), ('proxy-multi.conf', 'getProxyConfig')]:
            with urllib.request.urlopen('https://core.telegram.org/' + endpoint, timeout=30) as response:
                payload = response.read(1024 * 1024 + 1)
            if not payload or len(payload) > 1024 * 1024:
                raise ValueError('Invalid Telegram data size')
            if name == 'proxy-secret' and len(payload) < 32:
                raise ValueError('Invalid Telegram secret')
            if name == 'proxy-multi.conf' and b'proxy_for' not in payload:
                raise ValueError('Invalid Telegram proxy config')
            data[name] = payload
        previous = {name: (ROOT / name).read_bytes() if (ROOT / name).exists() else None for name in data}
        if all(previous[n] == p for n, p in data.items()):
            return
        def write(name, payload):
            fd, tmp = tempfile.mkstemp(dir=ROOT)
            try:
                os.fchmod(fd, 0o644)
                with os.fdopen(fd, 'wb') as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, ROOT / name)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        try:
            for name, payload in data.items(): write(name, payload)
            if not initial:
                subprocess.run(['systemctl', 'restart', 'vpn-mtproxy'], check=True, timeout=30)
                subprocess.run(['systemctl', 'is-active', '--quiet', 'vpn-mtproxy'], check=True, timeout=10)
        except Exception:
            for name, payload in previous.items():
                if payload is not None: write(name, payload)
                else: (ROOT / name).unlink(missing_ok=True)
            if not initial:
                subprocess.run(['systemctl', 'restart', 'vpn-mtproxy'], timeout=30)
            raise


if __name__ == '__main__':
    try:
        refresh('--initial' in sys.argv)
    except Exception:
        sys.exit('Telegram upstream refresh failed; previous configuration was preserved.')
