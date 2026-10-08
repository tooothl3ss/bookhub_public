"""First-run setup must not disclose credentials or replace existing settings."""
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


class ConfigureTests(unittest.TestCase):
    def test_random_credentials_private_file_and_no_overwrite(self):
        source = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            for name in ('scripts/configure.py', '.env.example'):
                shutil.copy2(source / name, root / name)
            command = [sys.executable, str(root / 'scripts/configure.py')]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, first.returncode, first.stderr)
            config = root / '.env'
            contents = config.read_text()
            settings = dict(line.split('=', 1) for line in contents.splitlines()
                            if line and not line.startswith('#') and '=' in line)
            password, token = settings['SYNC_PASSWORD'], settings['SYNC_API_KEY']
            self.assertGreaterEqual(len(password), 32)
            self.assertGreaterEqual(len(token), 40)
            self.assertNotEqual(password, token)
            self.assertNotIn(password, first.stdout + first.stderr)
            self.assertNotIn(token, first.stdout + first.stderr)
            self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(0, second.returncode)
            self.assertEqual(contents, config.read_text())


if __name__ == '__main__':
    unittest.main()
