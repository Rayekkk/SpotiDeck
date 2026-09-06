"""Translate the owned Soloist connection without treating device names as IDs."""
import time

from .catalog import media

LOCAL_DEVICE = "local:soloist"


def local_media(entity):
    if not isinstance(entity, dict):
        return None
    decorations = entity.get("decorations") or {}
    parent = (decorations.get("parent") or {}).get("entity") or {}
    parent_decorations = parent.get("decorations") or {}
    kind, uri = entity.get("entity_type"), entity.get("uri", "")
    creators = decorations.get("creators") or []
    data = {"type": kind, "id": uri.rsplit(":", 1)[-1], "uri": uri,
            "name": (decorations.get("identity") or {}).get("name"),
            "duration_ms": (decorations.get("playback") or {}).get("duration_ms", 0),
            "explicit": "explicit" in ((decorations.get("playback") or {}).get("content_ratings") or []),
            "artists": [{"name": (((v.get("entity") or {}).get("decorations") or {}).get("identity") or {}).get("name", "")}
                        for v in creators if isinstance(v, dict)],
            "images": (decorations.get("visual_identity") or {}).get("cover", []),
            "album": {"images": (decorations.get("visual_identity") or {}).get("cover") or
                                  (parent_decorations.get("visual_identity") or {}).get("cover", [])},
            "show": {"name": (parent_decorations.get("identity") or {}).get("name")}}
    return media(data)


def local_playback(data, device_id=LOCAL_DEVICE):
    item = local_media(data.get("item"))
    now = int(time.time() * 1000)
    options = data.get("options") or {}
    available = data.get("available_actions")
    disallows = {}
    if isinstance(available, dict):
        for key, action in {"pausing": "pause", "resuming": "play", "seeking": "seek",
                            "skipping_next": "skip_next", "skipping_prev": "skip_prev",
                            "toggling_shuffle": "shuffle", "toggling_repeat_context": "set_repeat",
                            "toggling_repeat_track": "set_repeat"}.items():
            if action not in available:
                disallows[key] = True
    volume = data.get("volume")
    return {"track": item, "playing": data.get("status") == "playing",
            "progress": (data.get("position") or {}).get("position_ms", 0),
            "shuffle": bool(options.get("shuffle")), "repeat": options.get("repeat", "off"),
            "device": {"id": device_id, "name": data.get("device_name") or "SpotiDeck",
                       "type": "Computer", "active": True, "restricted": False,
                       "volume": volume, "supportsVolume": type(volume) in (int, float)},
            "disallows": disallows, "sourceTimestamp": data.get("received_at", now), "updatedAt": now,
            "source": "soloist", "revision": data.get("revision", 0)}
