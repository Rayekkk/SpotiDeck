import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock

from backend.player import Player, extract_binary
from backend.service import Service, media, playback
from backend.spotify import REDIRECT, Spotify, SpotifyError
from backend.storage import Store

TRACK = {"id": "0" * 22, "uri": "spotify:track:" + "0" * 22, "type": "track", "name": "A track",
         "duration_ms": 200000, "artists": [{"name": "Artist"}], "album": {"images": [{"url": "https://i.scdn.co/image/test"}]}}


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0)


class SpotifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.transport = Transport([])
        self.spotify = Spotify(self.store, self.transport)

    async def asyncTearDown(self):
        await self.spotify.close()
        self.temp.cleanup()

    def authenticated(self, expired=False):
        self.store.update(client_id="a" * 32, access_token="test-access", refresh_token="test-refresh",
                          expires_at=0 if expired else time.time() + 3600)

    async def test_pkce_state_and_verifier_are_cryptographically_bound(self):
        result = await self.spotify.begin_auth("a" * 32)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result["url"]).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(self.spotify.pending["verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertNotIn("client_secret", query)
        self.assertEqual(self.spotify.server.sockets[0].getsockname()[0], "127.0.0.1")

    async def test_bad_state_cannot_consume_valid_session(self):
        await self.spotify.begin_auth("a" * 32)
        with self.assertRaises(SpotifyError):
            await self.spotify.finish_auth(REDIRECT + "?state=wrong&code=test")
        self.assertIsNotNone(self.spotify.pending)
        self.assertEqual(self.transport.calls, [])

    async def test_callback_wrong_origin_is_rejected(self):
        await self.spotify.begin_auth("a" * 32)
        state = self.spotify.pending["state"]
        with self.assertRaises(SpotifyError):
            await self.spotify.finish_auth("https://attacker.example/callback?code=test&state=" + state)
        self.assertEqual(self.transport.calls, [])

    async def test_callback_exchanges_once_and_cannot_be_replayed(self):
        await self.spotify.begin_auth("a" * 32)
        verifier = self.spotify.pending["verifier"]
        state = self.spotify.pending["state"]
        self.transport.responses.append((200, {}, {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}))
        url = REDIRECT + "?state=" + state + "&code=code"
        await self.spotify.finish_auth(url)
        self.assertTrue(self.spotify.connected)
        self.assertIsNone(self.spotify.server)
        posted = urllib.parse.parse_qs(self.transport.calls[0][3].decode())
        self.assertEqual(posted["code_verifier"], [verifier])
        with self.assertRaises(SpotifyError):
            await self.spotify.finish_auth(url)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_http_callback_replies_before_browser_closes_connection(self):
        await self.spotify.begin_auth("a" * 32)
        state = self.spotify.pending["state"]
        server = self.spotify.server
        if sys.version_info < (3, 12):
            # Match modern asyncio's accepted-connection drain on older dev hosts.
            original_wait_closed = server.wait_closed
            async def wait_for_connections():
                while server._active_count:
                    await asyncio.sleep(0.001)
                await original_wait_closed()
            server.wait_closed = wait_for_connections
        self.transport.responses.append((200, {}, {"access_token": "access", "refresh_token": "refresh"}))
        reader, writer = await asyncio.open_connection("127.0.0.1", 43891)
        try:
            request = f"GET /callback?state={state}&code=code HTTP/1.1\r\nHost: 127.0.0.1:43891\r\n\r\n"
            writer.write(request.encode("ascii"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 2)
            self.assertTrue(response.startswith(b"HTTP/1.1 200 OK\r\n"))
            self.assertIn(b"Connected to Spotify", response)
            self.assertIn(b"<title>SpotiDeck</title>", response)
            self.assertTrue(self.spotify.connected)
            self.assertEqual(len(self.transport.calls), 1)
            self.assertIsNone(self.spotify.server)
        finally:
            writer.close()
            await writer.wait_closed()
        await asyncio.wait_for(self.spotify.close(), 2)
        self.assertFalse(self.spotify.server_closures)
        self.assertFalse(self.spotify.callback_tasks)
        self.assertFalse(self.spotify.callback_writers)

    async def test_close_releases_incomplete_http_callback_and_listener(self):
        await self.spotify.begin_auth("a" * 32)
        reader, writer = await asyncio.open_connection("127.0.0.1", 43891)
        try:
            writer.write(b"GET /callback?state=")
            await writer.drain()
            await asyncio.sleep(0)
            await asyncio.wait_for(self.spotify.close(), 2)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            self.assertFalse(self.spotify.callback_tasks)
            self.assertFalse(self.spotify.callback_writers)
            self.assertFalse(self.spotify.server_closures)
            self.assertIsNone(self.spotify.server)
            self.assertFalse(self.spotify.connected)
            self.assertEqual(self.transport.calls, [])
            replacement = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 43891)
            replacement.close()
            await replacement.wait_closed()
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_expired_state_and_denial_never_exchange(self):
        await self.spotify.begin_auth("a" * 32)
        state = self.spotify.pending["state"]
        self.spotify.pending["expires"] = 0
        with self.assertRaises(SpotifyError):
            await self.spotify.finish_auth(REDIRECT + "?state=" + state + "&code=code")
        self.assertFalse(self.spotify.connected)
        self.assertEqual(len(self.transport.calls), 0)

    async def test_refresh_is_serialized_and_rotated_token_is_saved(self):
        self.authenticated(expired=True)
        self.transport.responses.append((200, {}, {"access_token": "new-access", "refresh_token": "rotated", "expires_in": 3600}))
        values = await asyncio.gather(*[self.spotify.token() for _ in range(8)])
        self.assertEqual(values, ["new-access"] * 8)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.data["refresh_token"], "rotated")

    async def test_invalid_refresh_forces_sign_in_without_leaking_response(self):
        self.authenticated(expired=True)
        self.transport.responses.append((400, {}, {"error": "invalid_grant", "private": "secret"}))
        with self.assertRaises(SpotifyError) as raised:
            await self.spotify.token()
        self.assertNotIn("secret", str(raised.exception))
        self.assertFalse(self.spotify.connected)

    async def test_rate_limit_stops_subsequent_requests(self):
        self.authenticated()
        self.transport.responses.append((429, {"retry-after": "90"}, {}))
        for _ in range(2):
            with self.assertRaises(SpotifyError) as raised:
                await self.spotify.request("GET", "/me/player")
            self.assertEqual(raised.exception.code, "rate_limit")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_rejected_write_is_not_automatically_replayed(self):
        self.authenticated()
        self.transport.responses.append((503, {}, {}))
        with self.assertRaises(SpotifyError):
            await self.spotify.request("POST", "/me/player/next")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_unauthorized_refresh_is_only_retried_once(self):
        self.authenticated()
        self.transport.responses.extend([(401, {}, {}), (200, {}, {"access_token": "fresh"}), (401, {}, {})])
        with self.assertRaises(SpotifyError):
            await self.spotify.request("GET", "/me/player")
        self.assertEqual(len(self.transport.calls), 3)

    async def test_disconnect_clears_tokens_and_pending_login(self):
        self.authenticated()
        await self.spotify.begin_auth("a" * 32)
        await self.spotify.disconnect()
        self.assertFalse(self.spotify.connected)
        self.assertIsNone(self.spotify.pending)
        self.assertIsNone(self.spotify.server)
        reread = Store(self.temp.name)
        self.assertIsNone(reread.data["access_token"])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        store = Store(self.temp.name)
        store.update(refresh_token="fake-refresh")
        self.spotify = Spotify(store)
        self.spotify.request = AsyncMock()
        self.service = Service(self.spotify, Player(store, Path(self.temp.name) / "player"))

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_search_uses_current_ten_item_limit(self):
        self.spotify.request.return_value = {"tracks": {"items": [None, TRACK], "next": "https://api.spotify.com/next", "total": 15}}
        result = await self.service.search("ambient & bass", "track")
        args = self.spotify.request.call_args.args
        self.assertEqual(args[2]["limit"], 10)
        self.assertEqual(args[2]["q"], "ambient & bass")
        self.assertEqual(result["next"], 2)
        self.assertEqual(len(result["items"]), 1)

    async def test_playlist_uses_items_schema_and_counts_nulls_in_pagination(self):
        self.spotify.request.return_value = {"items": [{"item": TRACK}, {"item": None}, None], "next": "https://api.spotify.com/next", "total": 40}
        result = await self.service.tracks("playlist", "1" * 22)
        self.assertEqual(self.spotify.request.call_args.args[1], "/playlists/" + "1" * 22 + "/items")
        self.assertEqual(result["next"], 3)
        self.assertEqual(len(result["items"]), 1)

    async def test_old_playlist_track_field_is_supported(self):
        self.spotify.request.return_value = {"items": [{"track": TRACK}], "next": None, "total": 1}
        self.assertEqual(len((await self.service.tracks("playlist", "1" * 22))["items"]), 1)

    async def test_cache_coalesces_reads_and_invalidates_after_change(self):
        self.spotify.request.return_value = {"items": [TRACK], "next": None}
        await asyncio.gather(*[self.service.library("playlists") for _ in range(5)])
        self.assertEqual(self.spotify.request.await_count, 1)
        self.service.invalidate()
        await self.service.library("playlists")
        self.assertEqual(self.spotify.request.await_count, 2)

    async def test_cancelled_cache_caller_cannot_orphan_its_loader_during_unload(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def load():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        caller = asyncio.create_task(self.service.cached("private", load))
        await entered.wait()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        self.assertEqual(len(self.service.inflight), 1)
        await self.service.close()
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.service.inflight, {})

    async def test_remote_playback_requests_episode_metadata(self):
        self.spotify.request.return_value = {"device": {"id": "phone", "is_active": True},
            "item": {"id": "A" * 22, "uri": "spotify:episode:" + "A" * 22,
                     "type": "episode", "name": "Episode"}}
        state = await self.service._playback_load()
        self.assertEqual(state["track"]["kind"], "episode")
        self.assertEqual(self.spotify.request.call_args.args[2], {"additional_types": "track,episode"})

    async def test_changed_account_cannot_repopulate_cache(self):
        gate = asyncio.Event()
        async def load():
            await gate.wait()
            return "old account"
        task = asyncio.create_task(self.service.cached("private", load))
        await asyncio.sleep(0)
        self.spotify.epoch += 1
        self.service.invalidate()
        gate.set()
        await task
        self.assertNotIn("private", self.service.cache)

    async def test_snapshot_never_exposes_keys_or_tokens(self):
        self.spotify.store.update(soloist_key="private-player-key", access_token="secret")
        self.spotify.request.side_effect = [{"display_name": "Rayek"}, None]
        result = await self.service.snapshot()
        text = json.dumps(result)
        self.assertNotIn("private-player-key", text)
        self.assertNotIn("fake-refresh", text)
        self.assertNotIn("secret", text)
        self.assertTrue(result["player"]["hasKey"])

    async def test_snapshot_handles_no_current_playback(self):
        self.spotify.request.side_effect = [{"display_name": "Rayek"}, None]
        result = await self.service.snapshot()
        self.assertIsNone(result["playback"])
        self.assertTrue(result["connected"])

    async def test_no_device_cannot_silently_start_playback_elsewhere(self):
        self.spotify.request.return_value = None
        with self.assertRaises(SpotifyError) as raised:
            await self.service.command("play", {"uri": TRACK["uri"]})
        self.assertEqual(raised.exception.code, "no_device")
        self.assertEqual(self.spotify.request.await_count, 1)

    async def test_playback_is_bound_to_the_shown_device(self):
        self.service.cache["playback"] = (time.monotonic() + 10, {"device": {"id": "shown-device", "restricted": False}, "track": {"duration": 200000}})
        await self.service.command("play", {"uri": TRACK["uri"]})
        self.spotify.request.assert_awaited_once_with("PUT", "/me/player/play", {"device_id": "shown-device"}, {"uris": [TRACK["uri"]]})

    async def test_invalid_uri_and_numeric_values_are_rejected(self):
        for command, value in [("play", {"uri": "https://attacker.example"}), ("volume", float('nan')), ("seek", -1), ("repeat", "forever")]:
            self.service.cache["playback"] = (time.monotonic() + 10, {"device": {"id": "device", "restricted": False}, "track": {"duration": 200000}})
            with self.assertRaises(SpotifyError):
                await self.service.command(command, value)
        self.spotify.request.assert_not_awaited()


class StorageAndPlayerTests(unittest.TestCase):
    def test_atomic_update_preserves_previous_file_when_replace_fails(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.update(value="before")
            with patch("backend.storage.os.replace", side_effect=OSError("test failure")):
                with self.assertRaises(OSError):
                    store.update(value="after")
            self.assertEqual(Store(temp).data["value"], "before")
            self.assertEqual(store.data["value"], "before")
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["account.json"])

    def test_corrupt_settings_are_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "account.json"
            path.write_text("bad json")
            with self.assertRaises(ValueError):
                Store(temp)
            self.assertEqual(path.read_text(), "bad json")

    def test_oversized_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            with self.assertRaises(ValueError):
                store.update(key="x" * 65537)
            self.assertFalse(store.path.exists())

    def archive(self, path, name="folder/soloist", content=b"\x7fELF" + bytes(64), link=False):
        with tarfile.open(path, "w:gz") as bundle:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            if link:
                item.type = tarfile.SYMTYPE
                item.linkname = "../../outside"
                item.size = 0
            bundle.addfile(item, None if link else io.BytesIO(content))

    def test_installer_ignores_archive_paths_and_writes_only_requested_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination = Path(temp) / "archive.gz", Path(temp) / "output"
            self.archive(source, "../../outside/soloist")
            extract_binary(source, destination)
            self.assertTrue(destination.read_bytes().startswith(b"\x7fELF"))
            self.assertEqual(len(list(Path(temp).iterdir())), 2)

    def test_installer_rejects_symlinks_and_non_elf_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination = Path(temp) / "archive.gz", Path(temp) / "output"
            for options in ({"link": True}, {"content": b"not an executable" * 5}):
                self.archive(source, **options)
                with self.assertRaises(SpotifyError):
                    extract_binary(source, destination)

    def test_null_items_and_restricted_tracks_are_supported(self):
        self.assertIsNone(media(None))
        result = media({**TRACK, "is_playable": False, "is_local": True})
        self.assertFalse(result["playable"])
        self.assertIsNone(playback(None))

    def test_expired_player_has_actionable_message(self):
        self.assertIn("expired", Player._exit_message(10))


if __name__ == "__main__":
    unittest.main()
