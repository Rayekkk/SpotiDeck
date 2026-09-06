"""A temporary per-stream gain; normal app volumes and device volume stay intact."""

import asyncio
import hashlib
import json
import math
import os
import platform
import shutil

from .player import player_environment
from .spotify import SpotifyError

MAX_RECORDS = 64
MAX_LEDGER_BYTES = 24576
POLL_SECONDS = 1.0


def _integer(value, maximum=2 ** 64 - 1):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if str(result) == str(value) and 0 <= result <= maximum else None


def _volumes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(v) or not 0 <= v <= 10 for v in value):
        return None
    return [float(v) for v in value]


def _same(left, right):
    return len(left) == len(right) and all(
        math.isclose(a, b, rel_tol=0.0001, abs_tol=0.00001) for a, b in zip(left, right))


def _key(node):
    return node['cookie'], node['serial']


def _spotify(props, client, owned_pid):
    for source in (props, client):
        if owned_pid and any(_integer(source.get(k)) == owned_pid for k in
                             ('pipewire.sec.pid', 'application.process.id')):
            return True
        binary = str(source.get('application.process.binary') or '').replace('\\', '/').rsplit('/', 1)[-1].lower()
        app_id = str(source.get('application.id') or '').lower()
        name = str(source.get('application.name') or '').strip().lower()
        node = str(source.get('node.name') or '').strip().lower()
        if binary in ('soloist', 'spotify', 'spotify.exe') or app_id in ('com.spotify.client', 'com.spotify'):
            return True
        # Keep the previous player name excluded while an existing session moves to SpotiDeck.
        if name in ('spotify', 'spotideck', 'spotify for decky') or node in ('spotify', 'spotideck', 'spotify for decky'):
            return True
    return False


def parse_snapshot(objects, owned_pid=None):
    """Only explicit output streams with a verifiable identity and volume qualify."""
    if not isinstance(objects, list) or len(objects) > 8192:
        raise SpotifyError('PipeWire returned an invalid audio graph.', 'audio')
    clients, cookie = {}, None
    for item in objects:
        if not isinstance(item, dict):
            continue
        info = item.get('info') or {}
        if item.get('type') == 'PipeWire:Interface:Core':
            cookie = _integer(info.get('cookie'), 2 ** 32 - 1)
        elif item.get('type') == 'PipeWire:Interface:Client':
            clients[item.get('id')] = info.get('props') or {}
    if cookie is None:
        raise SpotifyError('Cannot identify the desktop audio session.', 'audio')
    nodes = {}
    for item in objects:
        if not isinstance(item, dict) or item.get('type') != 'PipeWire:Interface:Node':
            continue
        info = item.get('info') or {}
        props = info.get('props') or {}
        if props.get('media.class') != 'Stream/Output/Audio':
            continue
        client = clients.get(_integer(props.get('client.id')), {})
        if _spotify(props, client, owned_pid):
            continue
        node_id, serial = _integer(item.get('id'), 2 ** 32 - 1), _integer(props.get('object.serial'))
        if node_id is None or serial is None:
            continue
        values = next((p for p in info.get('params', {}).get('Props', [])
                       if isinstance(p, dict) and _volumes(p.get('channelVolumes')) is not None), None)
        if values is None:
            continue
        volumes = _volumes(values['channelVolumes'])
        soft = _volumes(values.get('softVolumes'))
        if soft is None or len(soft) != len(volumes):
            continue
        identity = [str(props.get(k) or client.get(k) or '')[:256] for k in
                    ('application.id', 'application.name', 'application.process.binary', 'media.role', 'node.name')]
        identity.extend([len(volumes), values.get('channelMap', [])])
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        node = {'id': node_id, 'cookie': cookie, 'serial': serial, 'fingerprint': fingerprint,
                'volumes': volumes, 'soft': soft, 'mute': bool(values.get('mute')),
                'softMute': bool(values.get('softMute'))}
        nodes[_key(node)] = node
    return cookie, nodes


