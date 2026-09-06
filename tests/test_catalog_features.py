import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.catalog import media
from backend.service import Service, playback
from backend.spotify import SpotifyError
from backend.storage import Store

TRACK_ID, PLAYLIST_ID = "a" * 22, "p" * 22
URI = "spotify:track:" + TRACK_ID
CONTEXT = "spotify:playlist:" + PLAYLIST_ID
TRACK = {"id": TRACK_ID, "uri": URI, "type": "track", "name": "Track", "duration_ms": 90000}
INFO = {"id": PLAYLIST_ID, "type": "playlist", "uri": CONTEXT, "name": "Playlist", "snapshot_id": "version-1",
        "owner": {"id": "listener"}, "items": {"total": 3}}


class CatalogFeaturesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.spotify = SimpleNamespace(store=self.store, epoch=0, connected=True, pending=None, auth_error=None,
                                       request=AsyncMock())
        self.player = SimpleNamespace(status=lambda: {})
        self.service = Service(self.spotify, self.player)
        self.service.cache["profile"] = (time.monotonic() + 3600, {"id": "listener", "name": "Listener"})
        self.state = playback({"is_playing": True, "progress_ms": 1500, "item": TRACK,
                               "device": {"id": "remote-device", "name": "Phone", "is_active": True, "volume_percent": 60}})
        self.service.cache["playback"] = (time.monotonic() + 10, self.state)

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    def page(self, items, next=None, total=None):
        return {"items": items, "next": next, "total": len(items) if total is None else total}

    async def test_followed_artist_cursor_is_not_an_offset(self):
        artist = {"type": "artist", "uri": "spotify:artist:" + TRACK_ID, "name": "Artist", "id": TRACK_ID}
        self.spotify.request.return_value = {"artists": {**self.page([artist], "next"), "cursors": {"after": TRACK_ID}}}
        first = await self.service.library("artists")
        self.assertEqual(first["next"], TRACK_ID)
        self.assertTrue(first["items"][0]["saved"])
        await self.service.library("artists", TRACK_ID)
        self.assertEqual(self.spotify.request.call_args.args[2], {"limit": 30, "type": "artist", "after": TRACK_ID})
        with self.assertRaises(SpotifyError):
            await self.service.library("artists", "https://attacker.example")

    async def test_recently_played_cursor_and_timestamp(self):
        self.spotify.request.return_value = {**self.page([{"track": TRACK, "played_at": "2026-09-06T12:00:00Z"}], "next"),
                                            "cursors": {"before": "1788696000000"}}
        result = await self.service.library("recent")
        self.assertEqual(result["next"], "1788696000000")
        self.assertEqual(result["items"][0]["playedAt"], "2026-09-06T12:00:00Z")
        await self.service.library("recent", result["next"])
        self.assertEqual(self.spotify.request.call_args.args[2]["before"], result["next"])

    async def test_show_episode_mapping_and_resume(self):
        episode = {"type": "episode", "id": TRACK_ID, "uri": "spotify:episode:" + TRACK_ID,
                   "name": "Episode", "duration_ms": 3600000, "show": {"name": "Show"},
                   "resume_point": {"fully_played": False, "resume_position_ms": 234000}}
        self.spotify.request.return_value = self.page([{"episode": episode}])
        row = (await self.service.library("episodes"))["items"][0]
        self.assertEqual((row["kind"], row["subtitle"], row["resumePosition"]), ("episode", "Show", 234000))
        self.spotify.request.return_value = None
        await self.service.command("play", {"uri": row["uri"], "positionMs": row["resumePosition"]})
        self.assertEqual(self.spotify.request.call_args.args[3], {"uris": [row["uri"]], "position_ms": 234000})
        episode["resume_point"]["fully_played"] = True
        self.assertEqual(media(episode)["resumePosition"], 0)

    async def test_artist_discography_and_show_episodes_use_supported_paths(self):
        self.spotify.request.return_value = self.page([])
        await self.service.tracks("artist", TRACK_ID)
        self.assertEqual(self.spotify.request.call_args.args[1], f"/artists/{TRACK_ID}/albums")
        await self.service.tracks("show", TRACK_ID)
        self.assertEqual(self.spotify.request.call_args.args[1], f"/shows/{TRACK_ID}/episodes")
        for kind in ("top_tracks", "top_artists"):
            await self.service.library(kind)
            self.assertEqual(self.spotify.request.call_args.args[1], "/me/top/" + kind.split("_")[1])

    async def test_artist_discography_paginates_under_live_ten_item_cap(self):
        albums = [{"type": "album", "id": str(i).zfill(22), "uri": "spotify:album:" + str(i).zfill(22), "name": f"Album {i}"}
                  for i in range(13)]
        # Null catalog entries still consume a provider offset. Counting only
        # rendered rows would repeat an album on the second page.
        raw = albums[:5] + [None] + albums[5:]
        async def request(method, path, params):
            self.assertEqual(path, f"/artists/{TRACK_ID}/albums")
            self.assertLessEqual(params["limit"], 10)
            offset, limit = params["offset"], params["limit"]
            return self.page(raw[offset:offset + limit], "next" if offset + limit < len(raw) else None, len(raw))
        self.spotify.request.side_effect = request
        first = await self.service.tracks("artist", TRACK_ID)
        self.assertEqual(first["next"], 10)
        second = await self.service.tracks("artist", TRACK_ID, first["next"])
        self.assertIsNone(second["next"])
        self.assertEqual([v["uri"] for v in first["items"] + second["items"]], [v["uri"] for v in albums])
        self.assertEqual(self.spotify.request.call_args.args[2]["offset"], 10)

    async def test_playlist_rows_preserve_original_positions_through_nulls(self):
        self.spotify.request.side_effect = [INFO, self.page([None, {"item": TRACK}], total=44)]
        result = await self.service.tracks("playlist", PLAYLIST_ID, 30)
        self.assertEqual(result["items"][0]["position"], 31)
        self.assertEqual(result["items"][0]["contextUri"], CONTEXT)
        self.assertEqual(result["snapshotId"], "version-1")
        self.assertTrue(result["editable"])
        self.spotify.request.side_effect = None
        self.spotify.request.return_value = None
        await self.service.command("play", {"uri": URI, "contextUri": CONTEXT, "position": 31})
        self.assertEqual(self.spotify.request.call_args.args[3], {"context_uri": CONTEXT, "offset": {"position": 31}})

    async def test_liked_selection_builds_forward_queue_across_ui_pages(self):
        next_track = {**TRACK, "id": "b" * 22, "uri": "spotify:track:" + "b" * 22}
        self.spotify.request.side_effect = [self.page([{"track": TRACK}], "next", 101),
                                            self.page([{"track": next_track}], None, 101), None]
        await self.service.command("play", {"uri": URI, "source": "liked", "position": 29})
        self.assertEqual(self.spotify.request.call_args_list[0].args[2]["offset"], 29)
        self.assertEqual(self.spotify.request.call_args_list[1].args[2]["offset"], 30)
        self.assertEqual(self.spotify.request.call_args.args[3], {"uris": [URI, next_track["uri"]]})

    async def test_liked_changed_selection_does_not_start_different_track(self):
        self.spotify.request.return_value = self.page([])
        with self.assertRaises(SpotifyError) as raised:
            await self.service.command("play", {"uri": URI, "source": "liked", "position": 29})
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(self.spotify.request.await_count, 1)

    async def test_library_checks_validate_and_deduplicate_current_api(self):
        artist = "spotify:artist:" + TRACK_ID
        self.spotify.request.return_value = [True, False]
        result = await self.service.library_state([URI, artist, URI])
        self.assertEqual(result, {"states": {URI: True, artist: False}})
        self.assertEqual(self.spotify.request.call_args.args[1:3], ("/me/library/contains", {"uris": URI + "," + artist}))
        with self.assertRaises(SpotifyError):
            await self.service.library_state([URI] * 41)

    async def test_save_unsave_mixed_entities_and_episode_queue(self):
        for kind in ("track", "album", "playlist", "artist", "show", "episode"):
            uri = f"spotify:{kind}:{TRACK_ID}"
            self.spotify.request.return_value = None
            await self.service.command("save", uri)
            self.assertEqual(self.spotify.request.call_args.args[:3], ("PUT", "/me/library", {"uris": uri}))
            await self.service.command("unsave", uri)
            self.assertEqual(self.spotify.request.call_args.args[:3], ("DELETE", "/me/library", {"uris": uri}))
        self.service.cache["playback"] = (time.monotonic() + 10, self.state)
        await self.service.command("queue", "spotify:episode:" + TRACK_ID)
        self.assertEqual(self.spotify.request.call_args.args[2]["uri"], "spotify:episode:" + TRACK_ID)

    async def test_new_account_cannot_receive_old_catalog_response_or_cached_page(self):
        gate, started = asyncio.Event(), asyncio.Event()
        async def request(*args):
            started.set()
            await gate.wait()
            return self.page([{"track": TRACK}])
        self.spotify.request.side_effect = request
        task = asyncio.create_task(self.service.library("liked"))
        await started.wait()
        self.spotify.epoch += 1
        gate.set()
        with self.assertRaises(SpotifyError):
            await task
        self.assertNotIn("library:liked:0", self.service.cache)

    async def test_queued_command_cannot_modify_replacement_account(self):
        await self.service.command_lock.acquire()
        task = asyncio.create_task(self.service.command("save", URI))
        await asyncio.sleep(0)
        self.spotify.epoch += 1
        self.service.command_lock.release()
        with self.assertRaises(SpotifyError):
            await task
        self.spotify.request.assert_not_awaited()

    async def test_pin_and_search_history_persist_and_clear(self):
        row = media(INFO)
        await self.service.pin("add", row)
        self.spotify.request.return_value = {"tracks": self.page([])}
        for i in range(10):
            await self.service.search("query " + str(i), "track")
        await self.service.search("QUERY 9", "track")
        reread = Store(self.directory.name)
        self.assertEqual(reread.data["pinned_collections"][0]["uri"], CONTEXT)
        self.assertEqual(len(reread.data["search_history"]), 8)
        self.assertEqual(reread.data["search_history"][0], "QUERY 9")
        await self.service.preferences("clear_search_history")
        self.assertEqual(self.service.feature_snapshot()["searchHistory"], [])
        await self.service.clear_personal_data()
        self.assertEqual((await self.service.library("pinned"))["items"], [])

    async def test_pin_limit_and_unsafe_uri_are_rejected(self):
        for i in range(12):
            await self.service.pin("add", {"uri": "spotify:playlist:" + str(i).zfill(22), "title": "Collection"})
        with self.assertRaises(SpotifyError):
            await self.service.pin("add", {"uri": CONTEXT})
        with self.assertRaises(SpotifyError):
            await self.service.pin("add", {"uri": "https://other.example"})

    async def test_playlist_create_defaults_to_private_and_does_not_retry(self):
        self.spotify.request.return_value = INFO
        await self.service.playlist("create", {"name": " New playlist "})
        self.assertEqual(self.spotify.request.call_args.args[:2], ("POST", "/me/playlists"))
        self.assertFalse(self.spotify.request.call_args.args[3]["public"])
        self.assertEqual(self.spotify.request.await_count, 1)

    async def test_editable_playlist_picker_loads_cold_profile(self):
        self.service.cache.pop("profile")
        self.spotify.request.side_effect = [self.page([INFO]), {"id": "listener", "display_name": "Listener"}]
        result = await self.service.library("playlists")
        self.assertTrue(result["items"][0]["editable"])
        self.assertEqual(self.spotify.request.call_args.args[1], "/me")

    async def test_playlist_add_and_reorder_use_items_and_snapshot(self):
        self.spotify.request.side_effect = [INFO, {"snapshot_id": "v2"}]
        result = await self.service.playlist("add", {"id": PLAYLIST_ID, "uris": [URI]})
        self.assertEqual(result["snapshotId"], "v2")
        self.assertEqual(self.spotify.request.call_args.args, ("POST", f"/playlists/{PLAYLIST_ID}/items", {}, {"uris": [URI]}))
        self.service.cache["profile"] = (time.monotonic() + 3600, {"id": "listener"})
        self.spotify.request.side_effect = [{**INFO, "snapshot_id": "v2"}, {"snapshot_id": "v3"}]
        await self.service.playlist("reorder", {"id": PLAYLIST_ID, "position": 0, "insertBefore": 3, "snapshotId": "v2"})
        self.assertEqual(self.spotify.request.call_args.args[3], {"range_start": 0, "range_length": 1, "insert_before": 3, "snapshot_id": "v2"})

    async def test_playlist_stale_version_and_non_owner_cannot_write(self):
        for info, value in [(INFO, {"snapshotId": "old"}), ({**INFO, "owner": {"id": "other"}}, {"snapshotId": "version-1"})]:
            self.spotify.request.return_value = info
            self.spotify.request.reset_mock()
            with self.assertRaises(SpotifyError):
                await self.service.playlist("reorder", {"id": PLAYLIST_ID, "position": 0, "insertBefore": 3, **value})
            self.assertTrue(all(c.args[0] == "GET" for c in self.spotify.request.call_args_list))

    async def test_unique_removal_reads_complete_playlist_and_rechecks_snapshot(self):
        other = {**TRACK, "uri": "spotify:track:" + "b" * 22}
        self.spotify.request.side_effect = [INFO, self.page([{"item": other}, {"item": TRACK}], "next", 3),
                                            self.page([{"item": other}], None, 3), INFO, {"snapshot_id": "v2"},
                                            self.page([{"item": other}, {"item": other}])]
        await self.service.playlist("remove", {"id": PLAYLIST_ID, "uri": URI, "position": 1, "snapshotId": "version-1"})
        delete = next(call for call in self.spotify.request.call_args_list if call.args[0] == "DELETE")
        self.assertEqual(delete.args[3], {"items": [{"uri": URI}], "snapshot_id": "version-1"})

    async def test_known_mutation_snapshot_rejects_eventually_consistent_old_metadata(self):
        self.service._remember_playlist_version(PLAYLIST_ID, "new-version", "version-1")
        self.spotify.request.side_effect = [INFO, self.page([{"item": TRACK}], total=1), self.page([{"item": TRACK}])]
        result = await self.service.tracks("playlist", PLAYLIST_ID)
        self.assertEqual(result["snapshotId"], "new-version")
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.spotify.request.await_count, 3)

    async def test_stale_metadata_with_unconfirmed_item_count_cannot_enable_edit(self):
        self.service._remember_playlist_version(PLAYLIST_ID, "new-version", "version-1")
        self.spotify.request.side_effect = [INFO, {"items": []}]
        with self.assertRaises(SpotifyError) as raised:
            await self.service.tracks("playlist", PLAYLIST_ID)
        self.assertEqual(raised.exception.code, "pending")
        self.assertNotIn(f"tracks:playlist:{PLAYLIST_ID}:0", self.service.cache)
        self.assertTrue(all(call.args[0] == "GET" for call in self.spotify.request.call_args_list))

    async def test_stale_get_snapshot_uses_acknowledged_version_for_next_edit(self):
        self.service._remember_playlist_version(PLAYLIST_ID, "after-reorder", "version-1")
        self.spotify.request.side_effect = [INFO, self.page([{"item": TRACK}], total=3), {"snapshot_id": "next-version"}]
        await self.service.playlist("reorder", {"id": PLAYLIST_ID, "position": 0, "insertBefore": 3, "snapshotId": "after-reorder"})
        self.assertEqual(self.spotify.request.call_args.args[3]["snapshot_id"], "after-reorder")

    async def test_unknown_new_external_snapshot_is_not_confused_with_known_stale_one(self):
        self.service._remember_playlist_version(PLAYLIST_ID, "our-version", "version-1")
        self.spotify.request.side_effect = [{**INFO, "snapshot_id": "external-edit"}, self.page([{"item": TRACK}])]
        self.assertEqual((await self.service.tracks("playlist", PLAYLIST_ID))["snapshotId"], "external-edit")

    async def test_delete_ack_without_removal_returns_unconfirmed_and_never_repeats_write(self):
        before = self.page([{"item": TRACK}])
        info = {**INFO, "items": {"total": 1}}
        self.spotify.request.side_effect = [info, before, info, {"snapshot_id": "new-version"}, before, before, before]
        with self.assertRaises(SpotifyError) as raised:
            await self.service.playlist("remove", {"id": PLAYLIST_ID, "uri": URI, "position": 0, "snapshotId": "version-1"})
        self.assertEqual(raised.exception.code, "unconfirmed")
        self.assertEqual(sum(call.args[0] == "DELETE" for call in self.spotify.request.call_args_list), 1)
        self.assertEqual(self.service._playlist_versions[PLAYLIST_ID]["current"], "new-version")
        self.assertNotIn(f"tracks:playlist:{PLAYLIST_ID}:0", self.service.cache)

    async def test_duplicate_occurrence_removal_refuses_before_any_write(self):
        self.spotify.request.side_effect = [INFO, self.page([{"item": TRACK}, {"item": TRACK}, None])]
        with self.assertRaises(SpotifyError) as raised:
            await self.service.playlist("remove", {"id": PLAYLIST_ID, "uri": URI, "position": 1, "snapshotId": "version-1"})
        self.assertEqual(raised.exception.code, "unsupported")
        self.assertTrue(all(c.args[0] == "GET" for c in self.spotify.request.call_args_list))

    async def test_timer_pauses_without_any_snapshot_poll_and_cancels_on_close(self):
        self.service.command = AsyncMock()
        await self.service.timer(1)
        self.service._timer_deadline = time.monotonic() - 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.service.command.assert_awaited_once_with("pause")
        self.assertFalse(self.service.timer_status()["active"])
        await self.service.timer(10)
        await self.service.close()
        self.assertFalse(self.service.timer_status()["active"])
        self.assertEqual(self.service.command.await_count, 1)

    async def test_timer_cancels_on_account_change_and_reports_pause_failure(self):
        self.service.command = AsyncMock(side_effect=SpotifyError("Disconnected", "offline"))
        await self.service.timer(1)
        self.spotify.epoch += 1
        self.service.feature_snapshot()
        await asyncio.sleep(0)
        self.service.command.assert_not_awaited()
        await self.service.timer(1)
        self.service._timer_ends = int(time.time() * 1000) - 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.service.timer_status()["error"], "Disconnected")

    async def test_timer_input_validation(self):
        for value in (True, -1, 241, 1.5, "30"):
            with self.assertRaises(SpotifyError):
                await self.service.timer(value)


if __name__ == "__main__":
    unittest.main()
