import asyncio
import base64
import hashlib
import json
import struct
import time
import unittest
from unittest.mock import AsyncMock, patch

from backend.soloist import LocalSocket, MAX_MESSAGE, SocketError, SoloistClient, WS_GUID, command_messages
from backend.spotify import SpotifyError


URI = "spotify:track:" + "a" * 22


def wire(payload, opcode=1, final=True):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    size = len(payload)
    prefix = bytes(((128 if final else 0) | opcode, size)) if size < 126 else (
        bytes(((128 if final else 0) | opcode, 126)) + struct.pack("!H", size) if size < 65536 else
        bytes(((128 if final else 0) | opcode, 127)) + struct.pack("!Q", size))
    return prefix + payload


class Writer:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data):
        self.buffer.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def playback_event():
    return {"type": "playback_state", "status": "playing", "is_active": True,
            "position": {"position_ms": 1000, "timestamp_ms": int(time.time() * 1000), "speed": 1},
            "volume": 64, "options": {"shuffle": False, "repeat": "off"},
            "item": {"uri": URI, "entity_type": "track", "decorations": {
                "identity": {"name": "Example"}, "playback": {"duration_ms": 200000}}}}


class FramingTests(unittest.IsolatedAsyncioTestCase):
    def socket(self, data):
        reader, writer = asyncio.StreamReader(), Writer()
        reader.feed_data(data)
        reader.feed_eof()
        return LocalSocket(reader, writer)

    async def test_client_frames_are_masked_and_support_long_payloads(self):
        for length in (10, 300, 66000):
            connection = self.socket(b"")
            payload = {"value": "x" * length}
            await connection.send(payload)
            frame = connection.writer.buffer
            self.assertEqual(frame[0], 129)
            self.assertTrue(frame[1] & 128)
            offset = 2 + (2 if (frame[1] & 127) == 126 else 8 if (frame[1] & 127) == 127 else 0)
            mask = frame[offset:offset + 4]
            decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(frame[offset + 4:]))
            self.assertEqual(json.loads(decoded), payload)

    async def test_fragmented_text_with_interleaved_ping_is_reassembled(self):
        socket = self.socket(wire(b'{"type":', final=False) + wire(b"hello", opcode=9) + wire(b'"auth_state"}', opcode=0))
        self.assertEqual(await socket.receive(), {"type": "auth_state"})
        self.assertEqual(socket.writer.buffer[0], 0x8A)
        self.assertTrue(socket.writer.buffer[1] & 128)

    async def test_server_frames_extended_lengths_are_supported(self):
        for length in (500, 66000):
            payload = {"value": "x" * length}
            self.assertEqual(await self.socket(wire(payload)).receive(), payload)

    async def test_malformed_or_oversized_frames_are_rejected_before_body_read(self):
        invalid = [b"\x81\x80", b"\xc1\x00", b"\x89\x7e" + struct.pack("!H", 126),
                   b"\x81\x7f" + struct.pack("!Q", MAX_MESSAGE + 1), wire(b"x", opcode=2),
                   wire(b"x", opcode=0), wire(b"{}", opcode=9, final=False), wire(b"", opcode=8)]
        for data in invalid:
            with self.subTest(data=data[:8]), self.assertRaises(SocketError):
                await self.socket(data).receive()

    async def test_truncated_and_invalid_json_do_not_become_state(self):
        for data, error in ((b"\x81\x05{}", asyncio.IncompleteReadError), (wire(b"[]"), SocketError),
                            (wire(b'{"volume":NaN}'), ValueError), (wire(b"\xff"), UnicodeError)):
            with self.subTest(data=data), self.assertRaises(error):
                await self.socket(data).receive()


