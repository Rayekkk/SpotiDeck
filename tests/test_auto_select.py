import asyncio
import copy
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.service import Service
from backend.spotify import SpotifyError
from backend.storage import Store


class AutoSelectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.local = {"status": "paused", "is_active": False, "device_name": "SpotiDeck", "revision": 1,
                      "item": {"uri": "spotify:track:" + "a" * 22, "entity_type": "track",
                               "decorations": {"identity": {"name": "Track"}, "playback": {"duration_ms": 100000}}},
                      "position": {"position_ms": 1234}, "volume": 70, "options": {}}
        self.status = {"running": True, "ready": False, "active": False}
        self.spotify = SimpleNamespace(store=Store(self.directory.name), epoch=0, connected=True,
                                       pending=None, auth_error=None, request=AsyncMock(return_value=None))
        self.player = SimpleNamespace(status=lambda: dict(self.status), local_active=False,
                                      local_snapshot=lambda: copy.deepcopy(self.local), local_command=AsyncMock(),
                                      activate_local=AsyncMock(side_effect=self.activate))
        self.service = Service(self.spotify, self.player)
        self.service.AUTO_SELECT_POLL = 0.005
        self.service.AUTO_SELECT_SETTLE = 0.005
        self.service.AUTO_SELECT_TIMEOUT = 0.2
        self.service.AUTO_SELECT_RETRIES = (0.005, 0.005)

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def activate(self, *, allowed):
        if not allowed():
            raise SpotifyError("Cancelled", "local_unavailable")
        self.local["is_active"] = True
        self.status["active"] = self.player.local_active = True
        return {"accepted": True, "active": True}

    async def until(self, predicate):
        async def spin():
            while not predicate():
                await asyncio.sleep(0.002)
        await asyncio.wait_for(spin(), 1)

    async def test_startup_selects_owned_player_without_open_panel_or_cloud_reads(self):
        self.service.start_auto_select()
        self.assertTrue(self.service._local_snapshot()["player"]["selecting"])
        await asyncio.sleep(0.01)
        self.player.activate_local.assert_not_awaited()
        self.status["ready"] = True
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.activate_local.assert_awaited_once()
        self.spotify.request.assert_not_awaited()
        self.player.local_command.assert_not_awaited()
        state = self.service._owned_playback()
        self.assertEqual(state["device"]["name"], "SpotiDeck")
        self.assertFalse(state["playing"])
        self.assertIsNone(self.service._auto_select_error)

    async def test_saved_balance_syncs_without_an_open_panel(self):
        self.spotify.store.update(audio_mode="balance", audio_balance=25)
        self.service.mixer = SimpleNamespace(status=lambda: {"supported": True, "otherVolume": 100,
                                            "otherStreams": 1, "error": None},
                                            set_volume=AsyncMock(), close=AsyncMock())
        self.status["ready"] = True
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        await self.service._quick_balance_task
        self.player.local_command.assert_awaited_once_with("volume", 50)
        self.service.mixer.set_volume.assert_awaited_once_with(100)
        self.spotify.request.assert_not_awaited()

    async def test_startup_does_not_pause_already_active_user_playback(self):
        self.status.update(ready=True, active=True)
        self.player.local_active = True
        self.local.update(status="playing", is_active=True)
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.activate_local.assert_not_awaited()
        self.player.local_command.assert_not_awaited()

    async def test_transient_paused_active_boot_state_does_not_finish_selection(self):
        self.service.AUTO_SELECT_SETTLE = 0.03
        self.status.update(ready=True, active=True)
        self.player.local_active = True
        self.local["is_active"] = True
        self.service.start_auto_select()
        await asyncio.sleep(0.01)
        self.assertIsNotNone(self.service._auto_select_task)
        self.player.activate_local.assert_not_awaited()
        self.status["active"] = self.player.local_active = self.local["is_active"] = False
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.activate_local.assert_awaited_once()
        self.assertTrue(self.player.local_active)

    async def test_confirmed_activation_that_drops_during_boot_gets_only_one_correction(self):
        self.service.AUTO_SELECT_SETTLE = 0.02
        self.status["ready"] = True
        drops = []
        async def activate(*, allowed):
            result = await self.activate(allowed=allowed)
            if self.player.activate_local.await_count == 1:
                async def drop():
                    await asyncio.sleep(0.002)
                    self.status["active"] = self.player.local_active = self.local["is_active"] = False
                drops.append(asyncio.create_task(drop()))
            return result
        self.player.activate_local.side_effect = activate
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        await asyncio.gather(*drops)
        self.assertEqual(self.player.activate_local.await_count, 2)
        self.assertTrue(self.player.local_active)

    async def test_activation_never_issues_play_and_pauses_transferred_playing_context(self):
        self.status["ready"] = True
        async def activate(*, allowed):
            result = await self.activate(allowed=allowed)
            self.local["status"] = "playing"
            return result
        self.player.activate_local.side_effect = activate
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.local_command.assert_awaited_once_with("pause")
        self.spotify.request.assert_not_awaited()

    async def test_readiness_timeout_is_visible_and_snapshot_does_not_restart_task(self):
        self.service.AUTO_SELECT_TIMEOUT = 0.02
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        for _ in range(3):
            snapshot = self.service._local_snapshot()
            self.assertFalse(snapshot["player"]["selecting"])
            self.assertIn("still connecting", snapshot["player"]["selectionError"])
        self.player.activate_local.assert_not_awaited()

    async def test_only_safe_predispatch_failure_is_retried_and_bounded(self):
        self.status["ready"] = True
        self.player.activate_local.side_effect = SpotifyError("Not ready", "local_unavailable")
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        self.assertEqual(self.player.activate_local.await_count, 3)
        self.assertEqual(self.service._auto_select_error, "Not ready")

    async def test_uncertain_activation_is_never_replayed(self):
        self.status["ready"] = True
        self.player.activate_local.side_effect = SpotifyError("Activation uncertain", "local_uncertain")
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.activate_local.assert_awaited_once()
        self.assertEqual(self.service._auto_select_error, "Activation uncertain")

    async def test_explicit_remote_selection_cancels_waiting_auto_selection(self):
        self.service.start_auto_select()
        self.spotify.request.side_effect = [
            {"devices": [{"id": "phone", "name": "Phone", "is_active": False}]}, None]
        await self.service.command("transfer", "phone")
        self.status["ready"] = True
        await asyncio.sleep(0.02)
        self.player.activate_local.assert_not_awaited()
        self.assertIsNone(self.service._auto_select_task)
        transfers = [call for call in self.spotify.request.call_args_list
                     if call.args[:2] == ("PUT", "/me/player")]
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].args[3], {"device_ids": ["phone"], "play": False})

    async def test_remote_transfer_cancels_inflight_activation_before_acquiring_command_lock(self):
        self.status["ready"] = True
        entered, gate, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def activate(*, allowed):
            entered.set()
            try:
                await gate.wait()
            finally:
                stopped.set()
        self.player.activate_local.side_effect = activate
        self.service.start_auto_select()
        await entered.wait()
        self.spotify.request.side_effect = [
            {"devices": [{"id": "phone", "name": "Phone", "is_active": False}]}, None]
        await asyncio.wait_for(self.service.command("transfer", "phone"), 0.2)
        self.assertTrue(stopped.is_set())
        self.player.local_command.assert_not_awaited()
        self.assertIsNone(self.service._auto_select_task)

    async def test_pending_oauth_waits_for_new_session_before_activation(self):
        self.status["ready"] = True
        self.spotify.pending = {"epoch": 0}
        self.service.start_auto_select()
        await asyncio.sleep(0.01)
        self.player.activate_local.assert_not_awaited()
        self.spotify.epoch += 1
        self.spotify.pending = None
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.activate_local.assert_awaited_once()

    async def test_logged_out_runtime_recovers_only_after_network_success_without_qam(self):
        self.status["wsConnected"] = True
        cloud_gate = asyncio.Event()
        async def profile(*args):
            await cloud_gate.wait()
            return {"id": "listener", "display_name": "Listener"}
        self.spotify.request.side_effect = profile
        async def recover(*, allowed):
            self.assertTrue(allowed())
            self.status["ready"] = True
            return True
        self.player.recover_login = AsyncMock(side_effect=recover)
        self.service.start_auto_select()
        await asyncio.sleep(0.01)
        self.player.recover_login.assert_not_awaited()
        cloud_gate.set()
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.recover_login.assert_awaited_once()
        self.player.activate_local.assert_awaited_once()

    async def test_network_failure_does_not_restart_logged_out_runtime(self):
        self.status["wsConnected"] = True
        self.service.AUTO_SELECT_TIMEOUT = 0.02
        self.spotify.request.side_effect = SpotifyError("Offline", "offline")
        self.player.recover_login = AsyncMock()
        self.service.start_auto_select()
        await self.until(lambda: self.service._auto_select_task is None)
        self.player.recover_login.assert_not_awaited()
        self.player.activate_local.assert_not_awaited()

    async def test_unload_drains_startup_task(self):
        self.service.start_auto_select()
        task = self.service._auto_select_task
        await self.service.close()
        self.assertTrue(task.done())
        self.assertIsNone(self.service._auto_select_task)
        self.player.activate_local.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
