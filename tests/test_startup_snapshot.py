"""Panel readiness and owned background work during a cold or offline start."""

import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.service import Service, playback
from backend.spotify import SpotifyError
from backend.storage import Store


def remote_playback(title="Current track", playing=True):
    return {"is_playing": playing, "progress_ms": 10000,
            "item": {"id": "a" * 22, "uri": "spotify:track:" + "a" * 22,
                     "type": "track", "name": title, "duration_ms": 180000},
            "device": {"id": "handheld", "name": "SpotiDeck", "volume_percent": 100}}


class StartupSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.update(client_id="a" * 32, refresh_token="private-refresh",
                          soloist_key="private-player-key")
        self.now = 1000.0
        self.clock = patch("backend.service.time", SimpleNamespace(monotonic=lambda: self.now, time=time.time))
        self.clock.start()
        self.spotify = SimpleNamespace(store=self.store, connected=True, pending=None, auth_error=None,
                                       epoch=0, backoff_until=0, request=AsyncMock(side_effect=self.request))
        self.player = SimpleNamespace(status=lambda: {"installed": True, "running": True,
                                                     "hasKey": True, "supported": True, "error": None})
        self.mixer = SimpleNamespace(status=lambda: {"supported": True, "otherVolume": 100,
                                                     "otherStreams": 2, "error": None},
                                     set_volume=AsyncMock(), close=AsyncMock())
        self.service = Service(self.spotify, self.player, self.mixer)

    async def asyncTearDown(self):
        await self.service.close()
        self.clock.stop()
        self.temp.cleanup()

    async def request(self, method, path, params=None, body=None):
        if path == "/me":
            return {"display_name": "Listener"}
        if path == "/me/player":
            return remote_playback()
        if method == "PUT" and path == "/me/player/volume":
            return None
        raise AssertionError(f"Unexpected request: {method} {path}")

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 1)

    def calls(self, method, path):
        return sum(call.args[:2] == (method, path) for call in self.spotify.request.await_args_list)

    async def test_stalled_remote_reads_leave_local_status_and_settings_available(self):
        gate = asyncio.Event()

        async def stalled(*args):
            await gate.wait()
            return await self.request(*args)

        self.spotify.request.side_effect = stalled
        result = await asyncio.wait_for(self.service.quick_snapshot(), 0.1)
        self.assertTrue(result["connected"])
        self.assertTrue(result["player"]["running"])
        self.assertEqual(result["audio"]["otherStreams"], 2)
        self.assertTrue(result["playbackPending"])
        self.assertTrue(result["refreshing"])
        self.assertIsNone(result["playback"])
        self.assertNotIn("private-refresh", str(result))
        self.assertNotIn("private-player-key", str(result))
        await self.until(lambda: self.spotify.request.await_count == 2)
        for _ in range(20):
            await self.service.quick_snapshot()
        self.assertEqual(self.spotify.request.await_count, 2)

    async def test_ready_playback_does_not_wait_for_profile_or_repeat_its_read(self):
        profile_gate = asyncio.Event()

        async def delayed_profile(method, path, *args):
            if path == "/me":
                await profile_gate.wait()
            return await self.request(method, path, *args)

        self.spotify.request.side_effect = delayed_profile
        await self.service.quick_snapshot()
        await self.until(lambda: "playback" in self.service.cache and not any(k[2] == "playback" for k in self.service._quick_reads))
        result = await self.service.quick_snapshot()
        self.assertEqual(result["playback"]["track"]["title"], "Current track")
        self.assertIsNone(result["profile"])
        self.assertFalse(result["playbackPending"])
        self.assertFalse(result["refreshing"])
        for _ in range(20):
            self.now += 0.1
            await self.service.quick_snapshot()
        self.assertEqual(self.calls("GET", "/me/player"), 1)
        self.assertEqual(self.calls("GET", "/me"), 1)
        profile_gate.set()
        await self.until(lambda: "profile" in self.service.cache)
        self.assertEqual((await self.service.quick_snapshot())["profile"]["name"], "Listener")

    async def test_confirmed_no_active_device_finishes_startup(self):
        async def no_device(method, path, *args):
            return None if path == "/me/player" else await self.request(method, path, *args)

        self.spotify.request.side_effect = no_device
        self.player.status = lambda: {"installed": True, "running": False, "hasKey": True,
                                      "supported": True, "error": None}
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        result = await self.service.quick_snapshot()
        self.assertIsNone(result["playback"])
        self.assertFalse(result["playbackPending"])
        self.assertFalse(result["refreshing"])
        self.assertEqual(result["retryAfter"], 0)
        self.assertEqual(self.calls("GET", "/me/player"), 1)

    async def test_late_connect_state_is_discovered_with_bounded_negative_cache(self):
        registered = False

        async def connecting(method, path, *args):
            return None if path == "/me/player" and not registered else await self.request(method, path, *args)

        self.spotify.request.side_effect = connecting
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        waiting = await self.service.quick_snapshot()
        self.assertFalse(waiting["playbackPending"])
        self.assertFalse(waiting["refreshing"])
        self.assertEqual(waiting["retryAfter"], 2)
        for _ in range(20):
            self.now += 0.05
            await self.service.quick_snapshot()
        self.assertEqual(self.calls("GET", "/me/player"), 1)
        registered = True
        self.now = 1002
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        ready = await self.service.quick_snapshot()
        self.assertEqual(ready["playback"]["device"]["id"], "handheld")
        self.assertEqual(ready["retryAfter"], 0)
        self.assertEqual(self.calls("GET", "/me/player"), 2)
        self.assertEqual(self.calls("PUT", "/me/player"), 0)

    async def test_no_active_device_stops_fast_retries_after_startup_window(self):
        async def no_device(method, path, *args):
            return None if path == "/me/player" else await self.request(method, path, *args)

        self.spotify.request.side_effect = no_device
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        self.assertEqual((await self.service.quick_snapshot())["retryAfter"], 2)
        self.now += 20
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        settled = await self.service.quick_snapshot()
        self.assertFalse(settled["playbackPending"])
        self.assertFalse(settled["refreshing"])
        self.assertEqual(settled["retryAfter"], 0)
        for _ in range(20):
            self.now += 0.4
            await self.service.quick_snapshot()
        self.assertEqual(self.calls("GET", "/me/player"), 2)

    async def test_transient_failure_retries_after_cooldown_without_losing_account(self):
        offline = True

        async def recovering(method, path, *args):
            if path == "/me/player" and offline:
                raise SpotifyError("Cannot reach Spotify.", "offline")
            return await self.request(method, path, *args)

        self.spotify.request.side_effect = recovering
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        failed = await self.service.quick_snapshot()
        self.assertEqual(failed["retryAfter"], 3)
        self.assertTrue(failed["playbackPending"])
        self.assertEqual(failed["playbackError"], "Cannot reach Spotify.")
        for _ in range(10):
            await self.service.quick_snapshot()
        self.assertEqual(self.calls("GET", "/me/player"), 1)
        self.assertEqual(self.store.data["refresh_token"], "private-refresh")
        offline = False
        self.now += 3
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        recovered = await self.service.quick_snapshot()
        self.assertEqual(recovered["playback"]["device"]["id"], "handheld")
        self.assertIsNone(recovered["playbackError"])
        self.assertFalse(recovered["playbackPending"])
        self.assertEqual(recovered["retryAfter"], 0)
        self.assertEqual(self.calls("GET", "/me/player"), 2)
        self.assertEqual(self.calls("GET", "/me"), 1)

    async def test_account_or_access_errors_use_normal_retry_interval(self):
        async def denied(method, path, *args):
            if path == "/me/player":
                raise SpotifyError("Access denied.", "restricted")
            return await self.request(method, path, *args)

        self.spotify.request.side_effect = denied
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        self.assertEqual((await self.service.quick_snapshot())["retryAfter"], 15)
        for _ in range(4):
            self.now += 3
            await self.service.quick_snapshot()
        self.assertEqual(self.calls("GET", "/me/player"), 1)

    async def test_rate_limit_deadline_prevents_reads_until_provider_allows_them(self):
        self.spotify.backoff_until = self.now + 25
        for _ in range(10):
            result = await self.service.quick_snapshot()
        self.assertEqual(result["retryAfter"], 25)
        self.assertTrue(result["playbackPending"])
        self.spotify.request.assert_not_awaited()
        self.now += 25
        await self.service.quick_snapshot()
        await self.until(lambda: not self.service._quick_tasks)
        self.assertIsNotNone((await self.service.quick_snapshot())["playback"])

    async def test_balance_restoration_does_not_hide_ready_playback_or_duplicate_writes(self):
        self.store.update(audio_mode="balance", audio_balance=50)
        volume_gate = asyncio.Event()

        async def delayed_balance(method, path, *args):
            if method == "PUT":
                await volume_gate.wait()
            return await self.request(method, path, *args)

        self.spotify.request.side_effect = delayed_balance
        await self.service.quick_snapshot()
        await self.until(lambda: self.calls("PUT", "/me/player/volume") == 1)
        for _ in range(10):
            result = await asyncio.wait_for(self.service.quick_snapshot(), 0.1)
        self.assertEqual(result["playback"]["device"]["id"], "handheld")
        self.assertFalse(result["playbackPending"])
        self.assertTrue(result["refreshing"])
        self.assertEqual(self.calls("PUT", "/me/player/volume"), 1)
        self.mixer.set_volume.assert_not_awaited()
        volume_gate.set()
        await self.until(lambda: self.service.balance_target == (0, "handheld"))
        self.mixer.set_volume.assert_awaited_once_with(100)

    async def test_old_account_read_cannot_replace_new_account_status(self):
        old_gate = asyncio.Event()

        async def account_read(method, path, *args):
            old_account = self.spotify.epoch == 0
            if old_account:
                await old_gate.wait()
            if path == "/me":
                return {"display_name": "Old account" if old_account else "New account"}
            return remote_playback("Old track" if old_account else "New track")

        self.spotify.request.side_effect = account_read
        await self.service.quick_snapshot()
        await self.until(lambda: self.spotify.request.await_count == 2)
        self.spotify.epoch += 1
        switched = await self.service.quick_snapshot()
        self.assertIsNone(switched["playback"])
        await self.until(lambda: "profile" in self.service.cache and "playback" in self.service.cache)
        old_gate.set()
        await self.until(lambda: not self.service._quick_tasks)
        current = await self.service.quick_snapshot()
        self.assertEqual(current["profile"]["name"], "New account")
        self.assertEqual(current["playback"]["track"]["title"], "New track")

    async def test_slow_pre_command_read_cannot_undo_acknowledged_pause(self):
        read_gate = asyncio.Event()

        async def old_read(method, path, *args):
            if path == "/me/player":
                await read_gate.wait()
            return await self.request(method, path, *args)

        self.spotify.request.side_effect = old_read
        await self.service.quick_snapshot()
        await self.until(lambda: self.calls("GET", "/me/player") == 1)
        self.service.accept_transport(playback(remote_playback()), False, int(time.time() * 1000))
        acknowledged = await self.service.quick_snapshot()
        self.assertFalse(acknowledged["playback"]["playing"])
        read_gate.set()
        await self.until(lambda: not self.service._quick_tasks)
        settled = await self.service.quick_snapshot()
        self.assertFalse(settled["playback"]["playing"])
        self.assertEqual(settled["playback"]["progress"], acknowledged["playback"]["progress"])

    async def test_unload_cancels_read_owners_and_loaders_without_late_cache_updates(self):
        gate = asyncio.Event()
        cancelled = []

        async def stalled(method, path, *args):
            try:
                await gate.wait()
                return await self.request(method, path, *args)
            except asyncio.CancelledError:
                cancelled.append(path)
                raise

        self.spotify.request.side_effect = stalled
        await self.service.quick_snapshot()
        await self.until(lambda: self.spotify.request.await_count == 2)
        await asyncio.wait_for(self.service.close(), 1)
        self.assertCountEqual(cancelled, ["/me", "/me/player"])
        self.assertFalse(self.service.inflight)
        self.assertFalse(self.service._quick_tasks)
        gate.set()
        await asyncio.sleep(0)
        closed = await self.service.quick_snapshot()
        self.assertFalse(self.service.cache)
        self.assertFalse(closed["refreshing"])
        self.assertFalse(closed["playbackPending"])
        self.assertEqual(self.spotify.request.await_count, 2)
