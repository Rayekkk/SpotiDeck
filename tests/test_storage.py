import json
import os
import tempfile
import unittest
from pathlib import Path
from backend.storage import Store


class PrivateStorageTests(unittest.TestCase):
    def test_nonfinite_values_cannot_replace_a_valid_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.update(refresh_token="fixture")
            before = store.path.read_bytes()
            for invalid in (float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    store.update(audio_balance=invalid)
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(store.data, {"refresh_token": "fixture"})

    def test_malformed_account_is_preserved_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.json'
            path.write_text('{"audio_balance": NaN}', encoding='utf-8')
            with self.assertRaises(ValueError):
                Store(directory)
            self.assertIn('NaN', path.read_text())

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and permissions')
    def test_existing_credentials_and_directory_become_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'settings'
            root.mkdir(mode=0o755)
            path = root / 'account.json'
            path.write_text(json.dumps({'refresh_token': 'fixture'}))
            path.chmod(0o644)
            store = Store(root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.data['refresh_token'], 'fixture')

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink test')
    def test_linked_settings_directory_is_rejected_before_read_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.mkdir()
            link = Path(directory) / 'settings'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                Store(link)
            self.assertEqual(list(target.iterdir()), [])
