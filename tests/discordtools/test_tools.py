"""On-demand reads, durable acknowledgments, splitting, and owner bindings."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(os.environ.get('MUT_DIR', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))
from discord_tools.messages import format_message, split_message
from discord_tools.media import MediaTools
from discord_tools.state import read_state
from discord_tools.tools import MessageTools, PartialSendError

CHANNEL = '200000000000000001'
OTHER = '200000000000000002'
ITEM = '300000000000000001'
BOT = '100000000000000099'
OWNER = '100000000000000001'
NONCE = 'abc' * 16


def message(mid, content='hello', author=OWNER):
    return {'id': str(mid), 'content': content,
            'author': {'id': author, 'username': 'owner'}, 'mentions': []}


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'read.sqlite')
        self.history = [message(9000000000000000001)]
        self.calls = []
        self.sent = []
        self.denied = False
        self.tools = MessageTools(self.api, self.guard, lambda text: None,
                                  lambda: BOT, self.path, {'parse': ['users']})

    def guard(self, channel):
        if self.denied or channel != CHANNEL:
            raise SystemExit('Not authorized')

    def api(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == 'POST':
            self.sent.append(data)
            return {'id': str(100 + len(self.sent))}
        url = urlsplit(path)
        if url.path.endswith('/messages'):
            q = parse_qs(url.query)
            rows = self.history
            if 'after' in q:
                rows = sorted((m for m in rows if int(m['id']) > int(q['after'][0])),
                              key=lambda m: int(m['id']))
            else:
                rows = sorted(rows, key=lambda m: -int(m['id']))
            return rows[:int(q['limit'][0])]
        return message(path.rsplit('/', 1)[-1])

    def test_initialize_then_read_twice_and_explicitly_mark(self):
        first = self.tools.unread(CHANNEL)
        self.assertTrue(first['initialized'])
        self.history += [message(9000000000000000002), message(9000000000000000003)]
        result = self.tools.unread(CHANNEL, 1)
        self.assertEqual([m['id'] for m in result['messages']], ['9000000000000000002'])
        self.assertTrue(result['truncated'])
        self.assertEqual(self.tools.unread(CHANNEL, 1), result)
        self.tools.mark_read(CHANNEL, '9000000000000000002')
        self.assertEqual([m['id'] for m in self.tools.unread(CHANNEL)['messages']],
                         ['9000000000000000003'])
        self.tools.mark_read(CHANNEL, '9000000000000000001')
        with read_state(self.path) as state:
            self.assertEqual(state.get(CHANNEL), '9000000000000000002')
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)

    def test_empty_channel_and_own_message_filter(self):
        self.history = []
        self.tools.unread(CHANNEL)
        self.history = [message(100, author=BOT), message(101)]
        self.assertEqual([m['id'] for m in self.tools.unread(CHANNEL)['messages']], ['101'])
        self.tools.mark_read(CHANNEL)
        self.assertEqual(self.tools.unread(CHANNEL)['messages'], [])

    def test_pagination_reads_oldest_and_does_not_skip_self_message_page(self):
        self.history = []
        self.tools.unread(CHANNEL)
        self.history = [message(i, author=BOT) for i in range(1, 101)]
        self.history += [message(i) for i in range(101, 204)]
        result = self.tools.unread(CHANNEL, 2)
        self.assertEqual([m['id'] for m in result['messages']], ['101', '102'])
        self.assertTrue(result['truncated'])

    def test_refusal_and_validation_do_not_create_state_or_call_api(self):
        self.denied = True
        for operation in (lambda: self.tools.unread(CHANNEL),
                          lambda: self.tools.mark_read(CHANNEL),
                          lambda: self.tools.messages(CHANNEL),
                          lambda: self.tools.send(CHANNEL, 'hello')):
            with self.assertRaises(SystemExit):
                operation()
        self.assertEqual(self.calls, [])
        self.assertFalse(Path(self.path).exists())
        self.denied = False
        for limit in (0, -1, 101):
            with self.assertRaises(ValueError):
                self.tools.unread(CHANNEL, limit)
        self.assertEqual(self.calls, [])

    def test_failed_lookup_does_not_acknowledge(self):
        self.tools.unread(CHANNEL)
        self.tools.api = lambda *a: (_ for _ in ()).throw(SystemExit('404'))
        with self.assertRaises(SystemExit):
            self.tools.mark_read(CHANNEL, '9000000000000000099')
        with read_state(self.path) as state:
            self.assertEqual(state.get(CHANNEL), '9000000000000000001')

    def test_reply_reference_first_part_only_and_no_author_ping(self):
        self.tools.send(CHANNEL, 'hi ' * 1500, ITEM)
        self.assertGreater(len(self.sent), 1)
        for i, payload in enumerate(self.sent):
            self.assertLessEqual(len(payload['content'].encode('utf-16-le')) // 2, 2000)
            self.assertFalse(payload['allowed_mentions']['replied_user'])
            self.assertEqual('message_reference' in payload, i == 0)
        self.assertEqual(self.sent[0]['message_reference']['message_id'], ITEM)

    def test_secret_and_size_refused_before_any_send(self):
        def scrub(text):
            if 'SECRET' in text:
                raise SystemExit('secret')
        self.tools.scrub = scrub
        with self.assertRaises(SystemExit):
            self.tools.send(CHANNEL, 'a' * 3000 + 'SECRET')
        with self.assertRaises(ValueError):
            self.tools.send(CHANNEL, 'x' * 21000)
        self.assertEqual(self.calls, [])

    def test_partial_send_reports_ids_without_retry(self):
        original = self.tools.api
        def fail(method, path, data=None):
            if method == 'POST' and self.sent:
                raise SystemExit('rate limited')
            return original(method, path, data)
        self.tools.api = fail
        with self.assertRaises(PartialSendError) as caught:
            self.tools.send(CHANNEL, 'x' * 4000)
        self.assertEqual(caught.exception.message_ids, ['101'])
        self.assertEqual(len(self.sent), 1)

    def test_sqlite_concurrent_stale_ack_cannot_move_back(self):
        with read_state(self.path) as a, read_state(self.path) as b:
            a.advance(CHANNEL, '9000000000000000001')
            stale = b.get(CHANNEL)
            a.advance(CHANNEL, '9000000000000000005')
            self.assertEqual(b.advance(CHANNEL, stale), '9000000000000000005')


class Formatting(unittest.TestCase):
    def test_mentions_reply_and_attachment_metadata(self):
        m = message(1, '<@123> <:yay:456> <t:0:f>')
        m['mentions'] = [{'id': '123', 'username': 'Alice'}]
        m['message_reference'] = {'message_id': '2'}
        m['referenced_message'] = None
        m['attachments'] = [{'id': '3', 'filename': 'x.png', 'url': 'private-url'}]
        result = format_message(m)
        self.assertEqual(result['content_readable'], '@Alice :yay: 1970-01-01T00:00:00+00:00')
        self.assertEqual(result['reply_to'], {'deleted': True})
        self.assertNotIn('url', result['attachments'][0])

    def test_utf16_fences_and_exact_limit(self):
        self.assertEqual(split_message('a' * 2000), ['a' * 2000])
        plain = '😀' * 2500
        self.assertEqual(''.join(split_message(plain)), plain)
        source = '```python\n' + 'print(1)\n' * 600 + '```'
        chunks = split_message(source)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode('utf-16-le')) // 2, 2000)
            self.assertEqual(sum(line.startswith('```') for line in chunk.splitlines()), 2)


class Binding(unittest.TestCase):
    def test_cli_requires_live_owner_nonce_for_every_new_command(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = Path(temp) / 'config.yaml'
            cfg.write_text(f'version: 1\nowners: ["{OWNER}"]\ntoken_from: env:MUSE_CLI_TEST_TOKEN\n')
            old_config = os.environ.get('MUSE_DISCORD_CONFIG')
            old_token = os.environ.get('MUSE_CLI_TEST_TOKEN')
            os.environ['MUSE_DISCORD_CONFIG'] = str(cfg)
            os.environ['MUSE_CLI_TEST_TOKEN'] = 'test-token'
            try:
                spec = importlib.util.spec_from_file_location('discord_cli_test', ROOT / 'bin/discord-api.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.PACKAGE_DIR = temp
                module.OWNER_ITEMS_FILE = str(Path(temp) / 'owner_items.json')
                module.OWNER_DM_NONCES_FILE = str(Path(temp) / 'owner_dm_nonces.json')
                calls = []
                module.api = lambda *args: calls.append(args)
                for command, args in (
                    ('get-messages', [CHANNEL]), ('list-unread', [CHANNEL]),
                    ('mark-read', [CHANNEL]), ('send-message', [CHANNEL, 'hi']),
                    ('reply-message', [CHANNEL, ITEM, 'hi']),
                    ('fetch-attachment', [CHANNEL, ITEM, '400000000000000001']),
                    ('get-emoji', [CHANNEL, '😀']), ('get-avatar', [CHANNEL, OWNER])):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        rc = module.main(['discord-api.py', command, *args])
                    self.assertEqual(rc, 1)
                    self.assertIn('error', json.loads(output.getvalue()))
                self.assertEqual(calls, [])
                self.assertFalse((Path(temp) / 'read-state.sqlite').exists())
                Path(module.OWNER_ITEMS_FILE).write_text(json.dumps({
                    ITEM: {'nonce': NONCE, 'channel_id': CHANNEL,
                           'channels': [], 'post_channels': []}}))
                Path(module.OWNER_DM_NONCES_FILE).write_text('{}')

                def fake_api(method, path, data=None):
                    calls.append((method, path, data))
                    if path == '/users/@me':
                        return {'id': BOT}
                    if path.startswith('/channels/') and '/messages' not in path:
                        return {'id': path.rsplit('/', 1)[-1], 'type': 0}
                    if '/messages?' in path:
                        return [message(400000000000000001)]
                    result = message(400000000000000001)
                    result['attachments'] = [{'id': '400000000000000001',
                                               'url': 'https://cdn.discordapp.com/attachments/x',
                                               'filename': 'x.png'}]
                    return result
                module.api = fake_api
                module.media_tools = lambda: MediaTools(
                    module.api, module.guard_target, temp,
                    lambda *args: {'path': str(Path(temp) / 'media/x.png')})
                prefix = ['discord-api.py', '--for', ITEM, '--nonce', NONCE]
                for command, args in (
                    ('get-messages', [CHANNEL]), ('list-unread', [CHANNEL]),
                    ('mark-read', [CHANNEL]), ('send-message', [CHANNEL, 'hi']),
                    ('reply-message', [CHANNEL, ITEM, 'hi']),
                    ('fetch-attachment', [CHANNEL, ITEM, '400000000000000001']),
                    ('get-emoji', [CHANNEL, '😀']), ('get-avatar', [CHANNEL, OWNER])):
                    with self.subTest(command=command):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            rc = module.main([*prefix, command, *args])
                        self.assertEqual(rc, 0, output.getvalue())
                        self.assertNotIn('error', json.loads(output.getvalue()))
                        calls.clear()
                        with contextlib.redirect_stdout(io.StringIO()):
                            rc = module.main([*prefix, command, OTHER, *args[1:]])
                        self.assertEqual(rc, 1)
                        self.assertFalse(any('/messages' in call[1] for call in calls))
            finally:
                for key, old in [('MUSE_DISCORD_CONFIG', old_config), ('MUSE_CLI_TEST_TOKEN', old_token)]:
                    if old is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old


if __name__ == '__main__':
    unittest.main()
