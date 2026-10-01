#!/usr/bin/python3
"""Dependency-free Telegram long polling; private administrator chats only."""
import json
import logging
from pathlib import Path
import subprocess
import time
import urllib.request

HELP = '''مدیریت VPN
/status وضعیت سرویس‌ها
/add NAME DAYS ساخت کاربر (پیش‌فرض ۳۰ روز)
/list فهرست کاربران
/link NAME لینک اتصال
/delete NAME حذف کاربر
/proxy لینک پروکسی تلگرام
/restart xray یا /restart proxy
حذف و راه‌اندازی مجدد نیاز به تأیید /confirm دارد.
نام کاربر فقط حروف لاتین، عدد، _ و - است.'''
ALLOWED = {'status': (0, 0), 'add': (1, 2), 'list': (0, 0), 'link': (1, 1),
           'delete': (1, 1), 'proxy': (0, 0), 'restart': (1, 1)}


def authorized(message, admins):
    return (message.get('chat', {}).get('type') == 'private'
            and message.get('from', {}).get('id') in admins
            and message.get('chat', {}).get('id') == message.get('from', {}).get('id'))


def parse(text):
    parts = text.strip().split()
    if not parts or not parts[0].startswith('/'):
        raise ValueError('از /help استفاده کن.')
    cmd = parts[0][1:].split('@', 1)[0].lower()
    args = parts[1:]
    if cmd in ('help', 'start', 'confirm', 'cancel') and not args:
        return cmd, args
    if cmd not in ALLOWED or not ALLOWED[cmd][0] <= len(args) <= ALLOWED[cmd][1]:
        raise ValueError('دستور یا تعداد آرگومان‌ها نادرست است. /help')
    return cmd, args


class Bot:
    def __init__(self, settings):
        self.token = settings['token']
        self.admins = set(settings['admin_ids'])
        self.pending = {}
        self.last = {}
        self.offset_path = Path('/var/lib/vpn-bot/offset')

    def api(self, method, payload):
        request = urllib.request.Request(
            f'https://api.telegram.org/bot{self.token}/{method}',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.load(response)
        if not data.get('ok'):
            raise RuntimeError('Telegram API error')
        return data['result']

    def handle(self, message):
        if not authorized(message, self.admins):
            return
        uid = message['from']['id']
        now = time.monotonic()
        if now - self.last.get(uid, -100) < 1:
            return
        self.last[uid] = now
        try:
            cmd, args = parse(message.get('text', ''))
            if cmd in ('help', 'start'):
                reply = HELP
            elif cmd == 'cancel':
                self.pending.pop(uid, None)
                reply = 'لغو شد.'
            elif cmd in ('delete', 'restart'):
                self.pending[uid] = (now + 60, cmd, args)
                reply = f'تأیید {cmd} {" ".join(args)}؟ تا ۶۰ ثانیه /confirm بفرست؛ لغو: /cancel'
            else:
                if cmd == 'confirm':
                    pending = self.pending.pop(uid, None)
                    if not pending or pending[0] < now:
                        raise ValueError('تأیید منقضی شده یا درخواستی وجود ندارد.')
                    _, cmd, args = pending
                result = subprocess.run(['/usr/bin/sudo', '-n', '/usr/local/sbin/vpnctl', cmd, *args],
                    capture_output=True, text=True, timeout=90)
                reply = result.stdout if result.returncode == 0 else (result.stderr or 'Operation failed')
        except ValueError as e:
            reply = str(e)
        except (OSError, subprocess.SubprocessError):
            reply = 'عملیات ناموفق بود؛ وضعیت سرویس را روی سرور بررسی کن.'
        for start in range(0, len(reply), 3500):
            self.api('sendMessage', {'chat_id': uid, 'text': reply[start:start + 3500],
                                    'link_preview_options': {'is_disabled': True},
                                    'protect_content': True})

    def poll(self):
        self.api('deleteWebhook', {'drop_pending_updates': True})
        # Discard queued commands on start, so old destructive messages aren't replayed.
        updates = self.api('getUpdates', {'offset': -1, 'timeout': 0, 'allowed_updates': ['message']})
        offset = updates[-1]['update_id'] + 1 if updates else 0
        if self.offset_path.exists():
            offset = max(offset, int(self.offset_path.read_text()))
        delay = 2
        while True:
            try:
                updates = self.api('getUpdates', {'offset': offset, 'timeout': 30,
                                                 'allowed_updates': ['message']})
                for update in updates:
                    offset = update['update_id'] + 1
                    tmp = self.offset_path.with_suffix('.tmp')
                    tmp.write_text(str(offset))
                    tmp.replace(self.offset_path)
                    message = update.get('message', {})
                    if time.time() - message.get('date', 0) <= 120:
                        self.handle(message)
                delay = 2
            except Exception:
                # Never log exception URLs: Telegram URLs contain the bot token.
                logging.warning('Polling failed; retrying in %s seconds', delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    Bot(json.loads(Path('/etc/vpn-manager/bot.json').read_text())).poll()
