"""CDN credential isolation, authorization, and bounded local media files."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('MUT_DIR', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))
from discord_tools.media import MediaTools, NoRedirect, download, validate_url


class Response(io.BytesIO):
    def __init__(self, data, headers):
        super().__init__(data)
        self.headers = headers


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return self.response


class Media(unittest.TestCase):
    def test_download_has_no_credentials_and_uses_private_atomic_file(self):
        opener = Opener(Response(b'PNG', {'Content-Type': 'image/png'}))
        with tempfile.TemporaryDirectory() as directory:
            result = download('https://cdn.discordapp.com/attachments/1/2/x.png',
                              '../../x.png', directory, opener)
            path = Path(result['path'])
            self.assertTrue(path.is_relative_to(Path(directory) / 'media/on-demand'))
            self.assertEqual(path.read_bytes(), b'PNG')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result['content_type'], 'image/png')
            self.assertEqual(list(path.parent.glob('.download-*')), [])
        headers = {k.lower() for k, _ in opener.requests[0].header_items()}
        self.assertNotIn('authorization', headers)
        self.assertNotIn('cookie', headers)

    def test_untrusted_hosts_and_redirects_are_refused(self):
        for url in ('http://cdn.discordapp.com/attachments/a',
                    'https://localhost/attachments/a',
                    'https://cdn.discordapp.com.evil.test/attachments/a',
                    'https://user:secret@cdn.discordapp.com/attachments/a',
                    'https://cdn.discordapp.com:999/attachments/a',
                    'https://cdn.jsdelivr.net/other-project/file',
                    'https://cdn.discordapp.com/api/v10/users/@me'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test')

    def test_size_limits_cleanup_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            url = 'https://cdn.discordapp.com/attachments/1/2/x.png'
            with patch('discord_tools.media.MAX_BYTES', 2):
                for headers in ({'Content-Length': '3'}, {}):
                    with self.assertRaises(ValueError):
                        download(url, 'x.png', directory, Opener(Response(b'123', headers)))
            target = Path(directory) / 'media/on-demand'
            self.assertEqual(list(target.iterdir()), [])
            with tempfile.TemporaryDirectory() as linked:
                (Path(linked) / 'media').symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    download(url, 'x.png', linked, Opener(Response(b'a', {})))

    def test_only_message_attachment_is_downloaded_after_guard(self):
        calls = []
        def api(method, path):
            calls.append(path)
            return {'attachments': [{'id': '3', 'url': 'https://cdn.discordapp.com/attachments/x',
                                      'filename': 'x.png'}]}
        files = []
        tools = MediaTools(api, lambda c: calls.append('guard:' + c), '/tmp',
                           lambda *args: files.append(args) or {'ok': True})
        self.assertEqual(tools.attachment('1', '2', '3'), {'ok': True})
        self.assertEqual(calls, ['guard:1', '/channels/1/messages/2'])
        with self.assertRaises(ValueError):
            tools.attachment('1', '2', '99')
        self.assertEqual(len(files), 1)
        def refuse(c):
            raise SystemExit('refused')
        tools.guard = refuse
        calls.clear()
        for method, args in ((tools.attachment, ('1', '2', '3')),
                             (tools.avatar, ('1', '2')), (tools.emoji, ('1', '😀'))):
            with self.assertRaises(SystemExit):
                method(*args)
        self.assertEqual(calls, [])

    def test_custom_unicode_emoji_and_default_and_animated_avatar(self):
        urls = []
        user = {'discriminator': '0'}
        tools = MediaTools(lambda *a: user, lambda c: None, '/tmp',
                           lambda url, *a: urls.append(url))
        tools.emoji('1', '<a:party:100000000000000001>')
        self.assertTrue(urls[-1].endswith('100000000000000001.gif'))
        tools.emoji('1', '❤️')
        self.assertTrue(urls[-1].endswith('/2764.png'))
        tools.emoji('1', '👩\u200d💻')
        self.assertTrue(urls[-1].endswith('/1f469-200d-1f4bb.png'))
        tools.avatar('1', '100000000000000001')
        self.assertIn('/embed/avatars/', urls[-1])
        user['avatar'] = 'a_' + 'a' * 32
        tools.avatar('1', '100000000000000001')
        self.assertTrue(urls[-1].endswith('.gif?size=256'))
        user['avatar'] = '../../secrets'
        with self.assertRaises(ValueError):
            tools.avatar('1', '100000000000000001')


if __name__ == '__main__':
    unittest.main()
