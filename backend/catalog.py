"""Catalog and local library helpers; playback transport stays in Service."""
import asyncio
import math
import re
import time

from .spotify import SpotifyError

KINDS = "track|album|playlist|artist|show|episode"


def image(data):
    images = (data or {}).get("images") or []
    return next((item["url"] for item in images if isinstance(item, dict) and
                 isinstance(item.get("url"), str) and item["url"].startswith("https://")), None)


def media(data, album=None):
    if not isinstance(data, dict) or data.get("type") not in KINDS.split("|"):
        return None
    kind = data["type"]
    parent = data.get("album") or album or {}
    artists = data.get("artists") or []
    subtitle = ", ".join(str(a.get("name", "")) for a in artists if isinstance(a, dict))
    if kind == "playlist":
        subtitle = "Playlist · " + str((data.get("owner") or {}).get("display_name") or "Spotify")
    elif kind == "artist":
        subtitle = "Artist"
    elif kind == "show":
        subtitle = str(data.get("publisher") or "Podcast")
    elif kind == "episode":
        subtitle = str((data.get("show") or album or {}).get("name") or "Podcast episode")
    count = data.get("items") or data.get("tracks") or {}
    resume = data.get("resume_point") or {}
    result = {"id": str(data.get("id") or ""), "uri": str(data.get("uri") or ""), "kind": kind,
              "title": str(data.get("name") or "Unavailable item"), "subtitle": subtitle,
              "image": image(data if kind != "track" else parent), "duration": data.get("duration_ms") or 0,
              "total": count.get("total") if isinstance(count, dict) else None,
              "explicit": bool(data.get("explicit")),
              "playable": bool(data.get("uri")) and data.get("is_playable", True) and not data.get("is_local", False)}
    if kind == "show":
        result["total"] = data.get("total_episodes")
    if kind == "episode":
        result.update(resumePosition=0 if resume.get("fully_played") else resume.get("resume_position_ms", 0),
                      fullyPlayed=bool(resume.get("fully_played")))
    if kind == "playlist":
        result["snapshotId"] = data.get("snapshot_id")
        result["ownerId"] = (data.get("owner") or {}).get("id")
        result["collaborative"] = bool(data.get("collaborative"))
    return result


def offset_value(value):
    if type(value) is not int or not 0 <= value <= 100000:
        raise SpotifyError("Invalid page number.")
    return value


def item_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9]{22}", value):
        raise SpotifyError("Invalid Spotify item.")
    return value


def uri_value(value, kinds=KINDS):
    if not isinstance(value, str) or not re.fullmatch(r"spotify:(" + kinds + r"):[a-zA-Z0-9]{22}", value):
        raise SpotifyError("This Spotify item cannot be played here.")
    return value


