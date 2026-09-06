import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.service import Service, playback
from backend.spotify import SpotifyError


def raw_state(playing=True, device_id="handheld", track_id="a" * 22):
    return {"is_playing": playing, "progress_ms": 20000, "timestamp": int(time.time() * 1000),
            "item": {"id": track_id, "uri": "spotify:track:" + track_id, "type": "track",
                     "name": "Test track", "duration_ms": 200000},
            "device": {"id": device_id, "name": "Handheld", "is_active": True, "is_restricted": False, "supports_volume": True},
            "actions": {"disallows": {"resuming" if playing else "pausing": True}}}


class TransportStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.spotify = SimpleNamespace(epoch=0, connected=True, pending=None, auth_error=None,
                                       store=SimpleNamespace(data={}), request=AsyncMock(return_value=None))
        self.service = Service(self.spotify, SimpleNamespace(status=lambda: {}))
        self.service.cache["profile"] = (time.monotonic() + 3600, {"name": "Listener"})
        self.service.cache["playback"] = (time.monotonic() + 10, playback(raw_state()))

    async def asyncTearDown(self):
        await self.service.close()

    async def fresh(self, state):
        self.service.cache.pop("playback", None)
        self.spotify.request.return_value = state
        return (await self.service.snapshot())["playback"]

    async def test_pause_ack_freezes_progress_and_preserves_unrelated_cached_reads(self):
        self.service.cache["library:playlists:0"] = (time.monotonic() + 60, "existing library")
        await self.service.command("pause")
        first = (await self.service.snapshot())["playback"]
        self.assertFalse(first["playing"])
        self.assertNotIn("resuming", first["disallows"])
        self.assertEqual(first["progress"], (await self.service.snapshot())["playback"]["progress"])
        self.assertIn("library:playlists:0", self.service.cache)
        self.spotify.request.assert_awaited_once_with("PUT", "/me/player/pause", {"device_id": "handheld"}, None)

    async def test_lagging_read_does_not_undo_pause_and_confirmed_state_ends_grace(self):
        await self.service.command("pause")
        delayed = await self.fresh(raw_state(True))
        self.assertFalse(delayed["playing"])
        self.assertNotIn("resuming", delayed["disallows"])
        self.assertIsNotNone(self.service.accepted_playback)
        confirmed = await self.fresh(raw_state(False))
        self.assertFalse(confirmed["playing"])
        self.assertIsNone(self.service.accepted_playback)
        # Another controller's subsequent change is authoritative immediately.
        self.assertTrue((await self.fresh(raw_state(True)))["playing"])

    async def test_resume_is_available_without_waiting_for_a_slow_old_read(self):
        await self.service.command("pause")
        self.service.cache.pop("playback", None)
        started, gate = asyncio.Event(), asyncio.Event()
        async def request(method, *args):
            if method == "GET":
                started.set()
                await gate.wait()
                return raw_state(True)  # Predates even the pause; cannot confirm resume.
        self.spotify.request.side_effect = request
        read = asyncio.create_task(self.service.snapshot())
        await started.wait()
        # The UI's last accepted target must remain usable while this GET runs.
        self.service.cache["playback"] = (time.monotonic() + 10, self.service.accepted_playback["state"])
        await asyncio.wait_for(self.service.command("resume"), 0.2)
        gate.set()
        await read
        self.assertTrue(self.service.cache["playback"][1]["playing"])
        self.assertNotIn("pausing", self.service.cache["playback"][1]["disallows"])
        self.assertIsNotNone(self.service.accepted_playback)
        calls = [call.args[0] for call in self.spotify.request.await_args_list]
        self.assertEqual(calls, ["PUT", "GET", "PUT"])

    async def test_new_device_or_track_and_account_changes_end_the_overlay(self):
        for state in (raw_state(True, device_id="another-device"), raw_state(True, track_id="b" * 22)):
            with self.subTest(state=state):
                self.service.cache["playback"] = (time.monotonic() + 10, playback(raw_state()))
                self.spotify.request.return_value = None
                await self.service.command("pause")
                self.assertTrue((await self.fresh(state))["playing"])
                self.assertIsNone(self.service.accepted_playback)
        await self.service.command("pause")
        self.spotify.epoch += 1
        self.assertTrue((await self.fresh(raw_state(True)))["playing"])
        self.assertIsNone(self.service.accepted_playback)

    async def test_grace_expires_and_is_not_extended_by_cache_reads(self):
        await self.service.command("pause")
        self.service.accepted_playback["until"] = time.monotonic() + 1
        await self.fresh(raw_state(True))
        self.assertLessEqual(self.service.cache["playback"][0], self.service.accepted_playback["until"])
        self.service.accepted_playback["until"] = time.monotonic() - 1
        self.assertTrue((await self.fresh(raw_state(True)))["playing"])
        self.assertIsNone(self.service.accepted_playback)

    async def test_failed_command_never_installs_an_accepted_state(self):
        before = copy.deepcopy(self.service.cache["playback"][1])
        self.spotify.request.side_effect = SpotifyError("Rejected", "restricted")
        with self.assertRaises(SpotifyError):
            await self.service.command("pause")
        self.assertEqual(self.service.cache["playback"][1], before)
        self.assertIsNone(self.service.accepted_playback)

    async def test_unrelated_controls_preserve_pause_without_extending_grace(self):
        for command, value in [("volume", 35), ("shuffle", True), ("repeat", "track"),
                               ("seek", 42000), ("save", "spotify:track:" + "a" * 22),
                               ("queue", "spotify:track:" + "a" * 22)]:
            with self.subTest(command=command):
                self.spotify.request.return_value = None
                await self.service.command("pause")
                deadline = self.service.accepted_playback["until"]
                await self.service.command(command, value)
                state = await self.fresh(raw_state(True))
                self.assertFalse(state["playing"])
                self.assertEqual(self.service.accepted_playback["until"], deadline)
                if command == "volume": self.assertEqual(state["device"]["volume"], 35)
                if command == "seek": self.assertEqual(state["progress"], 42000)

    async def test_matching_old_state_cannot_confirm_a_newer_toggle(self):
        await self.service.command("pause")
        await self.service.command("resume")
        pending = self.service.accepted_playback
        old = {**raw_state(True), "timestamp": pending["started_at"] - 5000}
        await self.fresh(old)
        self.assertIsNotNone(self.service.accepted_playback)
        intermediate_pause = {**raw_state(False), "timestamp": pending["started_at"] - 1000}
        self.assertTrue((await self.fresh(intermediate_pause))["playing"])
        confirmed = {**raw_state(True), "timestamp": pending["started_at"] + 10}
        await self.fresh(confirmed)
        self.assertIsNone(self.service.accepted_playback)


if __name__ == "__main__":
    unittest.main()