class StateTests(unittest.TestCase):
    def client(self):
        client = SoloistClient(lambda: None, lambda: True)
        client.connected = True
        client.event({"type": "auth_state", "logged_in": True, "is_active": True, "device_name": "SpotiDeck"})
        client.event(playback_event())
        return client

    def test_pause_event_freezes_progress_even_before_position_sync(self):
        with patch("backend.soloist.time.time", return_value=1000):
            client = self.client()
        with patch("backend.soloist.time.time", return_value=1002):
            client.event({"type": "playback_changed", "status": "paused"})
        with patch("backend.soloist.time.time", return_value=1020):
            state = client.snapshot()
        self.assertEqual(state["position"]["position_ms"], 3000)
        self.assertEqual(state["position"]["speed"], 0)
        self.assertEqual(state["status"], "paused")

    def test_ack_does_not_advance_revision_or_confirm_playback(self):
        client = self.client()
        revision = client.revision
        client.event({"type": "command_result", "command": "pause"})
        self.assertEqual(client.revision, revision)
        self.assertEqual(client.snapshot()["status"], "playing")

    def test_granular_updates_replace_only_their_fields_and_snapshot_is_detached(self):
        client = self.client()
        client.event({"type": "volume_changed", "volume": 25})
        client.event({"type": "options_changed", "options": {"shuffle": True, "repeat": "track"}})
        state = client.snapshot()
        self.assertEqual(state["volume"], 25)
        self.assertEqual(state["options"]["repeat"], "track")
        state["item"]["uri"] = "mutated"
        self.assertEqual(client.snapshot()["item"]["uri"], URI)

    def test_logout_and_disconnect_never_return_stale_local_state(self):
        client = self.client()
        client.event({"type": "auth_state", "logged_in": False, "is_active": True})
        self.assertIsNone(client.snapshot())
        self.assertFalse(client.active)
        client = self.client()
        client.connected = False
        self.assertIsNone(client.snapshot())

    def test_device_transfer_clears_active_without_hijacking(self):
        client = self.client()
        client.event({"type": "device_changed", "is_active": False})
        self.assertFalse(client.active)
        self.assertFalse(client.snapshot()["is_active"])

    def test_track_change_resets_old_track_position_until_new_anchor(self):
        client = self.client()
        client.event({"type": "track_changed", "item": {"uri": "spotify:track:" + "b" * 22}})
        self.assertLess(client.snapshot()["position"]["position_ms"], 10)

    def test_first_snapshot_after_suspend_invalidates_old_state_immediately(self):
        with patch("backend.soloist.time.time", return_value=1000):
            client = self.client()
        client.socket = type("Socket", (), {"writer": Writer()})()
        with patch("backend.soloist.time.time", return_value=1060):
            self.assertIsNone(client.snapshot())
        self.assertFalse(client.active)
        self.assertFalse(client.connected)
        self.assertTrue(client.socket.writer.closed)

    def test_command_validation_and_repeat_order(self):
        self.assertEqual(command_messages("repeat", "track"), [
            ("set_repeat_context", {"enabled": False}), ("set_repeat_track", {"enabled": True})])
        self.assertEqual(command_messages("repeat", "context")[-1], ("set_repeat_context", {"enabled": True}))
        self.assertEqual(command_messages("play", {"uri": URI}), [("play", {"uri": URI})])
        for command, value in (("volume", float("nan")), ("volume", True), ("seek", -1),
                               ("play", "https://example.com"), ("shuffle", 1), ("repeat", "all")):
            with self.subTest(command=command), self.assertRaises(SpotifyError):
                command_messages(command, value)
        with self.assertRaises(SpotifyError) as error:
            command_messages("play", {"uri": URI, "contextUri": "spotify:playlist:" + "b" * 22})
        self.assertEqual(error.exception.code, "local_unavailable")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writers, self.handlers, self.commands = [], set(), []
        self.ack = True
        self.correct_accept = True
        self.hold_handshake = False
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.client = None

    async def asyncTearDown(self):
        if self.client:
            await self.client.close()
        self.server.close()
        for writer in self.writers:
            writer.close()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        # Python 3.12+ also waits for accepted connections. Retire held
        # handshake handlers before awaiting the listener's final close.
        await asyncio.wait_for(self.server.wait_closed(), 2)

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.append(writer)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if self.hold_handshake:
                await asyncio.Event().wait()
            key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
            accept = base64.b64encode(hashlib.sha1(key + WS_GUID.encode()).digest()) if self.correct_accept else b"invalid"
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            writer.write(wire({"type": "auth_state", "logged_in": True, "is_active": True}))
            writer.write(wire(playback_event()))
            await writer.drain()
            while True:
                first, second = await reader.readexactly(2)
                length = second & 127
                if length == 126:
                    length = struct.unpack("!H", await reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", await reader.readexactly(8))[0]
                mask = await reader.readexactly(4)
                payload = await reader.readexactly(length)
                command = json.loads(bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
                self.commands.append(command)
                if self.ack:
                    writer.write(wire({"type": "command_result", "command": command["command"]}))
                    await writer.drain()
        except (asyncio.IncompleteReadError, OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    async def until(self, condition):
        async def waiting():
            while not condition():
                await asyncio.sleep(0.002)
        await asyncio.wait_for(waiting(), 2)

    async def connect(self):
        self.client = SoloistClient(lambda: self.port, lambda: True)
        self.client.RETRIES = (0.01, 0.01, 0.01)
        self.client.start()
        await self.until(lambda: self.client.snapshot() is not None)

    async def test_real_loopback_handshake_and_ack_does_not_fake_pause(self):
        await self.connect()
        result = await self.client.request("pause")
        self.assertTrue(result["accepted"])
        self.assertEqual(self.client.snapshot()["status"], "playing")
        self.writers[-1].write(wire({"type": "playback_changed", "status": "paused"}))
        await self.until(lambda: self.client.snapshot()["status"] == "paused")
        self.assertEqual(self.commands, [{"type": "command", "command": "get_queue", "limit": 50},
                                        {"type": "command", "command": "pause"}])

    async def test_dropped_socket_reconnects_with_fresh_state(self):
        await self.connect()
        self.writers[-1].close()
        await self.until(lambda: len(self.writers) == 2 and self.client.snapshot() is not None)
        self.assertTrue(self.client.active)

    async def test_inactive_local_device_never_receives_control(self):
        await self.connect()
        self.client.event({"type": "device_changed", "is_active": False})
        with self.assertRaises(SpotifyError) as error:
            await self.client.request("play")
        self.assertEqual(error.exception.code, "local_unavailable")
        self.assertFalse(any(command["command"] in ("play", "activate") for command in self.commands))

    async def test_timeout_is_uncertain_and_not_replayed(self):
        await self.connect()
        self.ack = False
        self.client.COMMAND_TIMEOUT = 0.02
        with self.assertRaises(SpotifyError) as error:
            await self.client.request("skip_next")
        self.assertEqual(error.exception.code, "local_uncertain")
        self.assertEqual(len([command for command in self.commands if command["command"] == "skip_next"]), 1)
        self.assertFalse(self.client.connected)

    async def test_close_stops_reconnect_and_disposes_state(self):
        await self.connect()
        await self.client.close()
        self.assertTrue(self.client.task.done())
        self.assertIsNone(self.client.snapshot())
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.writers), 1)

    async def test_bad_upgrade_and_handshake_timeout_close_socket(self):
        self.correct_accept = False
        with self.assertRaises(SocketError):
            await LocalSocket.connect(self.port)
        self.correct_accept, self.hold_handshake = True, True
        with self.assertRaises(asyncio.TimeoutError):
            await LocalSocket.connect(self.port, timeout=0.02)

    async def test_endpoint_discovery_retries_are_bounded(self):
        attempts = []
        self.client = SoloistClient(lambda: attempts.append(1), lambda: True)
        self.client.RETRIES = (0.001, 0.001, 0.001)
        self.client.start()
        await asyncio.wait_for(self.client.task, 1)
        self.assertEqual(len(attempts), 3)
        self.assertFalse(self.client.connected)

    async def test_suspend_gap_reconnects_without_play_or_activate(self):
        socket = type("Socket", (), {"send": AsyncMock(), "close": AsyncMock()})()
        client = SoloistClient(lambda: None, lambda: True)
        client.socket = socket
        with patch("backend.soloist.time.time", side_effect=[1000, 1060]), \
                patch("backend.soloist.time.monotonic", side_effect=[1000, 1010]), \
                patch("backend.soloist.asyncio.sleep", new_callable=AsyncMock):
            await client._health_loop(socket)
        socket.close.assert_awaited_once()
        socket.send.assert_not_awaited()

    async def test_unresponsive_connection_is_closed_after_bounded_health_roundtrip(self):
        socket = type("Socket", (), {"send": AsyncMock(), "close": AsyncMock()})()
        client = SoloistClient(lambda: None, lambda: True)
        client.socket = socket
        client.logged_in = True
        client.last_event = 0
        with patch("backend.soloist.asyncio.sleep", new_callable=AsyncMock):
            await client._health_loop(socket)
        socket.close.assert_awaited_once()
        self.assertEqual([call.args[0]["command"] for call in socket.send.await_args_list], ["get_auth_state", "get_state"])

    async def test_health_send_failure_also_closes_connection(self):
        socket = type("Socket", (), {"send": AsyncMock(side_effect=OSError()), "close": AsyncMock()})()
        client = SoloistClient(lambda: None, lambda: True)
        client.socket = socket
        with patch("backend.soloist.asyncio.sleep", new_callable=AsyncMock):
            await client._health(socket)
        socket.close.assert_awaited_once()

    async def test_activation_of_inactive_receiver_waits_for_real_state_and_never_plays(self):
        await self.connect()
        self.client.event({**playback_event(), "status": "idle", "is_active": False, "item": {}})
        task = asyncio.create_task(self.client.activate())
        await self.until(lambda: any(command["command"] == "activate" for command in self.commands))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done(), "An ACK is not proof that device selection changed")
        self.assertFalse(self.client.active)
        self.writers[-1].write(wire({"type": "device_changed", "is_active": True}))
        self.assertEqual(await asyncio.wait_for(task, 1), {"accepted": True, "active": True})
        self.assertEqual(self.client.snapshot()["status"], "idle")
        self.assertFalse(any(command["command"] in ("play", "pause", "set_volume") for command in self.commands))

    async def test_activation_guard_and_inactive_control_bypass_do_not_dispatch(self):
        await self.connect()
        self.client.event({"type": "device_changed", "is_active": False})
        with self.assertRaises(SpotifyError) as error:
            await self.client.activate(allowed=lambda: False)
        self.assertEqual(error.exception.code, "local_cancelled")
        with self.assertRaises(SpotifyError):
            await self.client.request("play", allow_inactive=True)
        self.assertFalse(any(command["command"] in ("activate", "play") for command in self.commands))

    async def test_activation_ack_without_state_confirmation_is_uncertain_not_retried(self):
        await self.connect()
        self.client.event({"type": "device_changed", "is_active": False})
        self.client.COMMAND_TIMEOUT = 0.03
        with self.assertRaises(SpotifyError) as error:
            await self.client.activate()
        self.assertEqual(error.exception.code, "local_uncertain")
        self.assertFalse(self.client.active)
        self.assertEqual(len([command for command in self.commands if command["command"] == "activate"]), 1)

    async def test_already_active_device_needs_no_activation_write(self):
        await self.connect()
        self.assertTrue((await self.client.activate())["active"])
        self.assertFalse(any(command["command"] == "activate" for command in self.commands))


if __name__ == "__main__":
    unittest.main()