class AudioMixer:
    def __init__(self, store, owned_player_pid=lambda: None):
        self.store = store
        self.owned_player_pid = owned_player_pid
        saved = _integer(store.data.get('audio_other_volume'), 100)
        self.volume = 100 if saved is None else saved
        self.supported = (platform.system() == 'Linux' and getattr(os, 'geteuid', lambda: 0)() != 0 and
                          bool(shutil.which('pw-dump') and shutil.which('pw-cli')))
        self.error = None
        self.other_streams = 0
        self._records = self._load_records(store.data.get('audio_restore'))
        self._overrides = set()
        self._lock = asyncio.Lock()
        self._monitor = None
        self._started = False
        self._closed = False

    @staticmethod
    def _load_records(value):
        records = []
        if not isinstance(value, list) or len(value) > MAX_RECORDS:
            return records
        for entry in value:
            if not isinstance(entry, dict):
                continue
            channels, last = _volumes(entry.get('channels')), _volumes(entry.get('last'))
            fingerprint = entry.get('fingerprint')
            if (entry.get('kind') != 'soft' or channels is None or last is None or len(channels) != len(last) or
                    not isinstance(fingerprint, str) or len(fingerprint) != 64 or
                    any(c not in '0123456789abcdef' for c in fingerprint) or
                    type(entry.get('mute')) is not bool or type(entry.get('lastMute')) is not bool):
                continue
            ids = {name: _integer(entry.get(name)) for name in ('cookie', 'serial', 'id')}
            if any(v is None for v in ids.values()):
                continue
            records.append({**ids, 'kind': 'soft', 'fingerprint': fingerprint, 'channels': channels,
                            'last': last, 'mute': entry['mute'], 'lastMute': entry['lastMute']})
        return records

    def status(self):
        return {'supported': self.supported, 'otherVolume': self.volume,
                'otherStreams': self.other_streams, 'error': self.error}

    def _save(self, **settings):
        if len(self._records) > MAX_RECORDS or len(json.dumps(self._records).encode()) > MAX_LEDGER_BYTES:
            raise SpotifyError('Too many audio streams to control safely. Close unused audio applications and retry.', 'audio')
        try:
            self.store.update(audio_restore=self._records, **settings)
        except (OSError, ValueError):
            raise SpotifyError('Cannot save the audio restoration state. Check available storage.', 'audio') from None

    async def _run(self, *args):
        process = None

        async def read_limited(stream, limit):
            chunks, length = [], 0
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return b''.join(chunks)
                length += len(chunk)
                if length > limit:
                    raise SpotifyError('The desktop audio response exceeded its size limit.', 'audio')
                chunks.append(chunk)

        try:
            process = await asyncio.create_subprocess_exec(*args, env=player_environment(),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, errors, code = await asyncio.wait_for(asyncio.gather(
                read_limited(process.stdout, 8 * 1024 * 1024), read_limited(process.stderr, 65536), process.wait()), 4)
            if code or (args[0] == 'pw-cli' and b'error' in errors.lower()):
                raise SpotifyError('PipeWire could not change an audio stream. Retry after the application starts playing.', 'audio')
            return output
        except (OSError, asyncio.TimeoutError):
            raise SpotifyError('Cannot reach the desktop audio session. Check that PipeWire is running.', 'audio') from None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    async def _snapshot(self):
        try:
            result = json.loads(await self._run('pw-dump'))
        except (ValueError, UnicodeError):
            raise SpotifyError('PipeWire returned an invalid audio graph.', 'audio') from None
        cookie, nodes = parse_snapshot(result, self.owned_player_pid())
        self.other_streams = len(nodes)
        return cookie, nodes

    async def _write(self, node, expected, target, restore=False):
        # pw-cli takes a transient ID, so revalidate just before addressing it.
        cookie, nodes = await self._snapshot()
        current = nodes.get(_key(node))
        if cookie != node['cookie'] or not current or current['fingerprint'] != node['fingerprint']:
            return False
        if (current['id'] != node['id'] or not _same(current['soft'], expected) or
                current['softMute'] != node['softMute'] or current['mute'] != node['mute'] or
                not _same(current['volumes'], node['volumes'])):
            return False
        # A channelVolumes write selects normal channel volume/mute again. Do not
        # include softVolumes in this restore: that would re-enable soft mode.
        payload = {'channelVolumes': current['volumes']} if restore else {
            'softVolumes': target, 'softMute': current['mute']}
        await self._run('pw-cli', 'set-param', str(current['id']), 'Props',
                        json.dumps(payload, separators=(',', ':')))
        _, confirmed = await self._snapshot()
        actual = confirmed.get(_key(node))
        if actual is None:
            # WirePlumber does not persist softVolumes, so an exited stream needs
            # no fingerprint-based repair or changes to session-manager files.
            return None
        if (actual['fingerprint'] != node['fingerprint'] or
                not _same(actual['volumes'], current['volumes']) or actual['mute'] != current['mute'] or
                (not restore and (not _same(actual['soft'], target) or actual['softMute'] != current['mute']))):
            raise SpotifyError('The application did not accept its audio volume. Retry while it is playing.', 'audio')
        return True

    async def _apply(self, volume, explicit=False, restore=False):
        _, nodes = await self._snapshot()
        restore_errors = []

        def save():
            try:
                self._save()
            except SpotifyError as error:
                if not restore:
                    raise
                # A settings failure must not prevent restoring the remaining
                # streams. The previous durable entries remain safe to retry.
                restore_errors.append(error)

        if explicit:
            self._overrides.clear()
        self._overrides.intersection_update(nodes)
        active = [r for r in self._records if _key(r) in nodes and
                  nodes[_key(r)]['fingerprint'] == r['fingerprint']]
        if len(active) != len(self._records):
            self._records = active
            save()
        matches = {_key(r): r for r in self._records}
        for key, node in nodes.items():
            record = matches.get(key)
            standard_changed = record and (not _same(record['channels'], node['volumes']) or record['mute'] != node['mute'])
            soft_changed = record and (not _same(record['last'], node['soft']) or record['lastMute'] != node['softMute'])
            if soft_changed and (restore or volume == 100 or not standard_changed):
                # Another soft mixer took ownership. Never restore over its gain.
                self._records.remove(record)
                if not explicit:
                    self._overrides.add(key)
                record = None
                save()
            if key in self._overrides:
                continue
            if record is None:
                if restore or volume == 100:
                    continue
                record = {k: node[k] for k in ('id', 'cookie', 'serial', 'fingerprint')}
                record.update(kind='soft', channels=node['volumes'][:], mute=node['mute'],
                              last=node['soft'][:], lastMute=node['softMute'])
                self._records.append(record)
                try:
                    save()
                except SpotifyError:
                    self._records.remove(record)
                    raise
            resetting = restore or volume == 100
            target = node['volumes'][:] if resetting else [v * volume / 100 for v in node['volumes']]
            before = dict(record)
            if not resetting:
                record.update(last=target[:], lastMute=node['mute'], channels=node['volumes'][:], mute=node['mute'])
                if record != before:
                    save()  # Durable intent precedes every mutation, including mute.
            # Standard-volume or mute writes can disable soft mode without
            # changing the reported soft array. Reassert while attenuation is on.
            try:
                changed = await self._write(node, node['soft'], target, restore=resetting)
            except SpotifyError as error:
                if not restore:
                    raise
                # One disappearing or failing application must not leave other
                # applications attenuated on unload. Keep its entry for retry.
                restore_errors.append(error)
                continue
            if changed is False:
                record.update(before)
                save()
                continue
            if restore or volume == 100:
                self._records.remove(record)
                save()
        if restore_errors:
            raise restore_errors[0]

    def _ensure_monitor(self):
        if self._started and not self._closed and self.supported and (self.volume < 100 or self._records):
            if self._monitor is None or self._monitor.done():
                self._monitor = asyncio.create_task(self._watch())

    async def start(self):
        if self._started or self._closed:
            return
        self._started = True
        if not self.supported:
            return
        async with self._lock:
            try:
                if self._records:
                    await self._apply(100, restore=True)
                await self._apply(self.volume)
                self.error = None
            except SpotifyError as error:
                self.error = str(error)
        self._ensure_monitor()

    async def set_volume(self, volume):
        if type(volume) is not int or not 0 <= volume <= 100:
            raise SpotifyError('Choose an audio volume between 0 and 100.', 'audio')
        if not self.supported or self._closed:
            raise SpotifyError('Other audio volume requires the handheld desktop audio session.', 'audio')
        async with self._lock:
            previous = self.volume
            try:
                await self._apply(volume, explicit=True)
                self._save(audio_other_volume=volume)
                self.volume, self.error = volume, None
            except SpotifyError as error:
                self.error = str(error)
                try:
                    await self._apply(previous)
                except SpotifyError:
                    pass  # Keep the ledger so a later retry/unload can restore it.
                raise
            finally:
                self._ensure_monitor()
        return self.status()

    async def _watch(self):
        while not self._closed and (self.volume < 100 or self._records):
            await asyncio.sleep(POLL_SECONDS)
            async with self._lock:
                if self._closed:
                    return
                try:
                    await self._apply(self.volume)
                    self.error = None
                except SpotifyError as error:
                    self.error = str(error)
                    # A disconnected audio server should not cause a busy loop.
            if self.error:
                await asyncio.sleep(4)

    async def close(self):
        self._closed = True
        monitor, self._monitor = self._monitor, None
        if monitor and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        async with self._lock:
            if self.supported and self._records:
                try:
                    await self._apply(100, restore=True)
                except SpotifyError as error:
                    self.error = str(error)
