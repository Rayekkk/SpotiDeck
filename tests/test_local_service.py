import asyncio
import copy
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.local_state import LOCAL_DEVICE
from backend.service import Service, playback
from backend.spotify import SpotifyError
from backend.storage import Store

ID = "a" * 22
URI = "spotify:track:" + ID


class LocalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.local = {"status": "playing", "is_active": True, "device_name": "SpotiDeck", "revision": 1,
                      "item": {"uri": URI, "entity_type": "track", "decorations": {"identity": {"name": "Song"},
                               "playback": {"duration_ms": 90000}, "visual_identity": {"cover": [{"url": "https://i.scdn.co/image/song"}]}}},
                      "position": {"position_ms": 20000}, "volume": 80, "options": {"shuffle": False, "repeat": "off"},
                      "received_at": int(time.time() * 1000)}
        self.spotify = SimpleNamespace(store=Store(self.directory.name), epoch=0, connected=True, pending=None,
                                       auth_error=None, request=AsyncMock(return_value=None))
        self.player = SimpleNamespace(status=lambda: {"running": True}, local_snapshot=lambda: copy.deepcopy(self.local),
                                      local_active=True, local_command=AsyncMock(return_value={"accepted": True}))
        self.service = Service(self.spotify, self.player)
        self.service.cache["profile"] = (time.monotonic() + 3600, {"id": "listener", "name": "Listener"})
        self.raw = {"is_playing": True, "item": {"uri": URI, "id": ID, "type": "track", "name": "Song", "duration_ms": 90000},
                    "device": {"id": "actual-connect-id", "name": "SpotiDeck", "volume_percent": 80, "is_active": True}}

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_local_events_override_lagging_cloud_without_waiting(self):
        self.service.cache["playback"] = (time.monotonic() + 10, playback(self.raw))
        result = await self.service.quick_snapshot()
        self.assertEqual(result["playback"]["source"], "soloist")
        self.assertEqual(result["playback"]["device"]["id"], "actual-connect-id")
        self.local.update(status="paused", revision=2)
        result = await self.service.quick_snapshot()
        self.assertFalse(result["playback"]["playing"])
        self.assertEqual(result["playback"]["track"]["image"], "https://i.scdn.co/image/song")
        self.spotify.request.assert_not_awaited()

    async def test_local_pause_resume_use_socket_and_ack_updates_immediately(self):
        await self.service.command("pause")
        self.player.local_command.assert_awaited_once_with("pause", None)
        self.assertFalse((await self.service.quick_snapshot())["playback"]["playing"])
        await self.service.command("resume")
        self.assertTrue((await self.service.quick_snapshot())["playback"]["playing"])
        self.assertEqual(self.player.local_command.call_args.args, ("play", None))
        writes = [c for c in self.spotify.request.call_args_list if c.args[0] != "GET"]
        self.assertEqual(writes, [])

    async def test_uncertain_local_write_is_never_replayed_on_web_api(self):
        self.player.local_command.side_effect = SpotifyError("Unconfirmed", "local_uncertain")
        with self.assertRaises(SpotifyError):
            await self.service.command("next")
        self.spotify.request.assert_not_awaited()

    async def test_context_play_resolves_real_id_before_cloud_fallback(self):
        self.player.local_command.side_effect = SpotifyError("Use context API", "local_unavailable")
        self.spotify.request.side_effect = [self.raw, None]
        await self.service.command("play", {"uri": URI, "contextUri": "spotify:playlist:" + "p" * 22, "position": 3})
        self.assertEqual(self.spotify.request.call_args.args[2], {"device_id": "actual-connect-id"})
        self.assertEqual(self.spotify.request.call_args.args[3]["offset"], {"position": 3})

    async def test_matching_device_name_without_matching_track_never_binds_cloud_target(self):
        self.player.local_command.side_effect = SpotifyError("Use context API", "local_unavailable")
        self.spotify.request.return_value = {**self.raw, "item": {**self.raw["item"], "uri": "spotify:track:" + "b" * 22}}
        with self.assertRaises(SpotifyError) as raised:
            await self.service.command("play", {"uri": URI, "contextUri": "spotify:playlist:" + "p" * 22, "position": 3})
        self.assertEqual(raised.exception.code, "no_device")
        self.assertEqual(self.spotify.request.call_args.args[0], "GET")
        self.assertEqual(self.spotify.request.await_count, 1)

    async def test_local_disconnection_does_not_replay_against_unknown_device(self):
        self.player.local_active = False
        self.player.local_command.side_effect = SpotifyError("Gone", "local_unavailable")
        with self.assertRaises(SpotifyError):
            await self.service.command("next")
        self.spotify.request.assert_not_awaited()

    async def test_explicit_remote_transfer_blocks_stale_local_state(self):
        self.service.cache["playback"] = (time.monotonic() + 10, playback(self.raw))
        await self.service.quick_snapshot()
        self.spotify.request.side_effect = [
            {"devices": [{"id": "phone", "name": "Phone", "is_active": False}]}, None]
        await self.service.command("transfer", "phone")
        self.assertIsNone(self.service._owned_playback())
        self.local["is_active"] = False
        self.assertIsNone(self.service._owned_playback())
        self.local["is_active"] = True
        self.assertEqual(self.service._owned_playback()["device"]["id"], LOCAL_DEVICE)

    async def test_first_transfer_to_local_player_binds_only_from_fresh_matching_api(self):
        self.local["is_active"] = False
        self.spotify.request.side_effect = [
            {"devices": [{"id": "actual-connect-id", "name": "SpotiDeck", "is_active": False}]}, None]
        await self.service.command("transfer", "actual-connect-id")
        self.local["is_active"] = True
        self.assertIsNone(self.service._owned_playback(playback(self.raw)))
        # A library/transport command between transfer and discovery changes the
        # cache generation, but must not strand an otherwise confirmed target.
        self.service.invalidate_after_command("save", URI)
        self.spotify.request.side_effect = None
        self.spotify.request.return_value = self.raw
        result = await self.service._playback_load()
        self.assertEqual(result["source"], "soloist")
        self.assertEqual(result["device"]["id"], "actual-connect-id")
        self.assertFalse(self.service._local_blocked)

    async def test_remote_transfer_does_not_unblock_on_old_id_or_mismatched_metadata(self):
        self.spotify.request.side_effect = [
            {"devices": [{"id": "phone", "name": "Phone", "is_active": False}]}, None]
        await self.service.command("transfer", "phone")
        for raw in (self.raw, {**self.raw, "device": {**self.raw["device"], "id": "phone", "name": "Phone"}},
                    {**self.raw, "device": {**self.raw["device"], "id": "phone"}, "item": {**self.raw["item"], "uri": "spotify:track:" + "b" * 22}}):
            self.assertIsNone(self.service._owned_playback(playback(raw), fresh=True))
        self.assertTrue(self.service._local_blocked)

    async def test_pre_transfer_inflight_api_cannot_release_new_transfer_guard(self):
        started, release, fresh_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        reads = 0
        async def request(method, path, *args):
            nonlocal reads
            if path == "/me/player" and method == "GET":
                reads += 1
                if reads == 1:
                    started.set()
                    await release.wait()
                else:
                    await fresh_release.wait()
                return self.raw
            if path == "/me/player/devices":
                return {"devices": [{"id": "actual-connect-id", "name": "SpotiDeck", "is_active": False}]}
            return None
        self.spotify.request.side_effect = request
        read = asyncio.create_task(self.service._playback_load())
        await started.wait()
        await self.service.command("transfer", "actual-connect-id")
        release.set()
        result = await read
        self.assertNotEqual((result or {}).get("source"), "soloist")
        self.assertTrue(self.service._local_blocked)
        fresh_release.set()
        await self.service._device_selection_task
        self.assertFalse(self.service._local_blocked)

    async def test_local_queue_uses_event_metadata(self):
        self.player.local_queue = lambda: [{"item": self.local["item"]}]
        result = await self.service.queue()
        self.assertEqual(result[0]["uri"], URI)
        self.spotify.request.assert_not_awaited()

    async def test_unmapped_local_polling_does_not_extend_discovery_deadline(self):
        deadline = time.monotonic() + 0.5
        self.service.cache["playback"] = (deadline, self.service._owned_playback())
        for _ in range(3):
            await self.service.quick_snapshot()
        self.assertLessEqual(self.service.cache["playback"][0], deadline)
        self.service.cache["playback"] = (time.monotonic() - 1, self.service._owned_playback())
        await self.service.quick_snapshot()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(any(c.args[1] == "/me/player" for c in self.spotify.request.call_args_list))

    async def test_inactive_local_state_is_cleared_and_cloud_refresh_starts(self):
        self.service.cache["playback"] = (time.monotonic() + 25, self.service._owned_playback())
        self.local["is_active"] = False
        result = await self.service.quick_snapshot()
        self.assertIsNone(result["playback"])
        self.assertTrue(result["playbackPending"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(any(c.args[1] == "/me/player" for c in self.spotify.request.call_args_list))

    async def test_balancing_local_music_preserves_socket_target(self):
        self.service.mixer = SimpleNamespace(status=lambda: {"supported": True, "otherVolume": 100, "error": None},
                                             set_volume=AsyncMock(), close=AsyncMock())
        state = self.service._owned_playback()
        await self.service._apply_balance(state, 25, {"otherVolume": 100})
        self.player.local_command.assert_awaited_once_with("volume", 50)
        self.service.mixer.set_volume.assert_awaited_once_with(100)
        self.spotify.request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
