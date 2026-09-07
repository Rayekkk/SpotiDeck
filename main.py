import asyncio
import os
import sys
from pathlib import Path

import decky

# Decky loads this file by absolute path without adding its directory to sys.path.
PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from backend.player_router import Player
from backend.audio import AudioMixer
from backend.service import Service
from backend.spotify import Spotify, SpotifyError
from backend.storage import Store
from backend.updates import PluginUpdates


class Plugin:
    service = None
    updates = None
    startup_error = None
    closing = False

    async def _main(self):
        self.startup_error = None
        self.service = None
        try:
            if self.updates:
                self.updates.close()
            self.updates = PluginUpdates.for_plugin(PLUGIN_ROOT)
            directory = getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", None) or os.environ.get("DECKY_PLUGIN_SETTINGS_DIR")
            if not directory:
                raise ValueError("Decky did not provide a settings directory.")
            runtime = getattr(decky, "DECKY_PLUGIN_RUNTIME_DIR", None) or os.environ.get("DECKY_PLUGIN_RUNTIME_DIR") or str(Path(directory) / "runtime")
            store = Store(directory)
            player = Player(store, runtime)
            await player.initialize()
            mixer = AudioMixer(store, owned_player_pid=lambda: getattr(player.process, "pid", None))
            self.service = Service(Spotify(store), player, mixer)
            await mixer.start()
            self.closing = False
            if store.data.get("player_enabled"):
                try:
                    await self.service.player.start(persist=False)
                except SpotifyError as error:
                    self.service.player.error = str(error)
                self.service.start_auto_select()
            # Warm playback once during startup; the panel never waits for network I/O.
            await self.service.quick_snapshot()
            decky.logger.info("SpotiDeck 1.0.0 loaded (pid=%s uid=%s)", os.getpid(),
                              os.geteuid() if hasattr(os, "geteuid") else "unavailable")
        except Exception:
            self.startup_error = "SpotiDeck could not initialize its settings or local services. Check the plugin installation."
            decky.logger.error(self.startup_error)
            try:
                await self._unload()
            finally:
                self.service = None

    async def dispatch(self, method, args=None):
        if not self.service or self.closing:
            return {"ok": False, "error": self.startup_error or "SpotiDeck is starting. Please reopen the panel.", "code": "startup", "retry_after": 0}
        if not isinstance(args, dict) or not isinstance(method, str):
            return {"ok": False, "error": "Invalid request.", "code": "invalid", "retry_after": 0}
        service = self.service
        try:
            if method == "updates_check":
                data = await asyncio.to_thread(self.updates.check)
            elif method == "updates_download":
                data = await asyncio.to_thread(self.updates.download, args.get("version"))
            elif method == "snapshot":
                data = await service.quick_snapshot()
            elif method == "library":
                data = await service.library(args.get("kind"), args.get("offset", 0))
            elif method == "search":
                data = await service.search(args.get("query"), args.get("kind"), args.get("offset", 0))
            elif method == "tracks":
                data = await service.tracks(args.get("kind"), args.get("id"), args.get("offset", 0))
            elif method == "devices":
                data = await service.devices()
            elif method == "queue":
                data = await service.queue()
            elif method == "library_state":
                data = await service.library_state(args.get("uris"))
            elif method == "pin":
                data = await service.pin(args.get("action"), args.get("value"))
            elif method == "playlist":
                data = await service.playlist(args.get("action"), args.get("value"))
            elif method == "timer":
                data = await service.timer(args.get("minutes"))
            elif method == "preferences":
                data = await service.preferences(args.get("action"), args.get("value"))
            elif method == "command":
                data = await service.command(args.get("command"), args.get("value"))
            elif method == "audio":
                data = await service.audio(args.get("action"), args.get("value"))
            elif method == "connect":
                await service.cancel_device_selection()
                data = await service.spotify.begin_auth(args.get("client_id"), args.get("mode", "handheld"))
            elif method == "cancel_connect":
                await service.spotify.cancel_auth()
                data = None
            elif method == "disconnect":
                await service.cancel_device_selection()
                await service.cancel_auto_select()
                await service.clear_personal_data()
                await service.spotify.disconnect()
                service.invalidate()
                await service.player.forget_session()
                data = None
            elif method == "player":
                action = args.get("action")
                if action == "install":
                    await service.player.install()
                elif action == "start":
                    await service.player.start()
                    service.start_auto_select()
                elif action == "stop":
                    await service.cancel_device_selection()
                    await service.cancel_auto_select()
                    await service.player.stop()
                elif action == "key":
                    await service.player.save_key(args.get("key"))
                elif action == "engine":
                    await service.cancel_device_selection()
                    await service.cancel_auto_select()
                    async with service.command_lock:
                        await service.player.set_engine(args.get("key"))
                        service.invalidate()
                        service._local_device_id = None
                        service._local_blocked = False
                        service._local_transfer_target = None
                        service.balance_target = None
                        service.balance_transfer = None
                    if service.player.status().get("running"):
                        service.start_auto_select()
                elif action == "open":
                    await service.player.open_app()
                else:
                    raise SpotifyError("Unknown player action.")
                data = None
            else:
                raise SpotifyError("Unknown request.")
            return {"ok": True, "data": data}
        except SpotifyError as error:
            return {"ok": False, "error": str(error), "code": error.code, "retry_after": error.retry_after}
        except Exception:
            # Never log arguments, OAuth callbacks, tokens, keys or response bodies.
            decky.logger.error("SpotiDeck request failed; private request details omitted")
            return {"ok": False, "error": "This request could not be completed. Please try again.", "code": "internal", "retry_after": 0}

    async def _unload(self):
        self.closing = True
        if self.updates:
            self.updates.close()
        if self.service:
            await self.service.cancel_device_selection()
            await self.service.cancel_auto_select()
            try:
                await self.service.spotify.close()
            finally:
                try:
                    await self.service.player.stop(persist=False)
                finally:
                    await self.service.close()
