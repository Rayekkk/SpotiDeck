import asyncio
import math
import re
import time
from collections import OrderedDict

from .spotify import SpotifyError
from .catalog import Catalog, image, media, offset_value, item_id, uri_value
from .local_state import LOCAL_DEVICE, local_playback


def device(data):
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return {"id": data["id"], "name": data.get("name", "Spotify device"), "type": data.get("type", "Device"),
            "active": bool(data.get("is_active")), "restricted": bool(data.get("is_restricted")),
            "volume": data.get("volume_percent"), "supportsVolume": data.get("supports_volume", data.get("volume_percent") is not None)}


def playback(data):
    if not data:
        return None
    return {"track": media(data.get("item")), "playing": bool(data.get("is_playing")),
            "progress": data.get("progress_ms") or 0, "shuffle": bool(data.get("shuffle_state")),
            "repeat": data.get("repeat_state", "off"), "device": device(data.get("device")),
            "disallows": (data.get("actions") or {}).get("disallows") or {},
            "sourceTimestamp": data.get("timestamp"), "updatedAt": int(time.time() * 1000)}


class Service(Catalog):
    DEVICE_SELECTION_TIMEOUT = 20
    DEVICE_SELECTION_POLL = 0.75
    AUTO_SELECT_TIMEOUT = 120
    AUTO_SELECT_POLL = 0.5
    AUTO_SELECT_SETTLE = 1
    AUTO_SELECT_RETRIES = (0.5, 1, 2, 4, 8)

    def __init__(self, spotify, player, mixer=None):
        self.spotify, self.player = spotify, player
        self.mixer = mixer
        self.audio_lock = asyncio.Lock()
        self.cache = OrderedDict()
        self.inflight = {}
        self.generation = 0
        self.command_lock = asyncio.Lock()
        self.accepted_playback = None
        self.balance_target = None
        self.balance_error = None
        self.balance_retry_at = 0
        self.balance_transfer = None
        self._quick_epoch = self.spotify.epoch
        self._quick_reads = {}
        self._quick_errors = {}
        self._quick_tasks = set()
        self._quick_balance_task = None
        self._quick_startup_until = None
        self._closed = False
        self._catalog_init()
        self._local_device_id = None
        self._local_blocked = False
        self._local_transfer_target = None
        self._local_pause_revision = None
        self._device_selection = None
        self._device_selection_task = None
        self._auto_select_task = None
        self._auto_select_generation = 0
        self._auto_select_error = None
        self._cloud_ready_at = None

    def start_auto_select(self):
        """One startup attempt, independent of whether the QAM has been opened."""
        if self._closed or not callable(getattr(self.player, "activate_local", None)):
            return
        if self._auto_select_task and not self._auto_select_task.done():
            return
        self._auto_select_generation += 1
        self._auto_select_error = None
        self._auto_select_task = asyncio.create_task(self._auto_select_local(self._auto_select_generation))

    async def cancel_auto_select(self):
        self._auto_select_generation += 1
        task, self._auto_select_task = self._auto_select_task, None
        self._auto_select_error = None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _auto_select_local(self, generation):
        deadline, failures = time.monotonic() + self.AUTO_SELECT_TIMEOUT, 0
        ready_since, active_since, activations = None, None, 0
        try:
            while not self._closed and generation == self._auto_select_generation:
                if time.monotonic() >= deadline:
                    self._auto_select_error = "The local player is still connecting. Start it again in Settings to retry."
                    return
                status = self.player.status()
                now = time.monotonic()
                active = status.get("active") or getattr(self.player, "local_active", False)
                local = self.player.local_snapshot()
                if active and local and local.get("status") == "playing" and activations == 0:
                    return  # Never interrupt music that the user already started.
                if status.get("ready"):
                    ready_since = now if ready_since is None else ready_since
                else:
                    ready_since, active_since = None, None
                if active and status.get("ready"):
                    active_since = now if active_since is None else active_since
                    if local is not None and now - active_since >= self.AUTO_SELECT_SETTLE:
                        return
                    await asyncio.sleep(self.AUTO_SELECT_POLL)
                    continue
                if active_since is not None:
                    # Soloist can briefly restore active=true while loading its
                    # saved session, then reset to inactive. Wait for that restore
                    # to settle before the first (or one corrective) activation.
                    active_since, ready_since = None, now
                epoch = self.spotify.epoch
                def allowed():
                    return (not self._closed and generation == self._auto_select_generation and
                            epoch == self.spotify.epoch and self.spotify.connected and self.spotify.pending is None)
                if not allowed():
                    await asyncio.sleep(self.AUTO_SELECT_POLL)
                    continue
                if not status.get("ready"):
                    recover = getattr(self.player, "recover_login", None)
                    if callable(recover) and status.get("wsConnected"):
                        # A paired engine can start before the network does and
                        # remain logged out. Prove connectivity independently
                        # before considering the runtime's bounded login recovery.
                        self._schedule_quick_read("profile", self._profile_load, 3600)
                        if self._cloud_ready_at is not None and time.monotonic() - self._cloud_ready_at < 30:
                            async with self.command_lock:
                                if allowed():
                                    await recover(allowed=allowed)
                    await asyncio.sleep(self.AUTO_SELECT_POLL)
                    continue
                if now - ready_since < self.AUTO_SELECT_SETTLE:
                    await asyncio.sleep(self.AUTO_SELECT_POLL)
                    continue
                if activations >= 2:
                    self._auto_select_error = "The local device did not stay active. Choose it again in Playback device."
                    return
                try:
                    async with self.command_lock:
                        if not allowed():
                            continue
                        await self.player.activate_local(allowed=allowed)
                        activations += 1
                        if not allowed():
                            return
                        # Activation selects the receiver, and never issues play.
                        # If Connect transferred an already-playing session, keep
                        # startup silent by pausing that owned session immediately.
                        local = self.player.local_snapshot()
                        if local and local.get("is_active") and local.get("status") == "playing":
                            await self.player.local_command("pause")
                        self._local_blocked = False
                        self._local_transfer_target = None
                        self.cache.pop("playback", None)
                        self._quick_errors.pop("playback", None)
                        active_since, ready_since = None, time.monotonic()
                except SpotifyError as error:
                    # Only a rejection before dispatch is safe to retry. An
                    # uncertain activation must not repeatedly take a receiver.
                    if error.code != "local_unavailable" or failures >= len(self.AUTO_SELECT_RETRIES):
                        self._auto_select_error = str(error)
                        return
                    delay = self.AUTO_SELECT_RETRIES[failures]
                    failures += 1
                    await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._auto_select_error = "The local playback device could not be selected. Start it again in Settings."
        finally:
            if self._auto_select_task is asyncio.current_task():
                self._auto_select_task = None
            if not self._closed and generation == self._auto_select_generation and not self._auto_select_error:
                state = self._owned_playback()
                if state:
                    self._schedule_quick_balance(state, (self.generation, self.spotify.epoch))

    def audio_status(self):
        saved = self.spotify.store.data
        balance = saved.get("audio_balance", 50)
        if type(balance) is not int or not 0 <= balance <= 100:
            balance = 50
        result = {"mode": "balance" if saved.get("audio_mode") == "balance" else "separate",
                "balance": balance, **(self.mixer.status() if self.mixer else {
                    "supported": False, "otherVolume": 100, "otherStreams": 0,
                    "error": "Local audio control is unavailable."})}
        if result["mode"] == "balance" and self.balance_error and not result["error"]:
            result["error"] = self.balance_error
        return result

    async def _balance_volume(self, state, value, remember=True):
        """A transaction and its rollback always address the same device."""
        active = state["device"]
        if state.get("source") == "soloist":
            await self.player.local_command("volume", value)
        else:
            await self.spotify.request("PUT", "/me/player/volume",
                                       {"device_id": active["id"], "volume_percent": value}, None)
        latest = self.cache.get("playback")
        if not remember or (latest and ((latest[1] or {}).get("device") or {}).get("id") != active["id"]):
            return
        self.invalidate_after_command("volume", value)
        if not self.accepted_playback:
            self.cache["playback"] = (time.monotonic() + 10,
                                      {**state, "device": {**active, "volume": value}})

    async def _apply_balance(self, state, balance, settings, persist=False, remember=True):
        active = (state or {}).get("device")
        if not active or active["restricted"]:
            raise SpotifyError("Choose an available playback device first.", "no_device")
        previous = active.get("volume")
        if not active["supportsVolume"] or type(previous) is not int or not 0 <= previous <= 100:
            raise SpotifyError("This device does not support remote volume control; balance cannot be applied.", "audio")
        # The centre preserves both sources at full volume. Each half of the
        # slider attenuates only the source on the opposite side.
        music_volume = min(100, 2 * balance)
        other_volume = min(100, 2 * (100 - balance))
        await self._balance_volume(state, music_volume, remember)
        try:
            await self.mixer.set_volume(other_volume)
            if persist:
                self.spotify.store.update(audio_mode="balance", audio_balance=balance)
        except Exception as error:
            rollback_failed = False
            try:
                await self.mixer.set_volume(settings["otherVolume"])
            except Exception:
                rollback_failed = True
            try:
                await self._balance_volume(state, previous, remember)
            except Exception:
                rollback_failed = True
            if rollback_failed:
                raise SpotifyError("The balance change failed and some levels could not be restored. Check both volume controls.", "audio") from None
            if isinstance(error, SpotifyError):
                raise
            raise SpotifyError("Cannot save the balance setting. The previous volumes were restored.", "audio") from None
        self.balance_target = (self.spotify.epoch, active["id"])
        self.balance_error, self.balance_retry_at = None, 0

    async def sync_balance(self, state, identity):
        if (state or {}).get("source") == "selection" or identity != (self.generation, self.spotify.epoch):
            return state
        active = (state or {}).get("device")
        target = (self.spotify.epoch, active["id"]) if active else None
        if self.balance_transfer and target == self.balance_transfer[:2]:
            self.balance_transfer = None
        if self.audio_status()["mode"] != "balance" or target == self.balance_target or time.monotonic() < self.balance_retry_at:
            return state
        async with self.audio_lock:
            async with self.command_lock:
                if identity != (self.generation, self.spotify.epoch):
                    return state
                settings = self.audio_status()
                if settings["mode"] != "balance" or target == self.balance_target:
                    return state
                pending = self.balance_transfer
                if pending and pending[0] == self.spotify.epoch and time.monotonic() < pending[2]:
                    if not active or active["id"] != pending[1]:
                        return state  # Spotify can briefly report the device we just left.
                    self.balance_transfer = None
                try:
                    if not self.mixer or not settings["supported"]:
                        raise SpotifyError("Local audio control is unavailable.", "audio")
                    await self._apply_balance(state, settings["balance"], settings)
                except Exception as error:
                    self.balance_error = str(error) if isinstance(error, SpotifyError) else "Cannot apply the saved audio balance."
                    self.balance_retry_at = time.monotonic() + 10
                cached = self.cache.get("playback")
                return cached[1] if cached else state

    async def audio(self, action, value):
        if action == "mode":
            if value not in ("separate", "balance"):
                raise SpotifyError("Invalid volume slider mode.", "audio")
        elif action not in ("other", "balance") or type(value) is not int or not 0 <= value <= 100:
            raise SpotifyError("Choose a volume from 0 to 100.", "audio")
        async with self.audio_lock:
            settings = self.audio_status()
            if action == "other":
                if settings["mode"] != "separate":
                    raise SpotifyError("Use the balance slider in Balance mode.", "audio")
                if not settings["supported"] or not self.mixer:
                    raise SpotifyError("Local audio control is unavailable.", "audio")
                await self.mixer.set_volume(value)
                return
            async with self.command_lock:
                if action == "mode" and value == "separate":
                    self.spotify.store.update(audio_mode="separate")
                    self.balance_target, self.balance_error = None, None
                    return
                if action == "balance" and settings["mode"] != "balance":
                    raise SpotifyError("Select Balance mode first.", "audio")
                if not settings["supported"] or not self.mixer:
                    raise SpotifyError("Local audio control is unavailable.", "audio")
                balance = settings["balance"] if action == "mode" else value
                old = self.cache.get("playback")
                state = old[1] if old else await self._remote_playback()
                self.cache["playback"] = (time.monotonic() + 10, state)
                await self._apply_balance(state, balance, settings, persist=True)

    def invalidate(self):
        self.generation += 1
        self.cache.clear()
        self.accepted_playback = None

    def accept_transport(self, state, playing, started_at):
        """Keep an acknowledged action usable while Spotify's read API catches up."""
        now = int(time.time() * 1000)
        progress = state.get("progress", 0)
        if state.get("playing"):
            progress += max(0, now - state.get("updatedAt", now))
        duration = (state.get("track") or {}).get("duration", 0)
        disallows = dict(state.get("disallows") or {})
        disallows.pop("pausing" if playing else "resuming", None)
        accepted = {**state, "playing": playing,
                    "progress": min(progress, duration) if duration else progress,
                    "updatedAt": now, "disallows": disallows}
        self.generation += 1
        # Pausing does not change the profile, library, queue or available devices.
        self.cache["playback"] = (time.monotonic() + 10, accepted)
        self.accepted_playback = {"state": accepted, "until": time.monotonic() + 30,
                                  "generation": self.generation, "epoch": self.spotify.epoch,
                                  "started_at": started_at}

    def invalidate_after_command(self, command, value):
        pending = self.accepted_playback
        self.invalidate()
        if not pending or command not in ("volume", "shuffle", "repeat", "seek", "save", "unsave", "queue", "library"):
            return
        if pending["until"] <= time.monotonic() or pending["epoch"] != self.spotify.epoch:
            return
        state = {**pending["state"]}
        if command == "volume":
            state["device"] = {**state["device"], "volume": int(value)}
        elif command == "seek":
            state.update(progress=int(value), updatedAt=int(time.time() * 1000))
        elif command in ("shuffle", "repeat"):
            state[command] = value
        # Unrelated successful commands cannot resurrect an older playing state.
        self.accepted_playback = {**pending, "state": state, "generation": self.generation}
        self.cache["playback"] = (min(time.monotonic() + 10, pending["until"]), state)

    def reconcile_playback(self, state, generation):
        pending = self.accepted_playback
        if not pending:
            return state
        if pending["epoch"] != self.spotify.epoch or time.monotonic() >= pending["until"]:
            self.accepted_playback = None
            return state
        accepted = pending["state"]
        if state:
            same_device = (state.get("device") or {}).get("id") == (accepted.get("device") or {}).get("id")
            same_track = (state.get("track") or {}).get("uri") == (accepted.get("track") or {}).get("uri")
            # An old request cannot confirm or cancel a newer accepted command.
            if generation == pending["generation"]:
                if not same_device or not same_track or (state.get("device") or {}).get("restricted"):
                    self.accepted_playback = None
                    return state
                timestamp = state.get("sourceTimestamp")
                # The same playing flag may belong to an earlier pause/resume cycle.
                # Missing/skewed provider time uses the bounded grace instead.
                fresh = isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and timestamp >= pending["started_at"]
                if state["playing"] == accepted["playing"] and fresh:
                    self.accepted_playback = None
                    return state
        return accepted

    async def cached(self, key, loader, ttl=60):
        old = self.cache.get(key)
        if old and old[0] > time.monotonic():
            return old[1]
        identity = (self.generation, self.spotify.epoch, key)
        if identity in self.inflight:
            return await asyncio.shield(self.inflight[identity])
        task = asyncio.create_task(loader())
        self.inflight[identity] = task
        def retire(completed):
            if self.inflight.get(identity) is completed:
                self.inflight.pop(identity, None)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(retire)
        try:
            result = await asyncio.shield(task)
            if identity[:2] == (self.generation, self.spotify.epoch):
                expires = time.monotonic() + ttl
                if key == "playback" and self.accepted_playback and result is self.accepted_playback["state"]:
                    expires = min(expires, self.accepted_playback["until"])
                self.cache[key] = (expires, result)
                while len(self.cache) > 64:
                    self.cache.popitem(last=False)
            return result
        finally:
            if task.done() and self.inflight.get(identity) is task:
                self.inflight.pop(identity, None)

    def _local_snapshot(self):
        player = {**self.player.status(), "selecting": self._auto_select_task is not None and not self._auto_select_task.done(),
                  "selectionError": self._auto_select_error}
        return {"connected": self.spotify.connected, "connecting": self.spotify.pending is not None,
                "authError": self.spotify.auth_error, "clientId": self.spotify.store.data.get("client_id", ""),
                "profile": None, "playback": None, "playbackError": None, "player": player,
                "audio": self.audio_status(), "deviceSelection": self.device_selection_status(), **self.feature_snapshot()}

    def device_selection_status(self):
        selection = self._device_selection
        if selection and (selection["epoch"] != self.spotify.epoch or
                          (not selection["pending"] and not selection.get("waitingForTrack") and time.monotonic() > selection["until"])):
            self._device_selection = None
            selection = None
        return ({"device": selection["device"], "pending": selection["pending"], "error": selection["error"],
                 "waitingForTrack": bool(selection.get("waitingForTrack"))}
                if selection else None)

    def _selected_empty_playback(self):
        selection = self.device_selection_status()
        if not selection or not selection.get("waitingForTrack"):
            return None
        # An empty Connect session cannot always be transferred. Keep the
        # acknowledged destination for the next explicit play, without claiming
        # that it is already the active receiver or starting music implicitly.
        return {"device": {**selection["device"], "active": False}, "track": None,
                "playing": False, "progress": 0, "shuffle": False, "repeat": "off",
                "disallows": {}, "source": "selection", "updatedAt": int(time.time() * 1000)}

    async def cancel_device_selection(self):
        self._device_selection = None
        task, self._device_selection_task = self._device_selection_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _selection_current(self, selection):
        return not self._closed and self._device_selection is selection and selection["epoch"] == self.spotify.epoch

    def _confirm_device_selection(self, selection, state):
        active = (state or {}).get("device") or {}
        if not self._selection_current(selection) or not active.get("active") or active.get("id") != selection["device"]["id"]:
            return False
        selection.update(pending=False, waitingForTrack=False, error=None, until=time.monotonic() + 15)
        self.cache["playback"] = (time.monotonic() + 10, state)
        self._quick_errors.pop("playback", None)
        self._schedule_quick_balance(state, (self.generation, self.spotify.epoch))
        return True

    async def _wait_device_selection(self, selection):
        """Confirm an acknowledged transfer with reads; never resend the transfer."""
        deadline = time.monotonic() + self.DEVICE_SELECTION_TIMEOUT
        try:
            while self._selection_current(selection) and selection["pending"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                cooldown = max(0, getattr(self.spotify, "backoff_until", 0) - time.monotonic())
                if cooldown:
                    await asyncio.sleep(min(cooldown, remaining))
                    continue
                try:
                    state = await asyncio.wait_for(self._remote_playback(), remaining)
                    if self._selection_current(selection) and self._confirm_device_selection(selection, state):
                        self._owned_playback(state, fresh=True)
                        return
                except SpotifyError as error:
                    if error.code in ("auth", "restricted", "tls"):
                        selection["error"] = str(error)
                        break
                await asyncio.sleep(min(self.DEVICE_SELECTION_POLL, max(0, deadline - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if self._selection_current(selection) and selection["pending"]:
                selection.update(pending=False, until=time.monotonic() + 30,
                                 error=selection["error"] or "Spotify has not confirmed this device yet. Refresh the device list before retrying.")
                self.cache.pop("playback", None)
                self.cache.pop("devices", None)

    async def _select_device(self, value):
        if not isinstance(value, str) or (value != LOCAL_DEVICE and not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value)):
            raise SpotifyError("Invalid playback device.")
        selected = next((item for item in await self.devices() if item["id"] == value), None)
        if not selected or selected["restricted"]:
            raise SpotifyError("That device is unavailable. Refresh the device list.")
        current = self._selected_empty_playback() or self._owned_playback()
        if current is None:
            cached = self.cache.get("playback")
            current = cached[1] if cached and cached[0] > time.monotonic() else None
        empty = current is not None and not current.get("track") and not current.get("playing")
        await self.cancel_device_selection()
        epoch = self.spotify.epoch
        owned_id = self.player.owned_device_id() if callable(getattr(self.player, "owned_device_id", None)) else None
        local = value in (owned_id, LOCAL_DEVICE) and callable(getattr(self.player, "activate_local", None))
        if local:
            await self.player.activate_local(allowed=lambda: not self._closed and epoch == self.spotify.epoch)
        else:
            await self.spotify.request("PUT", "/me/player", {}, {"device_ids": [value], "play": False})
        self._check_epoch(epoch)
        self.invalidate_after_command("transfer", value)
        self._local_blocked = not local
        self._local_transfer_target = None if local else (epoch, self.generation, value)
        if local:
            self._local_device_id = owned_id or LOCAL_DEVICE
        selection = {"device": selected, "pending": True, "error": None, "epoch": epoch, "until": time.monotonic() + 20}
        self._device_selection = selection
        if self.audio_status()["mode"] == "balance":
            self.balance_transfer = (epoch, value, time.monotonic() + 30)
        if local:
            state = self._owned_playback()
            if self._confirm_device_selection(selection, state):
                return
        elif empty:
            selection.update(pending=False, waitingForTrack=True)
            self.cache["playback"] = (time.monotonic() + 3, self._selected_empty_playback())
            return
        self._device_selection_task = asyncio.create_task(self._wait_device_selection(selection))

    def _owned_playback(self, remote=None, fresh=False):
        if self._selected_empty_playback():
            return None
        getter = getattr(self.player, "local_snapshot", None)
        data = getter() if getter else None
        if not data or not data.get("is_active"):
            self._local_device_id = None
            self._local_blocked = False
            self._local_transfer_target = None
            self._local_pause_revision = None
            cached = self.cache.get("playback")
            if cached and (cached[1] or {}).get("source") == "soloist":
                self.cache.pop("playback", None)
            return None
        if remote is None:
            cached = self.cache.get("playback")
            remote = cached[1] if cached else None
        # An owned active WebSocket is the authority. Associate its Connect ID
        # only when API track and device both match, never merely by name.
        active = (remote or {}).get("device") or {}
        local_uri = (data.get("item") or {}).get("uri")
        matches = (active.get("id") not in (None, LOCAL_DEVICE) and active.get("active") is True and
                   active.get("name") == data.get("device_name") and local_uri and
                   ((remote or {}).get("track") or {}).get("uri") == local_uri)
        if self._local_blocked:
            target = self._local_transfer_target
            # The first explicit transfer after startup has no local Connect ID
            # yet. Only an API read begun after that transfer may establish it;
            # cached state or a late pre-transfer read must not release this guard.
            if not (fresh and matches and target and target[0] == self.spotify.epoch and
                    target[1] <= self.generation and target[2] == active.get("id")):
                return None
            self._local_blocked = False
            self._local_transfer_target = None
        if matches:
            self._local_device_id = active.get("id")
        state = local_playback(data, self._local_device_id or LOCAL_DEVICE)
        pending = self.accepted_playback
        if (pending and pending["epoch"] == self.spotify.epoch and pending["until"] > time.monotonic() and
                self._local_pause_revision == data.get("revision") and pending["state"].get("source") == "soloist"):
            return pending["state"]
        if pending and pending["state"].get("source") == "soloist":
            self.accepted_playback = None
        return state

    async def _profile_load(self):
        epoch = self.spotify.epoch
        data = await self.spotify.request("GET", "/me")
        self._check_epoch(epoch)
        self._cloud_ready_at = time.monotonic()
        saved = self.spotify.store.data.get("catalog_account_id")
        if data.get("id") and data["id"] != saved:
            if saved:
                await self.clear_personal_data()
            self.spotify.store.update(catalog_account_id=data["id"])
        return {"id": data.get("id"), "name": data.get("display_name") or "Spotify listener", "image": image(data)}

    async def _remote_playback(self):
        return playback(await self.spotify.request("GET", "/me/player", {"additional_types": "track,episode"}))

    async def _playback_load(self):
        generation = self.generation
        epoch = self.spotify.epoch
        state = await self._remote_playback()
        selection = self._device_selection
        if selection and selection.get("waitingForTrack") and self._selection_current(selection):
            if (generation, epoch) != (self.generation, self.spotify.epoch):
                return None
            if not self._confirm_device_selection(selection, state):
                if not state or not state.get("track"):
                    return self._selected_empty_playback()
                # A new real session on another receiver supersedes an idle choice.
                self._device_selection = None
                self._local_blocked = False
                self._local_transfer_target = None
        if selection and selection["pending"] and self._selection_current(selection):
            if (generation, epoch) != (self.generation, self.spotify.epoch):
                return None
            if not self._confirm_device_selection(selection, state):
                return None
        if epoch == self.spotify.epoch:
            self._cloud_ready_at = time.monotonic()
        return self._owned_playback(state, fresh=(generation, epoch) == (self.generation, self.spotify.epoch)) or self.reconcile_playback(state, generation)

    def _quick_current(self, identity):
        return not self._closed and identity[:2] == (self.generation, self.spotify.epoch)

    def _quick_startup_active(self):
        return (self._quick_startup_until is not None and time.monotonic() < self._quick_startup_until and
                self.player.status().get("running", False))

    def _shorten_startup_empty_cache(self):
        state = self.cache.get("playback")
        if state and state[1] is None and self._quick_startup_active():
            self.cache["playback"] = (min(state[0], time.monotonic() + 2, self._quick_startup_until), None)

    def _track_quick_task(self, task):
        self._quick_tasks.add(task)

        def finished(completed):
            self._quick_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    async def _quick_read(self, key, identity, loader, ttl):
        try:
            result = await self.cached(key, loader, ttl)
            if self._quick_current(identity):
                self._quick_errors.pop(key, None)
                if key == "playback":
                    self._shorten_startup_empty_cache()
                    self._schedule_quick_balance(result, identity[:2])
        except Exception as error:
            if self._quick_current(identity):
                # Keep failed reads from multiplying while the network starts.
                # Spotify's own rate-limit deadline may require a longer pause.
                code = getattr(error, "code", "")
                if code == "rate_limit":
                    delay = max(1, getattr(error, "retry_after", 0))
                else:
                    delay = max(15 if code in ("auth", "restricted", "tls", "setup", "not_found") else 3,
                                getattr(error, "retry_after", 0))
                self._quick_errors[key] = (identity[:2], error, time.monotonic() + delay)
        finally:
            if self._quick_reads.get(identity) is asyncio.current_task():
                self._quick_reads.pop(identity, None)

    def _schedule_quick_read(self, key, loader, ttl):
        identity = (self.generation, self.spotify.epoch, key)
        old = self.cache.get(key)
        error = self._quick_errors.get(key)
        now = time.monotonic()
        if (self._closed or (old and old[0] > now) or identity in self._quick_reads or
                (error and error[0] == identity[:2] and error[2] > now) or
                getattr(self.spotify, "backoff_until", 0) > now):
            return
        self._quick_reads[identity] = self._track_quick_task(
            asyncio.create_task(self._quick_read(key, identity, loader, ttl)))

    def _schedule_quick_balance(self, state, identity):
        active = (state or {}).get("device")
        if (self._auto_select_task and not self._auto_select_task.done() and
                (state or {}).get("source") != "soloist"):
            return  # Startup is still moving away from the previous receiver.
        if ((state or {}).get("source") == "selection" or not self._quick_current(identity) or not active or self.audio_status()["mode"] != "balance" or
                self.balance_target == (self.spotify.epoch, active["id"]) or
                time.monotonic() < self.balance_retry_at or
                (self._quick_balance_task and not self._quick_balance_task.done())):
            return
        # sync_balance retains the command/audio locks and account/device guards.
        # Its remote write must not hold up the panel's first status response.
        self._quick_balance_task = self._track_quick_task(
            asyncio.create_task(self.sync_balance(state, identity)))

    async def quick_snapshot(self):
        """Return local state immediately; keep independent remote reads owned."""
        if self._quick_epoch != self.spotify.epoch:
            self.invalidate()
            self._quick_errors.clear()
            self._quick_epoch = self.spotify.epoch
            self._quick_startup_until = None
        result = self._local_snapshot()
        result.update(refreshing=False, playbackPending=False, retryAfter=0)
        if not result["connected"] or self._closed:
            return result
        if (result.get("deviceSelection") or {}).get("pending"):
            profile = self.cache.get("profile")
            result.update(playbackPending=True, refreshing=True, profile=profile[1] if profile else None)
            return result
        if self._quick_startup_until is None and result["player"].get("running", False):
            # Soloist may register with Connect shortly after its process starts.
            # A brief negative-cache window discovers it without choosing a device.
            self._quick_startup_until = time.monotonic() + 20
        self._shorten_startup_empty_cache()
        self._schedule_quick_read("profile", self._profile_load, 3600)
        self._schedule_quick_read("playback", self._playback_load, 10)
        profile, state = self.cache.get("profile"), self.cache.get("playback")
        if profile:
            result["profile"] = profile[1]
        if state:
            result["playback"] = state[1]
            self._schedule_quick_balance(state[1], (self.generation, self.spotify.epoch))
        local = self._owned_playback(result["playback"])
        if local:
            result["playback"] = local
            # Keep cloud discovery due until its Connect ID has been bound;
            # unsupported local commands must never target an arbitrary device.
            expiry = time.monotonic() + (10 if self._local_device_id else 2)
            if not self._local_device_id and state:
                expiry = min(expiry, state[0])
            self.cache["playback"] = (expiry, local)
            self._schedule_quick_balance(local, (self.generation, self.spotify.epoch))
        identity = (self.generation, self.spotify.epoch)
        pending = (*identity, "playback") in self._quick_reads
        result["playbackPending"] = pending or state is None
        # A missing/slow profile never forces fast polling once playback (even
        # an explicit no-active-device response) has settled.
        result["refreshing"] = pending or bool(self._quick_balance_task and not self._quick_balance_task.done())
        retry_at = getattr(self.spotify, "backoff_until", 0)
        if state and state[1] is None and self._quick_startup_active():
            retry_at = max(retry_at, state[0])
        failure = self._quick_errors.get("playback")
        if failure and failure[0] == identity:
            error = failure[1]
            result["playbackError"] = str(error) if isinstance(error, SpotifyError) else "Cannot read playback right now."
            retry_at = max(retry_at, failure[2])
        result["retryAfter"] = max(0, math.ceil(retry_at - time.monotonic()))
        if local:
            result.update(playbackPending=False, refreshing=False, playbackError=None, retryAfter=0)
        elif (result["playback"] or {}).get("source") == "soloist":
            result.update(playback=None, playbackPending=True, refreshing=True)
            self._schedule_quick_read("playback", self._playback_load, 10)
        return result

    async def snapshot(self):
        identity = (self.generation, self.spotify.epoch)
        result = self._local_snapshot()
        if not self.spotify.connected:
            return result
        # Independent endpoints: a profile error must not break playback or vice versa.
        profile, state = await asyncio.gather(self.cached("profile", self._profile_load, 3600), self.cached("playback", self._playback_load, 10), return_exceptions=True)
        if not isinstance(profile, Exception):
            result["profile"] = profile
        if not isinstance(state, Exception):
            result["playback"] = await self.sync_balance(state, identity)
        else:
            result["playbackError"] = str(state) if isinstance(state, SpotifyError) else "Cannot read playback right now."
        result["connected"] = self.spotify.connected
        result["audio"] = self.audio_status()
        local = self._owned_playback(result["playback"])
        if local:
            result["playback"], result["playbackError"] = local, None
        return result

    async def devices(self):
        async def load():
            data = await self.spotify.request("GET", "/me/player/devices")
            return [item for entry in data.get("devices", []) if (item := device(entry))]
        own = self.player.owned_device_id() if callable(getattr(self.player, "owned_device_id", None)) else None
        local_ready = callable(getattr(self.player, "activate_local", None)) and self.player.status().get("ready")
        try:
            values = [dict(item) for item in await self.cached("devices", load, 3)]
        except SpotifyError:
            if not local_ready:
                raise
            values = []
        if local_ready:
            local_id = own or LOCAL_DEVICE
            found = next((item for item in values if item["id"] == local_id), None)
            active = bool(getattr(self.player, "local_active", False)) and not self._local_blocked
            if found:
                found["active"] = active
            else:
                values.insert(0, {"id": local_id, "name": "SpotiDeck", "type": "Computer", "active": active,
                                  "restricted": False, "volume": None, "supportsVolume": True})
        selection = self.device_selection_status()
        if selection and not selection["pending"] and not selection["error"]:
            for item in values:
                item["active"] = item["id"] == selection["device"]["id"]
        return values

    async def queue(self):
        getter = getattr(self.player, "local_queue", None)
        if getter and self._owned_playback():
            from .local_state import local_media
            queue = getter()
            if queue is not None:
                return [item for entry in queue if isinstance(entry, dict) and (item := local_media(entry.get("item")))]
        async def load():
            data = await self.spotify.request("GET", "/me/player/queue")
            return [item for entry in data.get("queue", []) if (item := media(entry))]
        return await self.cached("queue", load, 8)

    async def command(self, command, value=None):
        epoch = self.spotify.epoch
        if command == "transfer":
            await self.cancel_auto_select()
            async with self.audio_lock:
                async with self.command_lock:
                    self._check_epoch(epoch)
                    await self._select_device(value)
            return
        async with self.command_lock:
            self._check_epoch(epoch)
            if command not in ("save", "unsave") and (self.device_selection_status() or {}).get("pending"):
                raise SpotifyError("The playback device is connecting. Please wait for confirmation.", "pending")
            if command == "volume" and self.audio_status()["mode"] == "balance":
                raise SpotifyError("Use the balance slider in Balance mode.", "audio")
            await self._command(command, value)

    async def _command(self, command, value=None):
        epoch = self.spotify.epoch
        method, path, params, body = "PUT", "/me/player/", {}, None
        if command in ("save", "unsave"):
            path, params = "/me/library", {"uris": uri_value(value)}
            method = "PUT" if command == "save" else "DELETE"
        else:
            # Bind each action to the device displayed by the last confirmed snapshot.
            old = self.cache.get("playback")
            state = self._selected_empty_playback() or self._owned_playback() or (old[1] if old else await self._remote_playback())
            active = state.get("device") if state else None
            if not active or active["restricted"]:
                raise SpotifyError("Choose an available playback device first.", "no_device")
            if active["id"] != LOCAL_DEVICE:
                params["device_id"] = active["id"]
            if command in ("pause", "resume", "next", "previous"):
                path += {"pause": "pause", "resume": "play", "next": "next", "previous": "previous"}[command]
                method = "POST" if command in ("next", "previous") else "PUT"
            elif command == "play":
                path += "play"
                body = await self._play_body(value)
            elif command == "queue":
                method, path, params["uri"] = "POST", path + "queue", uri_value(value, "track|episode")
            elif command in ("volume", "seek"):
                max_value = 100 if command == "volume" else (state.get("track") or {}).get("duration", 0)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= max_value:
                    raise SpotifyError("Invalid playback value.")
                if command == "volume" and not active["supportsVolume"]:
                    raise SpotifyError("This device does not support remote volume control.")
                path += command
                params["volume_percent" if command == "volume" else "position_ms"] = int(value)
            elif command == "shuffle" and isinstance(value, bool):
                path += "shuffle"
                params["state"] = str(value).lower()
            elif command == "repeat" and value in ("off", "context", "track"):
                path += "repeat"
                params["state"] = value
            else:
                raise SpotifyError("Unsupported playback command.")
        started_at = int(time.time() * 1000)
        self._check_epoch(epoch)
        used_local = False
        if command not in ("transfer", "save", "unsave") and state.get("source") == "soloist":
            local_command, local_value = ("play", None) if command == "resume" else (command, value)
            try:
                await self.player.local_command(local_command, local_value)
                used_local = True
            except SpotifyError as error:
                if error.code != "local_unavailable":
                    raise  # Never replay an unconfirmed local write through the cloud.
                if active["id"] == LOCAL_DEVICE:
                    if not getattr(self.player, "local_active", False):
                        raise
                    remote = await self._remote_playback()
                    mapped = self._owned_playback(remote)
                    mapped_id = ((mapped or {}).get("device") or {}).get("id")
                    if mapped_id in (None, LOCAL_DEVICE):
                        raise SpotifyError("Spotify has not identified the local playback device yet. Refresh playback and retry.", "no_device")
                    params["device_id"] = mapped_id
        if not used_local:
            self._check_epoch(epoch)
            await self.spotify.request(method, path, params, body)
        if command in ("pause", "resume"):
            self.accept_transport(state, command == "resume", started_at)
            self._local_pause_revision = state.get("revision") if used_local else None
        else:
            self.invalidate_after_command(command, value)
            if command in ("volume", "seek") and not self.accepted_playback:
                accepted = ({**state, "device": {**active, "volume": int(value)}} if command == "volume" else
                            {**state, "progress": int(value), "updatedAt": int(time.time() * 1000)})
                self.cache["playback"] = (time.monotonic() + 10, accepted)
        selection = self._device_selection
        if command in ("play", "resume") and selection and selection.get("waitingForTrack") and self._selection_current(selection):
            selection.update(waitingForTrack=False, pending=True)
            self._device_selection_task = asyncio.create_task(self._wait_device_selection(selection))

    async def close(self):
        self._closed = True
        await self.cancel_device_selection()
        await self.cancel_auto_select()
        await self._catalog_close()
        # Capture both cache loaders and their owners before cancellation: cached
        # deliberately shields its loader, and drops its entry when its owner exits.
        tasks = set(self.inflight.values()) | self._quick_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._quick_reads.clear()
        self._quick_tasks.clear()
        try:
            if self.mixer:
                await self.mixer.close()
        finally:
            self.invalidate()
            tasks = list(self.inflight.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.inflight.clear()
