import asyncio
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest.mock import AsyncMock, patch

from backend.spotify import REDIRECT, SCOPES, Spotify, SpotifyError
from backend.storage import Store


class PhoneAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.calls = []
        self.response = {"access_token": "test-access", "refresh_token": "test-refresh", "scope": SCOPES}

        def transport(method, url, headers, payload):
            self.calls.append((method, url, payload))
            return 200, {}, self.response

        self.spotify = Spotify(self.store, transport)

    async def asyncTearDown(self):
        await self.spotify.close()
        self.temp.cleanup()

    async def phone(self):
        context = object()
        server = type("Server", (), {"close": lambda self: None, "wait_closed": AsyncMock()})()
        with patch("backend.spotify.lan_address", return_value="192.168.1.10"), \
                patch("backend.spotify.phone_tls", return_value=(context, "AA:BB")), \
                patch("backend.spotify.asyncio.start_server", new=AsyncMock(return_value=server)) as start:
            result = await self.spotify.begin_auth("a" * 32, "phone")
        self.assertEqual(start.call_args.args[1:3], ("192.168.1.10", 43892))
        self.assertIs(start.call_args.kwargs["ssl"], context)
        self.assertEqual(start.call_args.kwargs["ssl_handshake_timeout"], 5)
        return result

    async def test_phone_url_is_device_landing_page_and_callback_is_exact_https(self):
        result = await self.phone()
        self.assertTrue(result["url"].startswith("https://192.168.1.10:43892/phone/"))
        self.assertEqual(result["redirect"], "https://192.168.1.10:43892/callback")
        self.assertNotIn("code_verifier", result["url"])
        self.assertGreater(result["expiresAt"], time.time() * 1000)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.spotify.pending["authorization"]).query)
        self.assertEqual(params["redirect_uri"], [result["redirect"]])
        self.assertEqual(params["code_challenge_method"], ["S256"])

    async def test_phone_callback_cannot_be_replaced_by_loopback_or_another_host(self):
        result = await self.phone()
        query = "?state=" + self.spotify.pending["state"] + "&code=test-code"
        for wrong in (REDIRECT, "https://192.168.1.11:43892/callback", "http://192.168.1.10:43892/callback"):
            with self.assertRaises(SpotifyError):
                await self.spotify.finish_auth(wrong + query)
        self.assertEqual(self.calls, [])
        await self.spotify.finish_auth(result["redirect"] + query)
        payload = urllib.parse.parse_qs(self.calls[0][2].decode())
        self.assertEqual(payload["redirect_uri"], [result["redirect"]])
        self.assertFalse(self.spotify.needs_reauthorization)
        with self.assertRaises(SpotifyError):
            await self.spotify.finish_auth(result["redirect"] + query)
        self.assertEqual(len(self.calls), 1)

    async def test_phone_start_failure_leaves_no_pending_session(self):
        with patch("backend.spotify.lan_address", side_effect=OSError()):
            with self.assertRaises(SpotifyError) as failure:
                await self.spotify.begin_auth("a" * 32, "phone")
        self.assertEqual(failure.exception.code, "setup")
        self.assertIsNone(self.spotify.pending)
        self.assertIsNone(self.spotify.server)

    async def test_cancel_closes_phone_listener_without_changing_existing_account(self):
        self.store.update(refresh_token="old-token", granted_scopes=SCOPES)
        await self.phone()
        await self.spotify.cancel_auth()
        self.assertIsNone(self.spotify.server)
        self.assertIsNone(self.spotify.pending)
        self.assertTrue(self.spotify.connected)
        self.assertEqual(self.calls, [])

    async def test_new_features_require_new_scopes_without_disconnecting_old_account(self):
        self.store.update(refresh_token="old-token", granted_scopes="user-read-playback-state")
        self.assertTrue(self.spotify.needs_reauthorization)
        self.assertTrue(self.spotify.connected)
        result = await self.spotify.begin_auth("a" * 32)
        state = self.spotify.pending["state"]
        await self.spotify.finish_auth(result["redirect"] + "?state=" + state + "&code=test")
        self.assertFalse(self.spotify.needs_reauthorization)

    async def test_duplicate_callback_fields_do_not_consume_authorization(self):
        result = await self.spotify.begin_auth("a" * 32)
        state = self.spotify.pending["state"]
        with self.assertRaises(SpotifyError):
            await self.spotify.finish_auth(result["redirect"] + "?state=" + state + "&code=a&code=b")
        self.assertIsNotNone(self.spotify.pending)
        self.assertFalse(self.calls)

    async def test_successful_reauthorization_rejects_old_account_read_in_flight(self):
        self.store.update(client_id="a" * 32, access_token="old-access", refresh_token="old-refresh",
                          expires_at=time.time() + 3600)
        started, release = threading.Event(), threading.Event()
        def transport(method, url, headers, payload):
            if method == "GET":
                started.set()
                release.wait(3)
                return 200, {}, {"display_name": "Previous account"}
            return 200, {}, self.response
        self.spotify.transport = transport
        await self.spotify.begin_auth("a" * 32)
        old_epoch = self.spotify.epoch
        state = self.spotify.pending["state"]
        task = asyncio.create_task(self.spotify.request("GET", "/me"))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            await self.spotify.finish_auth(REDIRECT + "?state=" + state + "&code=test")
            self.assertGreater(self.spotify.epoch, old_epoch)
            release.set()
            with self.assertRaises(SpotifyError) as failure:
                await task
            self.assertEqual(failure.exception.code, "auth")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_wrong_host_header_cannot_complete_loopback_callback(self):
        await self.spotify.begin_auth("a" * 32)
        state = self.spotify.pending["state"]
        reader, writer = await asyncio.open_connection("127.0.0.1", 43891)
        try:
            writer.write((f"GET /callback?state={state}&code=test HTTP/1.1\r\nHost: attacker.invalid\r\n\r\n").encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            self.assertTrue(response.startswith(b"HTTP/1.1 400"))
            self.assertIn(b"Referrer-Policy: no-referrer", response)
            self.assertIsNotNone(self.spotify.pending)
            self.assertEqual(self.calls, [])
        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
