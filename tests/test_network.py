import ssl
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.network import SYSTEM_CA_FILES, https_opener, tls_context
from backend.player import Player
from backend.spotify import NoRedirect, SpotifyError, http
from backend.storage import Store


class VerifiedHttpsTests(unittest.TestCase):
    def setUp(self):
        tls_context.cache_clear()

    def tearDown(self):
        tls_context.cache_clear()

    def test_linux_missing_compiled_trust_paths_loads_system_ca_bundle(self):
        context = MagicMock()
        with patch("backend.network.ssl.create_default_context", return_value=context), \
             patch("backend.network.ssl.get_default_verify_paths", return_value=types.SimpleNamespace(cafile=None, capath=None)), \
             patch("backend.network.sys.platform", "linux"), \
             patch("backend.network.Path.is_file", lambda path: path.as_posix() == SYSTEM_CA_FILES[1]):
            self.assertIs(tls_context(), context)
            self.assertIs(tls_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile=SYSTEM_CA_FILES[1])

    def test_existing_configured_trust_paths_are_preserved(self):
        context = MagicMock()
        with patch("backend.network.ssl.create_default_context", return_value=context), \
             patch("backend.network.ssl.get_default_verify_paths", return_value=types.SimpleNamespace(cafile="/configured/trust.pem", capath=None)), \
             patch("backend.network.sys.platform", "linux"):
            tls_context()
        context.load_verify_locations.assert_not_called()

    def test_non_linux_keeps_its_platform_trust_store(self):
        context = MagicMock()
        with patch("backend.network.ssl.create_default_context", return_value=context), \
             patch("backend.network.ssl.get_default_verify_paths", return_value=types.SimpleNamespace(cafile=None, capath=None)), \
             patch("backend.network.sys.platform", "win32"):
            tls_context()
        context.load_verify_locations.assert_not_called()

    def test_missing_system_bundle_never_disables_certificate_or_hostname_checks(self):
        with patch("backend.network.ssl.get_default_verify_paths", return_value=types.SimpleNamespace(cafile=None, capath=None)), \
             patch("backend.network.sys.platform", "linux"), patch("backend.network.Path.is_file", return_value=False):
            context = tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_https_handler_uses_verified_context_and_keeps_redirect_policy(self):
        context = ssl.create_default_context()
        with patch("backend.network.tls_context", return_value=context), patch("backend.network.urllib.request.build_opener") as build:
            https_opener(NoRedirect)
        handlers = build.call_args.args
        self.assertIs(handlers[0], NoRedirect)
        self.assertIs(handlers[1]._context, context)

    def test_web_api_certificate_failure_is_actionable_and_does_not_echo_details(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("private-error-detail"))
        with patch("backend.spotify.https_opener", return_value=opener) as factory:
            with self.assertRaises(SpotifyError) as raised:
                http("GET", "https://api.spotify.com/v1/me")
        factory.assert_called_once_with(NoRedirect)
        self.assertEqual(raised.exception.code, "tls")
        self.assertIn("system certificates", str(raised.exception))
        self.assertNotIn("private-error-detail", str(raised.exception))

    def test_player_uses_same_verified_transport_and_redacts_certificate_details(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("private-error-detail"))
        with tempfile.TemporaryDirectory() as temporary:
            player = Player(Store(Path(temporary) / "settings"), Path(temporary) / "runtime")
            with patch("backend.player.platform.machine", return_value="x86_64"), \
                 patch("backend.player.https_opener", return_value=opener) as factory:
                with self.assertRaises(SpotifyError) as raised:
                    player._install()
            self.assertFalse(player.binary.exists())
        factory.assert_called_once_with(NoRedirect)
        self.assertIn("system certificates", str(raised.exception))
        self.assertNotIn("private-error-detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
