import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.deploy_migration import DeploymentMigration


class MigrationTests(unittest.TestCase):
    def make_layout(self, root, name="Spotify"):
        homebrew = root / "homebrew"
        stage = root / "staging" / "deploy-test"
        stage.mkdir(parents=True)
        for category in ("plugins", "settings", "data", "logs"):
            parent = homebrew / category
            parent.mkdir(parents=True)
            if name:
                folder = parent / name
                folder.mkdir()
                (folder / "retained.txt").write_text(category + " original", encoding="utf-8")
        incoming = stage / "unpacked" / "SpotiDeck"
        incoming.mkdir(parents=True)
        (incoming / "main.py").write_text("new plugin", encoding="utf-8")
        migration = DeploymentMigration(homebrew, stage)
        return migration, incoming

    def assert_original(self, migration, name="Spotify"):
        for category in ("plugins", "settings", "data", "logs"):
            retained = migration.homebrew / category / name / "retained.txt"
            self.assertEqual(retained.read_text(encoding="utf-8"), category + " original")

    def test_migrates_private_directories_but_keeps_log_history(self):
        with tempfile.TemporaryDirectory() as temp:
            migration, incoming = self.make_layout(Path(temp).resolve())
            migration.prepare()
            self.assert_original(migration)
            migration.install(incoming)
            self.assertEqual((migration.target / "main.py").read_text(), "new plugin")
            for category in ("settings", "data"):
                parent = migration.homebrew / category
                self.assertFalse((parent / "Spotify").exists())
                self.assertEqual((parent / "SpotiDeck" / "retained.txt").read_text(), category + " original")
            self.assertTrue((migration.homebrew / "logs" / "Spotify" / "retained.txt").exists())
            self.assertFalse((migration.homebrew / "logs" / "SpotiDeck").exists())
            self.assertFalse(migration.legacy.exists())
            self.assertEqual((migration.previous / "retained.txt").read_text(), "plugins original")

    def test_each_install_move_failure_restores_original_layout(self):
        for failed_move in range(1, 5):
            with self.subTest(failed_move=failed_move), tempfile.TemporaryDirectory() as temp:
                migration, incoming = self.make_layout(Path(temp).resolve())
                migration.prepare()
                move = migration._move
                calls = 0

                def fail_one(source, destination):
                    nonlocal calls
                    calls += 1
                    if calls == failed_move:
                        raise OSError("injected rename failure")
                    move(source, destination)

                with patch.object(migration, "_move", side_effect=fail_one):
                    with self.assertRaisesRegex(OSError, "injected"):
                        migration.install(incoming)
                migration.rollback()
                self.assert_original(migration)
                for category in ("plugins", "settings", "data"):
                    self.assertFalse((migration.homebrew / category / "SpotiDeck").exists())
                self.assertFalse(migration.changed)

    def test_failed_worker_verification_rolls_back_complete_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            migration, incoming = self.make_layout(Path(temp).resolve())
            migration.prepare()
            migration.install(incoming)
            migration.rollback()  # Equivalent to verification/start failing after install.
            self.assert_original(migration)
            self.assertFalse(migration.target.exists())
            self.assertEqual((migration.failed / "main.py").read_text(), "new plugin")
            self.assertFalse(migration.changed)

    def test_regular_update_restores_spotideck_without_moving_state(self):
        with tempfile.TemporaryDirectory() as temp:
            migration, incoming = self.make_layout(Path(temp).resolve(), "SpotiDeck")
            migration.prepare()
            self.assertEqual(migration.state_moves, [])
            migration.install(incoming)
            migration.rollback()
            self.assert_original(migration, "SpotiDeck")
            self.assertFalse(migration.legacy.exists())

    def test_mixed_identity_is_rejected_before_any_move(self):
        with tempfile.TemporaryDirectory() as temp:
            migration, _ = self.make_layout(Path(temp).resolve())
            (migration.homebrew / "settings" / "SpotiDeck").mkdir()
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                migration.prepare()
            self.assert_original(migration)
            self.assertFalse(migration.changed)
            self.assertFalse(migration.previous.exists())

    def test_directory_created_after_preflight_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            migration, incoming = self.make_layout(Path(temp).resolve())
            migration.prepare()
            migration.target.mkdir()
            (migration.target / "unrelated.txt").write_text("keep")
            with self.assertRaises(RuntimeError):
                migration.install(incoming)
            self.assert_original(migration)
            self.assertEqual((migration.target / "unrelated.txt").read_text(), "keep")

    def test_symlinked_target_is_rejected_without_touching_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            migration, _ = self.make_layout(root)
            outside = root / "unrelated"
            outside.mkdir()
            marker = outside / "keep.txt"
            marker.write_text("keep")
            try:
                migration.target.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"This environment cannot create symlinks: {error}")
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                migration.prepare()
            self.assert_original(migration)
            self.assertEqual(marker.read_text(), "keep")

    def test_rollback_collision_keeps_original_backup_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            migration, incoming = self.make_layout(Path(temp).resolve())
            migration.prepare()
            migration.install(incoming)
            migration.legacy.mkdir()
            (migration.legacy / "unrelated.txt").write_text("keep")
            with self.assertRaisesRegex(RuntimeError, "rollback failed"):
                migration.rollback()
            self.assertTrue(migration.saved_plugin)
            self.assertEqual((migration.previous / "retained.txt").read_text(), "plugins original")
            self.assertEqual((migration.legacy / "unrelated.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
