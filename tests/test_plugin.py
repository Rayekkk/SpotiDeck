import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.player import Player
from backend.spotify import SpotifyError
from backend.storage import Store


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Path(self.temp.name) / "settings"
        self.runtime = Path(self.temp.name) / "runtime"
        fake_decky = types.SimpleNamespace(DECKY_PLUGIN_SETTINGS_DIR=str(self.settings),
                                          DECKY_PLUGIN_RUNTIME_DIR=str(self.runtime), logger=logging.getLogger("spotideck-tests"))
        with patch.dict(sys.modules, {"decky": fake_decky}):
            spec = importlib.util.spec_from_file_location("spotideck_plugin_under_test", Path(__file__).resolve().parent.parent / "main.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.plugin = module.Plugin()
            await self.plugin._main()

    async def asyncTearDown(self):
        await self.plugin._unload()
        self.temp.cleanup()

    async def test_initial_snapshot_is_disconnected_and_has_no_fake_music(self):
        result = await self.plugin.dispatch("snapshot", {})
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["connected"])
        self.assertIsNone(result["data"]["playback"])

    async def test_updates_work_without_spotify_and_do_not_block_snapshot(self):
        import threading
        started, release = threading.Event(), threading.Event()
        def slow_check():
            started.set()
            release.wait(2)
            return {"success": True, "no_release": True}
        with patch.object(self.plugin.updates, "check", side_effect=slow_check):
            pending = asyncio.create_task(self.plugin.dispatch("updates_check", {}))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                snapshot = await asyncio.wait_for(self.plugin.dispatch("snapshot", {}), 0.2)
                self.assertTrue(snapshot["ok"])
                self.assertFalse(snapshot["data"]["connected"])
            finally:
                release.set()
            self.assertTrue((await pending)["data"]["no_release"])

    async def test_update_download_version_is_validated_and_unload_closes_updater(self):
        with patch.object(self.plugin.updates, "_metadata") as metadata:
            result = await self.plugin.dispatch("updates_download", {"version": "../bad"})
            self.assertTrue(result["ok"])
            self.assertFalse(result["data"]["success"])
            metadata.assert_not_called()
        updater = self.plugin.updates
        await self.plugin._unload()
        self.assertTrue(updater._closed.is_set())
        self.assertFalse((await self.plugin.dispatch("updates_check", {}))["ok"])

    async def test_snapshot_rpc_keeps_local_panel_available_while_spotify_is_slow(self):
        service = self.plugin.service
        service.spotify.store.update(refresh_token="test-refresh")
        release = asyncio.Event()

        async def delayed_request(method, path, *args):
            await release.wait()
            return {"display_name": "Test listener"} if path == "/me" else None

        service.spotify.request = AsyncMock(side_effect=delayed_request)
        try:
            result = await asyncio.wait_for(self.plugin.dispatch("snapshot", {}), 0.2)
            self.assertTrue(result["ok"])
            self.assertTrue(result["data"]["connected"])
            self.assertTrue(result["data"]["playbackPending"])
            self.assertIsNone(result["data"]["playback"])
            self.assertIn("audio", result["data"])
            self.assertIn("player", result["data"])
        finally:
            release.set()

    async def test_partial_startup_failure_closes_resources_and_disables_dispatch(self):
        await self.plugin._unload()
        with patch("backend.audio.AudioMixer.start", new=AsyncMock(side_effect=ValueError("test failure"))), \
             patch("backend.player.Player.stop", new=AsyncMock()) as stop:
            await self.plugin._main()
        stop.assert_awaited_once_with(persist=False)
        self.assertIsNone(self.plugin.service)
        result = await self.plugin.dispatch("snapshot", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "startup")
        await self.plugin._main()
        self.assertIsNone(self.plugin.startup_error)
        self.assertTrue((await self.plugin.dispatch("snapshot", {}))["ok"])

    async def test_startup_warms_playback_without_waiting_for_network(self):
        await self.plugin._unload()
        Store(self.settings).update(refresh_token="test-refresh")
        release = asyncio.Event()
        started = asyncio.Event()

        async def delayed_request(method, path, *args):
            started.set()
            await release.wait()
            return {"display_name": "Test listener"} if path == "/me" else None

        with patch("backend.spotify.Spotify.request", new=AsyncMock(side_effect=delayed_request)):
            try:
                await asyncio.wait_for(self.plugin._main(), 0.2)
                await asyncio.wait_for(started.wait(), 0.2)
                self.assertFalse(self.plugin.closing)
                self.assertIsNone(self.plugin.startup_error)
                result = await asyncio.wait_for(self.plugin.dispatch("snapshot", {}), 0.2)
                self.assertTrue(result["ok"])
                self.assertTrue(result["data"]["playbackPending"])
            finally:
                release.set()
                await self.plugin._unload()

    async def test_unknown_rpc_and_invalid_inputs_cannot_invoke_arbitrary_methods(self):
        for method, args in [("__dict__", {}), ("_unload", {}), ("snapshot", "bad")]:
            result = await self.plugin.dispatch(method, args)
            self.assertFalse(result["ok"])
        self.assertFalse(self.plugin.closing)

    async def test_unload_closes_callback_and_stops_owned_player(self):
        await self.plugin.service.spotify.begin_auth("a" * 32)
        self.plugin.service.player.stop = AsyncMock()
        await self.plugin._unload()
        self.assertIsNone(self.plugin.service.spotify.server)
        self.plugin.service.player.stop.assert_awaited_once_with(persist=False)
        result = await self.plugin.dispatch("snapshot", {})
        self.assertFalse(result["ok"])

    async def test_signout_forgets_both_api_and_local_player_authentication(self):
        player = self.plugin.service.player
        (self.runtime / "session").mkdir(parents=True)
        (self.runtime / "session" / "credentials").write_text("fake-player-session")
        player.store.update(refresh_token="fake-refresh", access_token="fake-access", soloist_key="fake-key")
        result = await self.plugin.dispatch("disconnect", {})
        self.assertTrue(result["ok"])
        self.assertFalse((self.runtime / "session").exists())
        saved = Store(self.settings).data
        self.assertIsNone(saved["refresh_token"])
        self.assertIsNone(saved["soloist_key"])
        self.assertFalse(saved["player_enabled"])

    async def test_player_exit_race_cannot_skip_audio_restore(self):
        self.plugin.service.player.stop = AsyncMock(side_effect=ProcessLookupError())
        self.plugin.service.mixer.close = AsyncMock()
        with self.assertRaises(ProcessLookupError):
            await self.plugin._unload()
        self.plugin.service.mixer.close.assert_awaited_once()
        self.plugin.service.player.stop = AsyncMock()

    async def test_stop_only_terminates_the_process_it_started(self):
        player = self.plugin.service.player
        fake = types.SimpleNamespace(returncode=None, terminate=lambda: None, kill=lambda: None, wait=AsyncMock(return_value=0))
        with patch.object(fake, "terminate") as terminate, patch.object(fake, "kill") as kill:
            player.process = fake
            await player.stop()
            terminate.assert_called_once()
            kill.assert_not_called()
        self.assertFalse(player.status()["running"])

    async def test_player_starts_as_spotideck_and_reuses_saved_pairing(self):
        player = self.plugin.service.player
        player.supported = True
        player.binary.parent.mkdir(parents=True)
        player.binary.write_bytes(b"fake-player-executable")
        session = self.runtime / "session"
        session.mkdir()
        pairing = session / "credentials"
        pairing.write_text("existing-pairing")
        player.store.update(soloist_key="fake-player-key")
        fake = types.SimpleNamespace(returncode=None, terminate=lambda: None, kill=lambda: None,
                                     wait=AsyncMock(return_value=0))
        with patch("backend.player.os.geteuid", return_value=1000, create=True), \
                patch("backend.player.player_environment", return_value={}), \
                patch("backend.player.asyncio.sleep", new_callable=AsyncMock), \
                patch("backend.player.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake) as spawn:
            await player.start()
        args = spawn.await_args.args
        self.assertEqual(args[args.index("--device-name") + 1], "SpotiDeck")
        self.assertEqual(args[args.index("--data-dir") + 1], str(session))
        self.assertEqual(pairing.read_text(), "existing-pairing")
        self.assertTrue(player.store.data["player_enabled"])
        self.assertTrue(player.status()["running"])

    async def test_key_cannot_change_while_player_is_running(self):
        player = self.plugin.service.player
        player.process = types.SimpleNamespace(returncode=None)
        try:
            with self.assertRaises(SpotifyError):
                await player.save_key("another-fake-player-key")
        finally:
            player.process = None
