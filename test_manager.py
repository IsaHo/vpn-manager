import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import manager
import bot


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(manager, 'STATE', self.root / 'state.json'),
                        patch.object(manager, 'CONFIG', self.root / 'xray.json'),
                        patch.object(manager, 'CONFIG_GROUP', None)]
        for p in self.patches: p.start()
        self.state = {'host': '203.0.113.7', 'vless_port': 8443, 'proxy_port': 9443,
            'sni': 'www.microsoft.com', 'private_key': 'test', 'public_key': 'public',
            'short_id': '1234567890abcdef', 'proxy_secret': 'a' * 32, 'users': []}
        manager.STATE.write_text(json.dumps(self.state))

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def test_lifecycle(self):
        with patch.object(manager, 'run', return_value='active'):
            uri = manager.dispatch(['add', 'phone', '30'])
            self.assertTrue(uri.startswith('vless://'))
            self.assertIn('security=reality', uri)
            self.assertEqual(manager.dispatch(['link', 'phone']), uri)
            self.assertIn('phone', manager.dispatch(['list']))
            manager.dispatch(['delete', 'phone'])
            self.assertEqual(manager.dispatch(['list']), 'No users')

    def test_no_shell_or_unknown_commands(self):
        with patch.object(manager, 'run') as call:
            for args in [['add', 'name;id'], ['restart', 'sshd'], ['add', 'x', '-1'],
                         ['delete', 'missing'], ['status', 'extra'], ['shell', 'id']]:
                with self.assertRaises(ValueError): manager.dispatch(args)
            call.assert_not_called()

    def test_duplicate_name(self):
        with patch.object(manager, 'run', return_value='active'):
            manager.dispatch(['add', 'alice'])
            with self.assertRaises(ValueError): manager.dispatch(['add', 'alice'])

    def test_expired_not_rendered(self):
        self.state['users'] = [{'id': 'x', 'name': 'expired', 'expires': int(time.time()) - 1}]
        self.assertEqual(manager.config(self.state)['inbounds'][0]['settings']['clients'], [])

    def test_expiry_removes_credentials(self):
        self.state['users'] = [{'id': 'x', 'name': 'expired', 'expires': 1}]
        manager.STATE.write_text(json.dumps(self.state))
        with patch.object(manager, 'run', return_value='active'):
            manager.dispatch(['expire'])
        self.assertEqual(json.loads(manager.STATE.read_text())['users'], [])

    def test_failed_validation_preserves_state(self):
        previous = manager.STATE.read_text()
        with patch.object(manager, 'run', side_effect=subprocess.CalledProcessError(1, 'xray')):
            with self.assertRaises(subprocess.CalledProcessError): manager.dispatch(['add', 'alice'])
        self.assertEqual(manager.STATE.read_text(), previous)

    def test_failed_restart_rolls_back(self):
        manager.CONFIG.write_text('previous config')
        old = manager.STATE.read_text()
        responses = ['', subprocess.CalledProcessError(1, 'systemctl'), '']
        with patch.object(manager, 'run', side_effect=responses):
            with self.assertRaises(subprocess.CalledProcessError): manager.dispatch(['add', 'alice'])
        self.assertEqual(manager.STATE.read_text(), old)
        self.assertEqual(manager.CONFIG.read_text(), 'previous config')

    def test_proxy_padding(self):
        self.assertIn('secret=dd' + 'a' * 32, manager.dispatch(['proxy']))


class BotTests(unittest.TestCase):
    def test_authorization(self):
        msg = {'chat': {'type': 'private', 'id': 12}, 'from': {'id': 12}}
        self.assertTrue(bot.authorized(msg, {12}))
        self.assertFalse(bot.authorized(msg, {13}))
        msg['chat']['type'] = 'supergroup'
        self.assertFalse(bot.authorized(msg, {12}))

    def test_parser(self):
        self.assertEqual(bot.parse('/add phone 30'), ('add', ['phone', '30']))
        for command in ['/shell id', '/status extra', 'restart', '/delete']:
            with self.assertRaises(ValueError): bot.parse(command)

    def test_delete_requires_confirmation(self):
        b = bot.Bot({'token': 'placeholder', 'admin_ids': [12]})
        msg = {'chat': {'type': 'private', 'id': 12}, 'from': {'id': 12}, 'text': '/delete phone'}
        with patch.object(b, 'api'), patch.object(bot.subprocess, 'run') as run:
            with patch.object(bot.time, 'monotonic', return_value=10): b.handle(msg)
            run.assert_not_called()
            msg['text'] = '/confirm'
            run.return_value = subprocess.CompletedProcess([], 0, 'Deleted', '')
            with patch.object(bot.time, 'monotonic', return_value=12): b.handle(msg)
            self.assertEqual(run.call_args.args[0][-2:], ['delete', 'phone'])

    def test_unauthorized_never_executes(self):
        b = bot.Bot({'token': 'placeholder', 'admin_ids': [12]})
        with patch.object(b, 'api') as api, patch.object(bot.subprocess, 'run') as run:
            b.handle({'chat': {'type': 'private', 'id': 99}, 'from': {'id': 99}, 'text': '/proxy'})
            api.assert_not_called()
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