def cursor_value(value, artist=False):
    if value == 0 and type(value) is int:
        return None
    if artist:
        return item_id(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
        raise SpotifyError("Invalid history cursor.")
    return value


class Catalog:
    def _catalog_init(self):
        self.catalog_lock = asyncio.Lock()
        self._timer_task = None
        self._timer_deadline = None
        self._timer_ends = None
        self._timer_error = None
        self._catalog_epoch = self.spotify.epoch
        self._playlist_versions = {}

    def _catalog_identity(self):
        if self._catalog_epoch != self.spotify.epoch:
            self.invalidate()
            self._cancel_timer()
            self._catalog_epoch = self.spotify.epoch
        return self.spotify.epoch

    def _check_epoch(self, epoch):
        if epoch != self.spotify.epoch or self._closed:
            raise SpotifyError("Your Spotify connection changed. Please try again.", "auth")

    def _pins(self):
        values = self.spotify.store.data.get("pinned_collections", [])
        return [v for v in values[:12] if isinstance(v, dict) and isinstance(v.get("uri"), str)] if isinstance(values, list) else []

    def feature_snapshot(self):
        self._catalog_identity()
        history = self.spotify.store.data.get("search_history", [])
        return {"pins": [v["uri"] for v in self._pins()], "sleepTimer": self.timer_status(),
                "searchHistory": [v for v in history[:8] if isinstance(v, str)] if isinstance(history, list) else [],
                "needsReauthorization": bool(getattr(self.spotify, "needs_reauthorization", False))}

    async def clear_personal_data(self):
        self._cancel_timer()
        self.spotify.store.update(pinned_collections=[], search_history=[], catalog_account_id=None)
        self.invalidate()

    async def pin(self, action, value):
        if action not in ("add", "remove") or not isinstance(value, dict):
            raise SpotifyError("Invalid pinned item.")
        uri = uri_value(value.get("uri"), "album|playlist|artist|show")
        async with self.catalog_lock:
            pins = [v for v in self._pins() if v["uri"] != uri]
            if action == "add":
                if len(pins) >= 12:
                    raise SpotifyError("You can pin up to 12 collections. Unpin one first.")
                kind, id = uri.split(":")[1:]
                # Store only bounded display metadata, never arbitrary caller properties.
                item = {"uri": uri, "id": id, "kind": kind, "title": str(value.get("title") or "Pinned collection")[:300],
                        "subtitle": str(value.get("subtitle") or "")[:300], "image": None,
                        "duration": 0, "total": None, "explicit": False, "playable": True}
                if isinstance(value.get("image"), str) and value["image"].startswith("https://") and len(value["image"]) <= 2048:
                    item["image"] = value["image"]
                pins.append(item)
            self.spotify.store.update(pinned_collections=pins)
            self.cache.pop("library:pinned:0", None)
            return [v["uri"] for v in pins]

    async def preferences(self, action, value=None):
        if action != "clear_search_history":
            raise SpotifyError("Unknown preference.")
        self.spotify.store.update(search_history=[])
        return self.feature_snapshot()

    async def library(self, kind, offset=0):
        epoch = self._catalog_identity()
        if kind == "pinned":
            offset_value(offset)
            pins = self._pins()
            return {"items": pins[offset:offset + 30], "next": None, "total": len(pins)}
        paths = {"playlists": "/me/playlists", "albums": "/me/albums", "liked": "/me/tracks",
                 "artists": "/me/following", "shows": "/me/shows", "episodes": "/me/episodes",
                 "recent": "/me/player/recently-played", "top_tracks": "/me/top/tracks", "top_artists": "/me/top/artists"}
        if kind not in paths:
            raise SpotifyError("Invalid library section.")
        params = {"limit": 30}
        if kind in ("artists", "recent"):
            cursor = cursor_value(offset, kind == "artists")
            if cursor:
                params["after" if kind == "artists" else "before"] = cursor
            if kind == "artists":
                params["type"] = "artist"
        else:
            params["offset"] = offset_value(offset)
        if kind.startswith("top_"):
            params["time_range"] = "medium_term"
        async def load():
            data = await self.spotify.request("GET", paths[kind], params)
            if kind == "artists":
                data = data.get("artists") or {}
            raw = data.get("items") or []
            items = []
            profile = self.cache.get("profile")
            user = profile[1].get("id") if profile else None
            if kind == "playlists" and not user and any(isinstance(entry, dict) and entry.get("type") == "playlist" for entry in raw):
                user = (await self.cached("profile", self._profile_load, 3600)).get("id")
            wrapper = {"albums": "album", "liked": "track", "shows": "show", "episodes": "episode", "recent": "track"}.get(kind)
            for index, entry in enumerate(raw):
                if not isinstance(entry, dict):
                    continue
                item = media(entry.get(wrapper) if wrapper else entry)
                if not item:
                    continue
                if kind in ("albums", "liked", "shows", "episodes", "artists", "playlists"):
                    item["saved"] = True
                if kind == "liked":
                    item.update(position=offset + index, source="liked")
                if kind == "recent":
                    item["playedAt"] = entry.get("played_at")
                if kind == "playlists":
                    item["editable"] = bool(item.get("collaborative") or (user and item.get("ownerId") == user))
                items.append(item)
            next_page = None
            if data.get("next") and raw:
                if kind in ("artists", "recent"):
                    candidate = (data.get("cursors") or {}).get("after" if kind == "artists" else "before")
                    if candidate and candidate != offset:
                        try:
                            next_page = cursor_value(str(candidate), kind == "artists")
                        except SpotifyError:
                            pass
                else:
                    next_page = offset + len(raw)
            return {"items": items, "next": next_page, "total": data.get("total")}
        result = await self.cached(f"library:{kind}:{offset}", load)
        self._check_epoch(epoch)
        return result

    async def search(self, query, kind, offset=0):
        epoch = self._catalog_identity()
        offset = offset_value(offset)
        if kind not in KINDS.split("|") or not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or offset > 1000:
            raise SpotifyError("Enter a search of 1–200 characters.")
        query = query.strip()
        async def load():
            response = await self.spotify.request("GET", "/search", {"q": query, "type": kind, "limit": 10, "offset": offset})
            data = response.get(kind + "s") or {}
            raw = data.get("items") or []
            return {"items": [item for entry in raw if (item := media(entry))],
                    "next": offset + len(raw) if data.get("next") and raw and offset + len(raw) <= 1000 else None,
                    "total": data.get("total")}
        result = await self.cached(f"search:{query}:{kind}:{offset}", load, 120)
        self._check_epoch(epoch)
        if offset == 0:
            history = self.feature_snapshot()["searchHistory"]
            updated = [query] + [v for v in history if v.casefold() != query.casefold()]
            if updated[:8] != history:
                self.spotify.store.update(search_history=updated[:8])
        return result

    async def _playlist_info(self, id, fresh=False):
        async def load():
            data = await self.spotify.request("GET", f"/playlists/{id}", {"fields": "id,uri,name,owner(id),collaborative,snapshot_id,items(total)"})
            known = self._playlist_versions.get(id)
            if known and known["epoch"] == self.spotify.epoch:
                if data.get("snapshot_id") in known["stale"]:
                    # Spotify's playlist metadata can retain an old snapshot
                    # long after item reads and mutations show the new contents.
                    # Keep our acknowledged version and refresh the count from
                    # the item endpoint rather than sending that old version back.
                    count = await self.spotify.request("GET", f"/playlists/{id}/items", {"offset": 0, "limit": 1})
                    total = count.get("total") if isinstance(count, dict) else None
                    if type(total) is not int or total < 0:
                        raise SpotifyError("Spotify could not confirm this playlist's size. Refresh it before editing.", "pending", 1)
                    data = {**data, "snapshot_id": known["current"], "items": {"total": total}}
                elif data.get("snapshot_id") != known["current"]:
                    self._remember_playlist_version(id, data.get("snapshot_id"), known["current"])
            profile = await self.cached("profile", self._profile_load, 3600)
            user = profile.get("id")
            return {**data, "editable": bool(data.get("collaborative") or (user and (data.get("owner") or {}).get("id") == user))}
        return await load() if fresh else await self.cached(f"playlist_info:{id}", load, 30)

    def _remember_playlist_version(self, id, snapshot, previous=None):
        if not isinstance(snapshot, str) or not snapshot or len(snapshot) > 512:
            return
        known = self._playlist_versions.get(id)
        stale = list(known["stale"]) if known and known["epoch"] == self.spotify.epoch else []
        if previous and previous != snapshot and previous not in stale:
            stale.append(previous)
        if known and known["epoch"] == self.spotify.epoch and known["current"] != snapshot and known["current"] not in stale:
            stale.append(known["current"])
        stale = [value for value in stale if value != snapshot][-64:]
        self._playlist_versions[id] = {"epoch": self.spotify.epoch, "current": snapshot, "stale": stale}
        if len(self._playlist_versions) > 64:
            self._playlist_versions.pop(next(iter(self._playlist_versions)))

    async def _confirm_playlist_removal(self, id, uri, epoch):
        path = f"/playlists/{id}/items"
        for delay in (0, 0.2, 0.5):
            if delay:
                await asyncio.sleep(delay)
            index, present = 0, False
            while index <= 100000:
                data = await self.spotify.request("GET", path, {"offset": index, "limit": 50})
                self._check_epoch(epoch)
                raw = data.get("items") if isinstance(data, dict) else None
                if not isinstance(raw, list):
                    raise SpotifyError("Spotify accepted the removal, but it could not be confirmed. Refresh the playlist before trying again.", "unconfirmed")
                for wrapper in raw:
                    item = wrapper.get("item", wrapper.get("track")) if isinstance(wrapper, dict) else None
                    if isinstance(item, dict) and item.get("uri") == uri:
                        present = True
                        break
                if present:
                    break
                if not data.get("next"):
                    return
                if not raw:
                    break
                index += len(raw)
        raise SpotifyError("Spotify accepted the removal, but it is not confirmed yet. Refresh the playlist before trying again.", "unconfirmed")

    async def tracks(self, kind, id, offset=0):
        epoch = self._catalog_identity()
        id, offset = item_id(id), offset_value(offset)
        paths = {"album": f"/albums/{id}/tracks", "playlist": f"/playlists/{id}/items",
                 "artist": f"/artists/{id}/albums", "show": f"/shows/{id}/episodes"}
        if kind not in paths:
            raise SpotifyError("This item has no track list.")
        async def load():
            # Artist discography has its own ten-item cap; the other collection
            # endpoints still accept our thirty-item controller-friendly pages.
            params = {"limit": 10 if kind == "artist" else 30, "offset": offset}
            if kind == "artist":
                params["include_groups"] = "album,single,appears_on,compilation"
            info = {}
            if kind == "playlist":
                try:
                    info = await self._playlist_info(id)
                except SpotifyError as error:
                    if error.code == "pending":
                        raise
            data = await self.spotify.request("GET", paths[kind], params)
            raw = data.get("items") or []
            context = f"spotify:{kind}:{id}" if kind in ("album", "playlist") else None
            values = []
            for index, entry in enumerate(raw):
                if not isinstance(entry, dict):
                    continue
                item = media(entry.get("item", entry.get("track")) if kind == "playlist" else entry)
                if item:
                    item["position"] = offset + index
                    if context:
                        item["contextUri"] = context
                    values.append(item)
            result = {"items": values, "next": offset + len(raw) if data.get("next") and raw else None,
                      "total": data.get("total"), "contextUri": context}
            if kind == "playlist":
                result.update(snapshotId=info.get("snapshot_id"), editable=info.get("editable", False))
            return result
        result = await self.cached(f"tracks:{kind}:{id}:{offset}", load)
        self._check_epoch(epoch)
        return result

    async def library_state(self, uris):
        epoch = self._catalog_identity()
        if not isinstance(uris, list) or not 1 <= len(uris) <= 40:
            raise SpotifyError("Choose from 1 to 40 library items.")
        values = list(dict.fromkeys(uri_value(v) for v in uris))
        async def load():
            flags = await self.spotify.request("GET", "/me/library/contains", {"uris": ",".join(values)})
            if not isinstance(flags, list) or len(flags) != len(values) or any(type(v) is not bool for v in flags):
                raise SpotifyError("Spotify returned invalid library state.")
            return {"states": dict(zip(values, flags))}
        result = await self.cached("saved:" + ",".join(values), load, 30)
        self._check_epoch(epoch)
        return result

    async def _play_body(self, value):
        if not isinstance(value, dict):
            raise SpotifyError("Choose an item to play.")
        uri = uri_value(value.get("uri"), "track|album|playlist|episode|artist")
        context = value.get("contextUri")
        if context:
            context = uri_value(context, "album|playlist")
            if not uri.startswith(("spotify:track:", "spotify:episode:")):
                raise SpotifyError("Choose a track from this collection.")
            offset = {"position": offset_value(value["position"])} if "position" in value else {"uri": uri}
            body = {"context_uri": context, "offset": offset}
        elif value.get("source") == "liked":
            position = offset_value(value.get("position"))
            epoch = self.spotify.epoch
            uris, index, pages = [], position, 0
            # Web API accepts at most 100 explicit tracks. Fetch forward across
            # UI pages so a selection near a page boundary still has a queue.
            while len(uris) < 100 and pages < 4:
                data = await self.spotify.request("GET", "/me/tracks", {"offset": index, "limit": min(50, 100 - len(uris))})
                self._check_epoch(epoch)
                raw = data.get("items") or []
                if pages == 0 and (not raw or not isinstance(raw[0], dict) or (raw[0].get("track") or {}).get("uri") != uri):
                    raise SpotifyError("Liked Songs changed. Refresh the list and choose the track again.", "conflict")
                for entry in raw:
                    item = media(entry.get("track") if isinstance(entry, dict) else None)
                    if item and item["playable"]:
                        uris.append(uri_value(item["uri"], "track"))
                index += len(raw)
                pages += 1
                if not raw or not data.get("next"):
                    break
            body = {"uris": uris[:100]}
        elif "uris" in value:
            values = value["uris"]
            if not isinstance(values, list) or not 1 <= len(values) <= 100:
                raise SpotifyError("Choose up to 100 tracks to play.")
            values = [uri_value(v, "track|episode") for v in values]
            if uri not in values:
                raise SpotifyError("The selected track is not in this queue.")
            body = {"uris": values, "offset": {"position": values.index(uri)}}
        else:
            body = {"uris": [uri]} if uri.startswith(("spotify:track:", "spotify:episode:")) else {"context_uri": uri}
        if "positionMs" in value:
            position = value["positionMs"]
            if type(position) is not int or not 0 <= position <= 604800000:
                raise SpotifyError("Invalid episode playback position.")
            body["position_ms"] = position
        return body

    async def playlist(self, action, value):
        epoch = self._catalog_identity()
        if action not in ("create", "add", "remove", "reorder") or not isinstance(value, dict):
            raise SpotifyError("Invalid playlist action.")
        async with self.catalog_lock:
            if action == "create":
                name, description, public = value.get("name"), value.get("description", ""), value.get("public", False)
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or not isinstance(description, str) or len(description) > 300 or type(public) is not bool:
                    raise SpotifyError("Enter a playlist name of 1–100 characters.")
                data = await self.spotify.request("POST", "/me/playlists", {}, {"name": name.strip(), "description": description, "public": public})
                self._check_epoch(epoch)
                if isinstance(data, dict) and data.get("id"):
                    self._remember_playlist_version(data["id"], data.get("snapshot_id"))
                self.invalidate_after_command("library", None)
                return {"item": media(data), "snapshotId": (data or {}).get("snapshot_id")}
            id = item_id(value.get("id"))
            info = await self._playlist_info(id, fresh=True)
            self._check_epoch(epoch)
            if not info["editable"]:
                raise SpotifyError("You can edit only your playlists or playlists you collaborate on.", "restricted")
            path = f"/playlists/{id}/items"
            if action == "add":
                uris = value.get("uris")
                if not isinstance(uris, list) or not 1 <= len(uris) <= 100:
                    raise SpotifyError("Choose from 1 to 100 tracks or episodes.")
                body = {"uris": [uri_value(v, "track|episode") for v in uris]}
                method = "POST"
            else:
                snapshot = value.get("snapshotId")
                if not isinstance(snapshot, str) or not 1 <= len(snapshot) <= 512 or snapshot != info.get("snapshot_id"):
                    raise SpotifyError("This playlist changed. Refresh it before editing.", "conflict")
                position = offset_value(value.get("position"))
                total = (info.get("items") or info.get("tracks") or {}).get("total")
                if type(total) is not int or position >= total:
                    raise SpotifyError("This playlist changed. Refresh it before editing.", "conflict")
                if action == "remove":
                    uri = uri_value(value.get("uri"), "track|episode")
                    # Current API documents URI removal, which removes every
                    # occurrence. Refuse ambiguous duplicate removal rather than
                    # send an undocumented positions property that may be ignored.
                    index, occurrences, selected = 0, 0, None
                    while index < total:
                        entry = await self.spotify.request("GET", path, {"offset": index, "limit": 50})
                        self._check_epoch(epoch)
                        raw = entry.get("items") or []
                        if not raw:
                            raise SpotifyError("This playlist changed. Refresh it before editing.", "conflict")
                        for relative, wrapper in enumerate(raw):
                            found = wrapper.get("item", wrapper.get("track")) if isinstance(wrapper, dict) else None
                            found_uri = found.get("uri") if isinstance(found, dict) else None
                            occurrences += found_uri == uri
                            if index + relative == position:
                                selected = found_uri
                        if occurrences > 1:
                            raise SpotifyError("This track occurs more than once. Remove the selected copy in Spotify; its API cannot safely remove just this occurrence.", "unsupported")
                        index += len(raw)
                    latest = await self._playlist_info(id, fresh=True)
                    self._check_epoch(epoch)
                    if selected != uri or occurrences != 1 or latest.get("snapshot_id") != snapshot:
                        raise SpotifyError("This playlist changed. Refresh it before editing.", "conflict")
                    body = {"items": [{"uri": uri}], "snapshot_id": snapshot}
                    method = "DELETE"
                else:
                    target = offset_value(value.get("insertBefore"))
                    if target > total:
                        raise SpotifyError("Invalid playlist position.")
                    if target in (position, position + 1):
                        return {"snapshotId": snapshot}
                    body = {"range_start": position, "range_length": 1, "insert_before": target, "snapshot_id": snapshot}
                    method = "PUT"
            self._check_epoch(epoch)
            data = await self.spotify.request(method, path, {}, body)
            self._check_epoch(epoch)
            self._remember_playlist_version(id, (data or {}).get("snapshot_id"), info.get("snapshot_id"))
            self.invalidate_after_command("library", None)
            if action == "remove":
                try:
                    await self._confirm_playlist_removal(id, uri, epoch)
                except SpotifyError as error:
                    if error.code in ("auth", "unconfirmed"):
                        raise
                    raise SpotifyError("Spotify accepted the removal, but it could not be confirmed. Refresh the playlist before trying again.",
                                       "unconfirmed", error.retry_after) from None
            return {"snapshotId": (data or {}).get("snapshot_id")}

    def timer_status(self):
        remaining = max(0, math.ceil(min(self._timer_deadline - time.monotonic(),
                                         (self._timer_ends - time.time() * 1000) / 1000))) if self._timer_deadline else 0
        return {"active": self._timer_task is not None and not self._timer_task.done(),
                "endsAt": self._timer_ends, "remaining": remaining, "error": self._timer_error}

    def _cancel_timer(self):
        if self._timer_task:
            self._timer_task.cancel()
        self._timer_task = None
        self._timer_deadline = self._timer_ends = None
        self._timer_error = None

    async def timer(self, minutes):
        self._catalog_identity()
        if type(minutes) is not int or not 0 <= minutes <= 240:
            raise SpotifyError("Choose a sleep timer from 1 to 240 minutes, or 0 to cancel.")
        previous = self._timer_task
        self._cancel_timer()
        if previous:
            await asyncio.gather(previous, return_exceptions=True)
        if minutes:
            self._timer_deadline = time.monotonic() + minutes * 60
            self._timer_ends = int(time.time() * 1000) + minutes * 60000
            self._timer_task = asyncio.create_task(self._run_timer(self.spotify.epoch))
        return self.timer_status()

    async def _run_timer(self, epoch):
        try:
            while not self._closed and epoch == self.spotify.epoch:
                remaining = self.timer_status()["remaining"]
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 1))
            if not self._closed and epoch == self.spotify.epoch:
                # No automatic retries: after a timed-out pause, the user may
                # deliberately resume playback on another device.
                self.cache.pop("playback", None)
                await self.command("pause")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._timer_error = str(error) if isinstance(error, SpotifyError) else "The sleep timer could not pause Spotify."
        finally:
            if self._timer_task is asyncio.current_task():
                self._timer_task = None
                self._timer_deadline = self._timer_ends = None

    async def _catalog_close(self):
        task = self._timer_task
        self._cancel_timer()
        if task:
            await asyncio.gather(task, return_exceptions=True)
