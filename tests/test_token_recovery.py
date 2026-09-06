import asyncio
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.spotify import REDIRECT, TOKEN_URL, Spotify, SpotifyError
from backend.storage import Store


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.entered = None
        self.release = None
        self.loop = None

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if self.release:
            self.loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(2):
                raise RuntimeError("Test did not release transport")
            self.release = None
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TokenRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.update(client_id="a" * 32, access_token="expired-access",
                          refresh_token="retained-refresh", expires_at=0)
        self.original = dict(self.store.data)
        self.transport = Transport([])
        self.spotify = Spotify(self.store, self.transport)
        self.now = 1000
        self.clock = patch("backend.spotify.time", SimpleNamespace(
            monotonic=lambda: self.now, time=time.time))
        self.clock.start()

    async def asyncTearDown(self):
        if self.transport.release:
            self.transport.release.set()
        await self.spotify.close()
        self.clock.stop()
        self.temp.cleanup()

    def success(self, access="new-access"):
        return (200, {}, {"access_token": access, "expires_in": 3600})

    async def fail_refresh(self):
        self.transport.responses.append(SpotifyError("Cannot reach Spotify.", "offline"))
        with self.assertRaises(SpotifyError) as raised:
            await self.spotify.token()
        self.assertEqual(raised.exception.code, "offline")

    async def test_concurrent_failed_reads_share_one_refresh_attempt(self):
        self.transport.responses.append(SpotifyError("Cannot reach Spotify.", "offline"))
        outcomes = await asyncio.gather(
            *[self.spotify.request("GET", "/me" if i % 2 else "/me/player") for i in range(8)],
            return_exceptions=True)
        self.assertTrue(all(isinstance(error, SpotifyError) and error.code == "offline" for error in outcomes))
        self.assertEqual(self.transport.calls, [("POST", TOKEN_URL)])
        self.assertEqual(self.store.data, self.original)

    async def test_failed_refresh_retries_after_brief_cooldown_and_recovers(self):
        await self.fail_refresh()
        self.transport.responses.append(self.success())
        self.now += 1.9
        with self.assertRaises(SpotifyError):
            await self.spotify.token()
        self.assertEqual(len(self.transport.calls), 1)
        self.now += 0.2
        self.assertEqual(await self.spotify.token(), "new-access")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.store.data["refresh_token"], "retained-refresh")
        self.assertIsNone(self.spotify.refresh_failure)

    async def test_server_failure_is_coalesced_without_signing_out(self):
        self.transport.responses.append((503, {}, {}))
        outcomes = await asyncio.gather(*[self.spotify.token() for _ in range(4)], return_exceptions=True)
        self.assertTrue(all(isinstance(error, SpotifyError) and error.code == "spotify_error" for error in outcomes))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.data, self.original)
        self.assertTrue(self.spotify.connected)

    async def test_refresh_rate_limit_is_checked_again_after_acquiring_lock(self):
        self.transport.responses.extend([(429, {"Retry-After": "90"}, {}), self.success()])
        outcomes = await asyncio.gather(
            *[self.spotify.request("GET", "/me/player") for _ in range(6)], return_exceptions=True)
        self.assertTrue(all(isinstance(error, SpotifyError) and error.code == "rate_limit" for error in outcomes))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.data, self.original)
        self.now += 3  # The transient cooldown must not shorten Retry-After.
        with self.assertRaises(SpotifyError) as raised:
            await self.spotify.token()
        self.assertEqual(raised.exception.code, "rate_limit")
        self.assertGreaterEqual(raised.exception.retry_after, 87)
        self.assertEqual(len(self.transport.calls), 1)
        self.now += 88
        self.assertEqual(await self.spotify.token(), "new-access")
        self.assertEqual(len(self.transport.calls), 2)

    async def test_epoch_change_discards_old_refresh_failure(self):
        await self.fail_refresh()
        self.spotify.epoch += 1
        self.transport.responses.append(self.success())
        self.assertEqual(await self.spotify.token(), "new-access")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertIsNone(self.spotify.refresh_failure)

    async def test_successful_authorization_clears_failure_and_rate_limit(self):
        await self.fail_refresh()
        self.spotify.backoff_until = self.now + 90
        self.spotify.pending = {"state": "state", "verifier": "verifier", "client_id": "b" * 32,
                                "expires": self.now + 300, "epoch": self.spotify.epoch}
        self.transport.responses.append((200, {}, {"access_token": "signed-in", "refresh_token": "new-account"}))
        await self.spotify.finish_auth(REDIRECT + "?state=state&code=code")
        self.assertIsNone(self.spotify.refresh_failure)
        self.assertEqual(self.spotify.backoff_until, 0)
        self.assertEqual(await self.spotify.token(), "signed-in")
        self.assertEqual(self.store.data["refresh_token"], "new-account")

    async def test_signout_clears_failure_and_never_reuses_credentials(self):
        await self.fail_refresh()
        await self.spotify.disconnect()
        self.assertIsNone(self.spotify.refresh_failure)
        self.assertFalse(self.spotify.connected)
        with self.assertRaises(SpotifyError) as raised:
            await self.spotify.token()
        self.assertEqual(raised.exception.code, "auth")
        self.assertEqual(len(self.transport.calls), 1)
        self.store.update(client_id="b" * 32, refresh_token="new-account", expires_at=0)
        self.transport.responses.append(self.success())
        self.assertEqual(await self.spotify.token(), "new-access")
        self.assertEqual(len(self.transport.calls), 2)

    async def test_failed_inflight_refresh_cannot_repopulate_cooldown_after_signout(self):
        self.transport.responses.append(SpotifyError("Cannot reach Spotify.", "offline"))
        self.transport.loop = asyncio.get_running_loop()
        self.transport.entered = asyncio.Event()
        self.transport.release = threading.Event()
        release = self.transport.release
        refresh = asyncio.create_task(self.spotify.token())
        try:
            await asyncio.wait_for(self.transport.entered.wait(), 1)
            signout = asyncio.create_task(self.spotify.disconnect())
            await asyncio.sleep(0)
            release.set()
            outcome = (await asyncio.gather(refresh, return_exceptions=True))[0]
            await signout
            self.assertIsInstance(outcome, SpotifyError)
            self.assertEqual(outcome.code, "auth")
            self.assertIsNone(self.spotify.refresh_failure)
            self.assertFalse(self.spotify.connected)
        finally:
            release.set()
            await asyncio.gather(refresh, return_exceptions=True)

    async def test_invalid_grant_after_recovery_window_immediately_expires_session(self):
        await self.fail_refresh()
        self.now += 3
        self.transport.responses.append((400, {}, {"error": "invalid_grant", "private": "do-not-expose"}))
        with self.assertRaises(SpotifyError) as raised:
            await self.spotify.token()
        self.assertEqual(raised.exception.code, "auth")
        self.assertNotIn("do-not-expose", str(raised.exception))
        self.assertFalse(self.spotify.connected)
        self.assertIsNone(self.spotify.refresh_failure)
        with self.assertRaises(SpotifyError):
            await self.spotify.token()
        self.assertEqual(len(self.transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
