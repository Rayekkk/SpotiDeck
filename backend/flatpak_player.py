"""Spotify's desktop client, controlled locally through its MPRIS interface."""
import asyncio
import configparser
import json
import math
import os
import platform
import re
import shutil
import time
from pathlib import Path

from .player import player_environment
from .spotify import SpotifyError

APP = 'com.spotify.Client'
BUS = 'org.mpris.MediaPlayer2.spotify'
OBJECT = '/org/mpris/MediaPlayer2'
INTERFACE = 'org.mpris.MediaPlayer2.Player'
LOCAL = 'local:flatpak'


def display_environment(proc_root=Path('/proc'), uid=None):
    """Use Steam's current display after a Desktop/Gamescope session change."""
    uid = os.geteuid() if uid is None else uid
    keys = ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY')
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != uid or (proc / 'comm').read_text().strip() != 'steam':
                continue
            values = dict(item.split('=', 1) for item in (proc / 'environ').read_bytes().decode().split('\0') if '=' in item)
            if values.get('DISPLAY') or values.get('WAYLAND_DISPLAY'):
                return {key: values[key] for key in keys if values.get(key)}
        except (OSError, ValueError, UnicodeError):
            continue
    return {}


def unpack(value):
    if isinstance(value, dict) and set(value) == {'type', 'data'}:
        return unpack(value['data'])
    if isinstance(value, dict):
        return {key: unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack(item) for item in value]
    return value


def finite(value, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) else default


def track_uri(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'(?:spotify:|https://open\.spotify\.com/)(track|episode)[:/]([A-Za-z0-9]{22})', value)
    return 'spotify:' + match[1] + ':' + match[2] if match else None


def convert_state(props, name, revision, received):
    meta = props.get('Metadata') or {}
    uri = track_uri(meta.get('xesam:url'))
    duration = max(0, finite(meta.get('mpris:length')) / 1000)
    artists = meta.get('xesam:artist') or []
    cover = meta.get('mpris:artUrl')
    cover = [{'url': cover}] if isinstance(cover, str) and cover.startswith('https://') else []
    item = None if not uri else {'uri': uri, 'entity_type': uri.split(':')[1], 'decorations': {
        'identity': {'name': meta.get('xesam:title') or 'Spotify'}, 'playback': {'duration_ms': duration},
        'visual_identity': {'cover': cover},
        'creators': [{'entity': {'decorations': {'identity': {'name': artist}}}} for artist in artists if isinstance(artist, str)]}}
    playing = props.get('PlaybackStatus') == 'Playing'
    progress = max(0, finite(props.get('Position')) / 1000)
    if playing:
        progress += max(0, time.monotonic() - received) * 1000
    if duration:
        progress = min(duration, progress)
    available = {action: True for prop, action in (
        ('CanPause', 'pause'), ('CanPlay', 'play'), ('CanSeek', 'seek'),
        ('CanGoNext', 'skip_next'), ('CanGoPrevious', 'skip_prev')) if props.get(prop) is True}
    if 'Shuffle' in props:
        available['shuffle'] = True
    if 'LoopStatus' in props:
        available['set_repeat'] = True
    return {'item': item, 'status': 'playing' if playing else 'paused', 'is_active': True,
            'device_name': name, 'source': 'flatpak', 'revision': revision,
            'position': {'position_ms': int(progress)}, 'received_at': int(time.time() * 1000),
            'volume': round(max(0, min(1, finite(props.get('Volume')))) * 100),
            'options': {'shuffle': bool(props.get('Shuffle')),
                        'repeat': {'Track': 'track', 'Playlist': 'context'}.get(props.get('LoopStatus'), 'off')},
            'available_actions': available}


def desktop_audio(objects, running=False):
    if not isinstance(objects, list):
        return False
    clients = {o.get('id'): (o.get('info') or {}).get('props', {}) for o in objects
               if o.get('type') == 'PipeWire:Interface:Client'}
    for obj in objects:
        info = obj.get('info') or {}; props = info.get('props') or {}
        if obj.get('type') != 'PipeWire:Interface:Node' or props.get('media.class') != 'Stream/Output/Audio':
            continue
        if running and info.get('state') != 'running':
            continue
        sources = (props, clients.get(props.get('client.id'), {}))
        # Soloist also calls its stream Spotify. Require the desktop binary/app ID.
        if any(str(p.get('application.process.binary', '')).rsplit('/', 1)[-1].lower() == 'spotify' or
               str(p.get('application.id', '')).lower() == APP.lower() for p in sources):
            return True
    return False


