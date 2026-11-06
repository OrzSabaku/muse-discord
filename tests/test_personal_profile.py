import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import muse_settings as MS
spec = importlib.util.spec_from_file_location('personal', ROOT / 'bin/personal-profile.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
OWNER = '123456789012345678'


class ProfileTests(unittest.TestCase):
    def test_effective_permissions_and_existing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            original = f'version: 1\nowners: ["{OWNER}"]\ntoken_from: env:PRIVATE_TOKEN\n'
            (d / 'config.yaml').write_text(original)
            target = p.apply(d)
            st = MS.load(str(target))
            dm = st.resolve(MS.Scope.dm())
            server = st.resolve(MS.Scope.server('223456789012345678'))
            self.assertEqual(st.owners, [OWNER])
            self.assertEqual(st.token_from, 'env:PRIVATE_TOKEN')
            self.assertEqual(dm['who.dms_from'], 'owners')
            self.assertEqual(dm['who.allow'], [OWNER])
            self.assertEqual(server['who.replies'], 'off')
            self.assertEqual(server['privacy.memory'], 'nothing')
            self.assertEqual(dm['who.bots'], 'ignore')
            self.assertEqual(dm['abilities.external_apps'], 'off')
            self.assertEqual(next(d.glob('config.yaml.bak-*')).read_text(), original)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_overlay_and_ambiguous_owner_do_not_mutate_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            target = d / 'config.yaml'
            target.write_text('version: 1\nowners: []\n')
            with self.assertRaises(ValueError):
                p.apply(d)
            (d / 'config.discord.yaml').write_text('version: 1\n')
            with self.assertRaises(ValueError):
                p.apply(d, OWNER)
            self.assertEqual(target.read_text(), 'version: 1\nowners: []\n')

    def test_invalid_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                p.apply(tmp, 'not-an-id')
            self.assertFalse((Path(tmp) / 'config.yaml').exists())


if __name__ == '__main__':
    unittest.main()
