import os
import stat
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.player import player_environment


class PlayerEnvironmentTests(unittest.TestCase):
    def linux_environment(self, inherited, directories):
        account = Mock(return_value=SimpleNamespace(pw_dir="/home/deck"))

        def filesystem_stat(path):
            value = directories.get(path)
            if value is None:
                raise FileNotFoundError(path)
            return value

        with patch.dict(os.environ, inherited, clear=True), \
                patch("backend.player.platform.system", return_value="Linux"), \
                patch("backend.player.os.geteuid", return_value=1000, create=True), \
                patch("backend.player.os.stat", side_effect=filesystem_stat), \
                patch.dict(sys.modules, {"pwd": SimpleNamespace(getpwuid=account)}):
            result = player_environment()
            self.assertEqual(dict(os.environ), inherited, "Must not change Decky's environment")
        account.assert_called_once_with(1000)
        return result

    @staticmethod
    def directory(owner=1000):
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=owner)

    def test_system_service_launch_uses_the_desktop_users_audio_session(self):
        inherited = {"HOME": "/root", "PATH": "/usr/bin", "USER": "deck"}
        result = self.linux_environment(inherited, {"/run/user/1000": self.directory()})
        self.assertEqual(result["HOME"], "/home/deck")
        self.assertEqual(result["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(result["PATH"], inherited["PATH"])
        self.assertNotIn("PULSE_SERVER", result)
        self.assertNotIn("PIPEWIRE_REMOTE", result)

    def test_existing_valid_session_and_explicit_audio_routing_are_preserved(self):
        inherited = {"HOME": "/root", "XDG_RUNTIME_DIR": "/custom/session",
                     "PULSE_SERVER": "unix:/custom/pulse/native", "PIPEWIRE_REMOTE": "custom-0",
                     "DBUS_SESSION_BUS_ADDRESS": "unix:path=/custom/session/bus"}
        result = self.linux_environment(inherited, {"/custom/session": self.directory(),
                                                    "/run/user/1000": self.directory()})
        self.assertEqual(result, {**inherited, "HOME": "/home/deck"})

    def test_invalid_inherited_runtime_cannot_select_another_users_session(self):
        cases = {
            "/run/user/0": self.directory(owner=0),
            "/stale/session": None,
            "/regular/file": SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000),
            "relative/session": self.directory(),
        }
        for path, metadata in cases.items():
            with self.subTest(runtime=path):
                result = self.linux_environment({"XDG_RUNTIME_DIR": path},
                                               {path: metadata, "/run/user/1000": self.directory()})
                self.assertEqual(result["XDG_RUNTIME_DIR"], "/run/user/1000")

    def test_missing_desktop_session_does_not_invent_a_runtime_directory(self):
        inherited = {"XDG_RUNTIME_DIR": "/run/user/0", "PULSE_SERVER": "tcp:127.0.0.1:4713"}
        result = self.linux_environment(inherited, {"/run/user/0": self.directory(owner=0)})
        self.assertNotIn("XDG_RUNTIME_DIR", result)
        self.assertEqual(result["PULSE_SERVER"], inherited["PULSE_SERVER"])

    def test_non_linux_preview_does_not_require_pwd(self):
        inherited = {"HOME": "C:\\Users\\developer", "XDG_RUNTIME_DIR": "custom"}
        with patch.dict(os.environ, inherited, clear=True), \
                patch("backend.player.platform.system", return_value="Windows"), \
                patch.dict(sys.modules, {"pwd": None}):
            result = player_environment()
            self.assertEqual(result, inherited)
            self.assertIsNot(result, os.environ)


if __name__ == "__main__":
    unittest.main()
