"""Private, bounded stdlib WebSocket client for the official Soloist API."""

import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import re
import struct
import time

from .spotify import SpotifyError

MAX_MESSAGE = 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class SocketError(Exception):
    pass


class LocalSocket:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.write_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, port, timeout=3):
        if type(port) is not int or not 1 <= port <= 65535:
            raise SocketError("Invalid local endpoint")
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port, limit=16384), timeout)
        connection = cls(reader, writer)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            writer.write((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
            await asyncio.wait_for(writer.drain(), timeout)
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            lines = response.decode("ascii").split("\r\n")
            headers = {}
            for line in lines[1:]:
                if line:
                    name, value = line.split(":", 1)
                    name = name.strip().lower()
                    if name in headers:
                        raise SocketError("Duplicate upgrade header")
                    headers[name] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
            if (lines[0].split()[:2] != ["HTTP/1.1", "101"]
                    or headers.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in [part.strip().lower() for part in headers.get("connection", "").split(",")]
                    or headers.get("sec-websocket-accept") != expected
                    or "sec-websocket-extensions" in headers):
                raise SocketError("Invalid WebSocket upgrade")
            return connection
        except BaseException:
            await connection.close()
            raise

    async def frame(self, opcode, payload=b""):
        if len(payload) > MAX_MESSAGE:
            raise SocketError("Message too large")
        mask = os.urandom(4)
        size = len(payload)
        prefix = bytes((0x80 | opcode, 0x80 | size)) if size < 126 else (
            bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", size) if size < 65536 else
            bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", size))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        async with self.write_lock:
            self.writer.write(prefix + mask + masked)
            await asyncio.wait_for(self.writer.drain(), 3)

    async def send(self, payload):
        await self.frame(1, json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"))

    async def receive(self):
        fragments, size, started = [], 0, False
        while True:
            first, second = await self.reader.readexactly(2)
            opcode, final = first & 15, bool(first & 128)
            if first & 0x70 or second & 128:
                raise SocketError("Invalid server frame")
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if length > MAX_MESSAGE or size + length > MAX_MESSAGE:
                raise SocketError("Message too large")
            if opcode >= 8 and (not final or length > 125):
                raise SocketError("Invalid control frame")
            payload = await self.reader.readexactly(length)
            if opcode == 8:
                raise SocketError("Local player closed the connection")
            if opcode == 9:
                await self.frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not started:
                started = True
            elif opcode != 0 or not started:
                raise SocketError("Expected a JSON text frame")
            fragments.append(payload)
            size += length
            if final:
                result = json.loads(b"".join(fragments).decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(result, dict):
                    raise SocketError("Expected a JSON object")
                return result

    async def close(self):
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 1)
        except (OSError, asyncio.TimeoutError):
            pass


def number(value, fallback=0):
    return value if type(value) in (int, float) and math.isfinite(value) else fallback


class SoloistClient:
    """One client belongs to one child process, never to a saved/stale endpoint."""

    RETRIES = (0.2, 0.5, 1, 2, 4, 8)
    HEALTH_INTERVAL = 10
    HEALTH_TIMEOUT = 5
    COMMAND_TIMEOUT = 3

    def __init__(self, endpoint, alive):
        self.endpoint, self.alive = endpoint, alive
        self.socket = None
        self.connected = False
        self.logged_in = False
        self.active = False
        self.device_name = "SpotiDeck"
        self.state = None
        self.queue = None
        self.revision = 0
        self.updated = 0
        self.last_event = 0
        self.last_state = 0
        self.pending = None
        self.state_changed = asyncio.Event()
        self.command_lock = asyncio.Lock()
        self.task = None
        self.closed = False
        self.error = None
        self.on_login = None

    def start(self):
        self.task = asyncio.create_task(self.run())

    def snapshot(self):
        if not self.connected or not self.logged_in or self.state is None:
            return None
        now = time.time() * 1000
        if self.updated and now - self.updated > max(30, 2 * (self.HEALTH_INTERVAL + self.HEALTH_TIMEOUT)) * 1000:
            # Linux monotonic timers can pause during suspend. A QAM read may arrive
            # before the next health tick; never expose the pre-suspend state as live.
            self.connected, self.active = False, False
            if self.socket:
                self.socket.writer.close()
            return None
        result = copy.deepcopy(self.state)
        position = result.get("position") or {}
        progress = number(position.get("position_ms"))
        if result.get("status") == "playing":
            progress += max(0, now - number(position.get("timestamp_ms"), now)) * max(0, number(position.get("speed"), 1))
        decorations = (result.get("item") or {}).get("decorations") or {}
        duration = number((decorations.get("playback") or {}).get("duration_ms"))
        result["position"] = {"position_ms": int(max(0, min(duration, progress) if duration else progress)),
                              "timestamp_ms": int(now), "speed": number(position.get("speed"), 1) if result.get("status") == "playing" else 0}
        result.update(is_active=self.active, device_name=self.device_name, received_at=self.updated, revision=self.revision)
        return result

    def event(self, event):
        kind = event.get("type")
        self.last_event = time.monotonic()
        if kind in ("command_result", "error"):
            if self.pending is not None:
                command, future = self.pending
                if not future.done() and (kind == "error" or event.get("command") == command):
                    if kind == "error":
                        future.set_exception(SpotifyError("The local player rejected this command. Refresh playback and retry.", "player"))
                    else:
                        future.set_result({"accepted": True})
            return
        if kind == "auth_state":
            self.logged_in = event.get("logged_in") is True
            self.active = self.logged_in and event.get("is_active") is True
            self.device_name = str(event.get("device_name") or "SpotiDeck")
            if not self.logged_in:
                self.state, self.queue = None, None
            elif self.on_login:
                self.on_login()
        elif kind == "playback_state":
            self.state = {key: copy.deepcopy(event[key]) for key in (
                "status", "item", "context", "position", "volume", "options", "available_actions") if key in event}
            self.active = self.logged_in and event.get("is_active") is True
            self.last_state = time.monotonic()
        elif kind == "device_changed":
            self.active = self.logged_in and event.get("is_active") is True
            self.device_name = str(event.get("device_name") or self.device_name)
        elif kind == "queue_changed":
            self.queue = {"previous": copy.deepcopy(event.get("previous", [])), "upcoming": copy.deepcopy(event.get("upcoming", []))}
        elif self.state is not None:
            if kind == "playback_changed":
                self.state["position"] = copy.deepcopy((self.snapshot() or self.state).get("position") or {
                    "position_ms": 0, "timestamp_ms": int(time.time() * 1000)})
                self.state["status"] = event.get("status", "idle")
                self.state["position"]["speed"] = 1 if event.get("status") == "playing" else 0
            else:
                key = {"track_changed": "item", "context_changed": "context", "volume_changed": "volume",
                       "options_changed": "options", "position_sync": "position"}.get(kind)
                if key and key in event:
                    self.state[key] = copy.deepcopy(event[key])
                    if kind == "track_changed":
                        self.state["position"] = {"position_ms": 0, "timestamp_ms": int(time.time() * 1000),
                                                  "speed": 1 if self.state.get("status") == "playing" else 0}
        self.revision += 1
        self.updated = int(time.time() * 1000)
        self.state_changed.set()

    async def request(self, command, fields=None, *, allow_inactive=False, allowed=None):
        async with self.command_lock:
            if allow_inactive and command != "activate":
                raise SpotifyError("Only device activation may target an inactive local player.", "invalid")
            if self.state is not None:
                self.snapshot()  # Reject stale authority immediately after suspend, even before a QAM refresh.
            socket = self.socket
            if not self.connected or not self.logged_in or socket is None or (not self.active and not allow_inactive):
                raise SpotifyError("The local player is not the active playback device.", "local_unavailable")
            if allowed is not None and not allowed():
                raise SpotifyError("Local device selection was cancelled.", "local_cancelled")
            future = asyncio.get_running_loop().create_future()
            self.pending = command, future
            try:
                await socket.send({"type": "command", "command": command, **(fields or {})})
                return await asyncio.wait_for(future, self.COMMAND_TIMEOUT)
            except asyncio.CancelledError:
                # Discard delayed acknowledgements before another command can reuse this socket.
                await socket.close()
                self.connected, self.active = False, False
                raise
            except (OSError, SocketError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                # An uncertain write must not be retried against another receiver.
                await socket.close()
                self.connected, self.active = False, False
                raise SpotifyError("The local command could not be confirmed. Playback will reconnect; please check its state.", "local_uncertain") from None
            finally:
                self.pending = None
                if not future.done():
                    future.cancel()

    async def activate(self, allowed=None):
        """Select the owned receiver without requesting playback; confirm real state."""
        if allowed is not None and not allowed():
            raise SpotifyError("Local device selection was cancelled.", "local_cancelled")
        current = self.snapshot()
        if current and current.get("is_active"):
            return {"accepted": True, "active": True}
        await self.request("activate", allow_inactive=True, allowed=allowed)
        socket = self.socket
        try:
            if socket is None or not self.connected:
                raise SocketError("Activation connection was lost")
            # Query commands report state only; neither query starts music.
            await socket.send({"type": "command", "command": "get_auth_state"})
            await socket.send({"type": "command", "command": "get_state"})
            deadline = time.monotonic() + self.COMMAND_TIMEOUT
            while self.connected and self.logged_in and not self.closed:
                self.state_changed.clear()
                current = self.snapshot()
                if current and current.get("is_active"):
                    return {"accepted": True, "active": True}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.wait_for(self.state_changed.wait(), remaining)
        except (OSError, SocketError, asyncio.TimeoutError):
            pass
        raise SpotifyError("Spotify accepted device selection but has not confirmed it yet. Check the playback device before retrying.", "local_uncertain")

    async def _health(self, socket):
        try:
            await self._health_loop(socket)
        except (OSError, SocketError, asyncio.TimeoutError):
            pass
        finally:
            await socket.close()

    async def _health_loop(self, socket):
        previous_wall, previous_mono = time.time(), time.monotonic()
        while self.socket is socket and not self.closed:
            await asyncio.sleep(self.HEALTH_INTERVAL)
            wall, mono = time.time(), time.monotonic()
            if wall - previous_wall > self.HEALTH_INTERVAL + 20 or abs((wall - previous_wall) - (mono - previous_mono)) > 20:
                await socket.close()
                return
            previous_wall, previous_mono = wall, mono
            # A roundtrip read catches stale sockets even when no track is playing.
            if self.pending is not None:
                continue
            sent = time.monotonic()
            await socket.send({"type": "command", "command": "get_auth_state"})
            if self.logged_in:
                await socket.send({"type": "command", "command": "get_state"})
            await asyncio.sleep(self.HEALTH_TIMEOUT)
            if self.last_event < sent:
                await socket.close()
                return
            previous_wall, previous_mono = time.time(), time.monotonic()

    async def run(self):
        failures = 0
        while not self.closed and self.alive() and failures < len(self.RETRIES):
            health, socket = None, None
            began = time.monotonic()
            try:
                port = self.endpoint()
                if port is None:
                    raise SocketError("Waiting for the local API")
                socket = await LocalSocket.connect(port)
                if self.closed or not self.alive():
                    return
                self.socket, self.connected = socket, True
                self.error = None
                health = asyncio.create_task(self._health(socket))
                while not self.closed and self.alive():
                    event = await socket.receive()
                    was_logged_in = self.logged_in
                    self.event(event)
                    if event.get("type") == "auth_state" and self.logged_in and not was_logged_in:
                        await socket.send({"type": "command", "command": "get_queue", "limit": 50})
            except asyncio.CancelledError:
                raise
            except (OSError, SocketError, ValueError, UnicodeError, RecursionError, asyncio.TimeoutError,
                    asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                self.error = "The local playback connection is unavailable."
            finally:
                self.connected, self.logged_in, self.active = False, False, False
                self.state, self.queue = None, None
                self.state_changed.set()
                if self.pending is not None and not self.pending[1].done():
                    self.pending[1].set_exception(SpotifyError("The local connection was interrupted. Check playback before retrying.", "local_uncertain"))
                if health:
                    health.cancel()
                    await asyncio.gather(health, return_exceptions=True)
                if socket:
                    await socket.close()
                self.socket = None
            if self.closed or not self.alive():
                return
            if time.monotonic() - began > 30:
                failures = 0
            await asyncio.sleep(self.RETRIES[failures])
            failures += 1

    async def close(self):
        self.closed = True
        self.connected, self.active = False, False
        self.state_changed.set()
        if self.socket:
            await self.socket.close()
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.state, self.queue = None, None


def command_messages(command, value=None):
    """Validate controls before anything can be dispatched to the player."""
    plain = {"pause": "pause", "next": "skip_next", "previous": "skip_prev"}
    if command in plain:
        return [(plain[command], {})]
    if command == "play":
        uri = value.get("uri") if isinstance(value, dict) else value
        if uri is None:
            return [("play", {})]
        if isinstance(value, dict) and any(key != "uri" for key in value):
            raise SpotifyError("Context playback requires the Spotify Web API.", "local_unavailable")
        if isinstance(uri, str) and re.fullmatch(r"spotify:(track|episode|album|playlist):[a-zA-Z0-9]{22}", uri):
            return [("play", {"uri": uri})]
    elif command == "queue" and isinstance(value, str) and re.fullmatch(r"spotify:track:[a-zA-Z0-9]{22}", value):
        return [("add_to_queue", {"uri": value})]
    elif command == "queue" and isinstance(value, str) and re.fullmatch(r"spotify:episode:[a-zA-Z0-9]{22}", value):
        raise SpotifyError("Episode queue changes require the Spotify Web API.", "local_unavailable")
    elif command in ("seek", "volume") and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= (100 if command == "volume" else 2 ** 31 - 1):
        return [("set_volume" if command == "volume" else "seek", {"volume" if command == "volume" else "position_ms": int(value)})]
    elif command == "shuffle" and type(value) is bool:
        return [("set_shuffle", {"enabled": value})]
    elif command == "repeat" and value in ("off", "context", "track"):
        if value == "track":
            return [("set_repeat_context", {"enabled": False}), ("set_repeat_track", {"enabled": True})]
        return [("set_repeat_track", {"enabled": False}), ("set_repeat_context", {"enabled": value == "context"})]
    raise SpotifyError("Invalid local playback command.", "invalid")