class FlatpakPlayer:
    kind = 'flatpak'
    process = None
    local = None

    def __init__(self, store):
        self.store = store
        self.supported = platform.system() == 'Linux' and bool(shutil.which('flatpak') and shutil.which('busctl'))
        self.installed = False
        self.error = None
        self.owner = None
        self.instance = None
        self.props = {}
        self.received = 0
        self.revision = 0
        self.audio_present = False
        self.audio_running = False
        self.active_hint = False
        self.remote_observed = 0
        self.remote_other = False
        self.monitor = None
        self.lock = asyncio.Lock()
        self.api = None
        self.environment = None
        saved_id = store.data.get('flatpak_device_id')
        self.connect_id = saved_id if isinstance(saved_id, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', saved_id) else None
        self.device_name = store.data.get('flatpak_device_name') or 'Spotify (Flatpak)'
        self._closed = False

    async def _run(self, *args, timeout=5, check=True):
        env = dict(self.environment or player_environment())
        env['XDG_RUNTIME_DIR'] = f'/run/user/{os.geteuid()}'
        env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + env['XDG_RUNTIME_DIR'] + '/bus'
        # Decky's frozen runtime must not supply libraries to system utilities.
        env.pop('LD_LIBRARY_PATH', None); env.pop('LD_PRELOAD', None)
        process = await asyncio.create_subprocess_exec(*args, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def read(stream, limit):
            output = bytearray()
            while chunk := await stream.read(65536):
                output.extend(chunk)
                if len(output) > limit:
                    raise SpotifyError('Spotify returned an oversized desktop response.', 'player')
            return bytes(output)
        try:
            output, _, code = await asyncio.wait_for(asyncio.gather(read(process.stdout, 2 * 1024 * 1024),
                read(process.stderr, 65536), process.wait()), timeout)
            if check and code:
                raise SpotifyError('Spotify desktop is unavailable. Open the app and check that you are signed in.', 'local_unavailable')
            return code, output
        except asyncio.TimeoutError:
            raise SpotifyError('Spotify desktop did not respond in time.', 'local_unavailable') from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _bus(self, destination, obj, interface, method, *args):
        _, output = await self._run('busctl', '--user', '--timeout=3', '--json=short', 'call',
                                    destination, obj, interface, method, *args)
        if not output.strip() and method in ('Play', 'Pause', 'Next', 'Previous', 'Set', 'SetPosition', 'Raise'):
            return []  # Successful void methods have no busctl JSON output.
        try:
            return unpack(json.loads(output))
        except (ValueError, UnicodeError):
            raise SpotifyError('Spotify returned an invalid desktop response.', 'player') from None

    async def _bus_owner(self):
        data = await self._bus('org.freedesktop.DBus', '/org/freedesktop/DBus',
                               'org.freedesktop.DBus', 'GetNameOwner', 's', BUS)
        owner = data[0] if isinstance(data, list) and data else None
        if not isinstance(owner, str) or not re.fullmatch(r':\d+\.\d+', owner):
            raise SpotifyError('Cannot identify Spotify desktop.', 'local_unavailable')
        return owner

    async def _verify_owner(self, owner):
        data = await self._bus('org.freedesktop.DBus', '/org/freedesktop/DBus',
                               'org.freedesktop.DBus', 'GetConnectionUnixProcessID', 's', owner)
        pid = data[0]
        if type(pid) is not int or pid <= 1:
            raise SpotifyError('Cannot identify Spotify desktop.', 'player')
        proc = Path('/proc') / str(pid)
        parser = configparser.ConfigParser()
        try:
            if proc.stat().st_uid != os.geteuid():
                raise ValueError()
            parser.read_string((proc / 'root/.flatpak-info').read_text())
            if parser.get('Application', 'name') != APP:
                raise ValueError()
        except (OSError, ValueError, configparser.Error):
            raise SpotifyError('The Spotify media interface does not belong to the Flatpak app.', 'player') from None

    async def initialize(self):
        if not self.supported:
            return
        try:
            code, _ = await self._run('flatpak', 'info', APP, check=False)
            self.installed = code == 0
        except (SpotifyError, OSError):
            self.error = 'Could not check the Spotify Flatpak installation.'

    def status(self):
        ready = bool(self.owner and self.props.get('CanControl') and time.monotonic() - self.received < 5)
        return {'engine': self.kind, 'installed': self.installed, 'running': bool(self.instance),
                'hasKey': True, 'paired': bool(self.connect_id), 'linked': bool(self.connect_id),
                'supported': self.supported, 'ready': ready, 'wsConnected': ready,
                'active': self.local_active, 'recovering': False, 'error': self.error}

    @property
    def local_active(self):
        return bool(self.owner and self.props.get('CanControl') and time.monotonic() - self.received < 5
                    and not (self.remote_other and time.monotonic() - self.remote_observed < 5)
                    and (self.audio_running or self.active_hint))

    def local_snapshot(self):
        return convert_state(self.props, self.device_name, self.revision, self.received) if self.local_active else None

    def local_queue(self):
        return None

    def owned_device_id(self):
        return self.connect_id

    def observe_remote(self, state):
        active = (state or {}).get('device') or {}
        if not active.get('active'):
            return
        uri = track_uri((self.props.get('Metadata') or {}).get('xesam:url'))
        # Correlate the desktop's actual output with its current Connect track;
        # never bind a receiver by display name alone or by Soloist's audio.
        if (not self.connect_id and self.audio_running and self.props.get('PlaybackStatus') == 'Playing'
                and state.get('playing') and uri and ((state or {}).get('track') or {}).get('uri') == uri
                and abs(finite(self.props.get('Position')) / 1000 - finite(state.get('progress'))) < 5000
                and active.get('type') == 'Computer' and active.get('id') not in (None, LOCAL)):
            if self.connect_id != active['id']:
                self.store.update(flatpak_device_id=active['id'], flatpak_device_name=active['name'])
            self.connect_id, self.device_name = active['id'], active['name']
        self.active_hint = bool(self.connect_id and active.get('id') == self.connect_id)
        self.remote_observed = time.monotonic()
        self.remote_other = bool(self.connect_id and active.get('id') != self.connect_id)
        if self.connect_id and active.get('id') != self.connect_id:
            self.audio_present = False

    async def refresh(self):
        _, output = await self._run('flatpak', 'ps', '--columns=application,instance')
        instances = [line.split() for line in output.decode().splitlines()]
        self.instance = next((parts[1] for parts in instances if len(parts) == 2 and parts[0] == APP and parts[1].isdigit()), None)
        if not self.instance:
            self.owner, self.props, self.audio_present, self.active_hint = None, {}, False, False
            self.audio_running = False
            return
        owner = await self._bus_owner()
        if owner != self.owner:
            await self._verify_owner(owner)
            self.active_hint = False
            self.remote_other = False
        values = await self._bus(owner, OBJECT, 'org.freedesktop.DBus.Properties', 'GetAll', 's', INTERFACE)
        if not isinstance(values, list) or not values or not isinstance(values[0], dict):
            raise SpotifyError('Spotify returned invalid playback properties.', 'player')
        _, audio = await self._run('pw-dump')
        graph = json.loads(audio)
        self.audio_present = desktop_audio(graph)
        self.audio_running = desktop_audio(graph, running=True)
        self.owner, self.props, self.received = owner, values[0], time.monotonic()
        self.revision += 1
        self.error = None

    async def _watch(self):
        while not self._closed:
            try:
                async with self.lock:
                    await self.refresh()
            except (SpotifyError, OSError, ValueError) as error:
                self.owner, self.audio_present = None, False
                self.error = str(error) if isinstance(error, SpotifyError) and self.instance else None
            await asyncio.sleep(1)

    async def start(self, persist=True, show=False):
        if not self.supported:
            raise SpotifyError('Spotify Flatpak requires Flatpak and busctl on a Linux handheld.', 'player')
        await self.initialize()
        if not self.installed:
            raise SpotifyError('Install Spotify from Discover first, then start it here.', 'player')
        self._closed = False
        async with self.lock:
            try:
                await self.refresh()
            except SpotifyError:
                pass
            if not self.instance:
                env = player_environment()
                env['XDG_RUNTIME_DIR'] = f'/run/user/{os.geteuid()}'
                env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + env['XDG_RUNTIME_DIR'] + '/bus'
                display = display_environment()
                if not display:
                    _, data = await self._run('systemctl', '--user', 'show-environment', check=False)
                    display = {key: value for line in data.decode().splitlines()
                               for key, _, value in [line.partition('=')]
                               if key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY') and value}
                if not display.get('DISPLAY') and not display.get('WAYLAND_DISPLAY'):
                    raise SpotifyError('No desktop display is available. Start Steam before opening Spotify.', 'player')
                for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY'):
                    env.pop(key, None)
                env.update(display)
                env.pop('LD_LIBRARY_PATH', None); env.pop('LD_PRELOAD', None)
                # Keep Flathub's wrapper (flags/preload) but wait for its forked
                # client; otherwise the launch shell may exit before Gamescope
                # has established the application's session.
                child = await asyncio.create_subprocess_exec('flatpak', 'run', '--command=bash', APP,
                    '-c', 'source /app/bin/spotify "$@"\nwait', 'spotify', *([] if show else ['--minimized']),
                    env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
                # The Flathub wrapper forks. Do not treat its exit as an app exit.
                try:
                    await asyncio.wait_for(child.wait(), 3)
                except asyncio.TimeoutError:
                    pass
            elif show and self.owner:
                await self._bus(self.owner, OBJECT, 'org.mpris.MediaPlayer2', 'Raise')
        if not self.monitor or self.monitor.done():
            self.monitor = asyncio.create_task(self._watch())
        if persist:
            self.store.update(player_enabled=True)

    async def stop(self, persist=True, close_app=False):
        self._closed = True
        task, self.monitor = self.monitor, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if persist or close_app:
            # Explicit Stop or an engine switch closes only the observed instance.
            async with self.lock:
                try:
                    await self.refresh()
                except SpotifyError:
                    pass  # The login window may not expose MPRIS yet.
                if self.instance:
                    await self._run('flatpak', 'kill', self.instance)
        if persist:
            self.store.update(player_enabled=False)
        self.owner, self.props, self.instance = None, {}, None
        self.active_hint = self.audio_present = self.audio_running = False

    async def install(self):
        raise SpotifyError('Install or update Spotify through Discover (com.spotify.Client).', 'player')

    async def save_key(self, key):
        raise SpotifyError('Spotify Flatpak uses the account signed in to the app, not a Soloist key.', 'player')

    async def forget_session(self):
        await self.stop(persist=False)
        self.store.update(flatpak_device_id=None, flatpak_device_name=None)
        self.connect_id = None

    async def activate_local(self, allowed=None):
        if allowed is not None and not allowed():
            raise SpotifyError('Device selection was cancelled.', 'local_cancelled')
        if not self.status()['ready']:
            await self.start(persist=False)
            deadline = time.monotonic() + 8
            while not self.status()['ready'] and time.monotonic() < deadline:
                if allowed is not None and not allowed():
                    raise SpotifyError('Device selection was cancelled.', 'local_cancelled')
                await asyncio.sleep(0.1)
        if not self.status()['ready']:
            raise SpotifyError('Open Spotify and sign in before selecting this handheld.', 'local_unavailable')
        if self.local_active:
            return {'accepted': True, 'active': True}
        if not self.connect_id or not self.api:
            raise SpotifyError('Play a song on this computer in Spotify once to link its playback device.', 'player')
        if allowed is not None and not allowed():
            raise SpotifyError('Device selection was cancelled.', 'local_cancelled')
        try:
            await self.api.request('PUT', '/me/player', {}, {'device_ids': [self.connect_id], 'play': False})
        except SpotifyError as error:
            if error.code == 'not_found':
                # MPRIS starts before the desktop receiver appears in Connect.
                # A 404 is an explicit rejection, safe for the bounded startup retry.
                raise SpotifyError('Spotify is still registering this playback device.', 'local_unavailable') from None
            raise
        return {'accepted': True}

    async def local_command(self, command, value=None):
        async with self.lock:
            owner = self.owner
            if not self.local_active or not owner or await self._bus_owner() != owner:
                raise SpotifyError('Spotify desktop is no longer the selected player.', 'local_unavailable')
            if command in ('pause', 'next', 'previous') or (command == 'play' and value is None):
                method = {'pause': 'Pause', 'next': 'Next', 'previous': 'Previous', 'play': 'Play'}[command]
                await self._write_bus(owner, OBJECT, INTERFACE, method)
            elif command in ('volume', 'shuffle', 'repeat'):
                if command == 'volume' and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100:
                    prop, signature, data = 'Volume', 'd', str(value / 100)
                elif command == 'shuffle' and type(value) is bool:
                    prop, signature, data = 'Shuffle', 'b', str(value).lower()
                elif command == 'repeat' and value in ('off', 'track', 'context'):
                    prop, signature, data = 'LoopStatus', 's', {'off': 'None', 'track': 'Track', 'context': 'Playlist'}[value]
                else:
                    raise SpotifyError('Invalid desktop playback value.', 'invalid')
                await self._write_bus(owner, OBJECT, 'org.freedesktop.DBus.Properties', 'Set', 'ssv', INTERFACE, prop, signature, data)
            elif command == 'seek' and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2 ** 31 - 1:
                track = (self.props.get('Metadata') or {}).get('mpris:trackid')
                if not isinstance(track, str) or not re.fullmatch(r'/[A-Za-z0-9_/]+', track):
                    raise SpotifyError('Spotify cannot seek this item.', 'local_unavailable')
                await self._write_bus(owner, OBJECT, INTERFACE, 'SetPosition', 'ox', track, str(int(value * 1000)))
            else:
                raise SpotifyError('This operation requires the Spotify Web API.', 'local_unavailable')
            try:
                await self.refresh()
            except (SpotifyError, OSError, ValueError):
                self.owner = None  # A successful write must not be replayed because its readback failed.
        return {'accepted': True}

    async def _write_bus(self, *args):
        try:
            return await self._bus(*args)
        except (SpotifyError, OSError):
            raise SpotifyError('The desktop command could not be confirmed. Check playback before retrying.', 'local_uncertain') from None
