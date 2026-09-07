import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.flatpak_player import FlatpakPlayer, convert_state, desktop_audio, display_environment, track_uri, unpack
from backend.local_state import local_playback
from backend.player_router import Player
from backend.spotify import SpotifyError
from backend.storage import Store

URI = 'spotify:track:' + 'a' * 22
PROPS = {'PlaybackStatus': 'Playing', 'CanControl': True, 'CanPlay': True, 'CanPause': True,
         'CanSeek': True, 'CanGoNext': True, 'Volume': 0.7, 'Position': 10000000,
         'Shuffle': True, 'LoopStatus': 'Track', 'Metadata': {
             'xesam:url': 'https://open.spotify.com/track/' + 'a' * 22,
             'xesam:title': 'Test track', 'xesam:artist': ['Artist'], 'mpris:length': 120000000,
             'mpris:trackid': '/com/spotify/track/' + 'a' * 22}}


class MappingTests(unittest.TestCase):
    def test_display_comes_from_current_steam_session_without_inheriting_stale_xauth(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder) / '123'; proc.mkdir()
            (proc / 'comm').write_text('steam\n')
            (proc / 'environ').write_bytes(b'DISPLAY=:1\0SECRET=not-for-child\0')
            self.assertEqual(display_environment(Path(folder), proc.stat().st_uid), {'DISPLAY': ':1'})
            self.assertEqual(display_environment(Path(folder), proc.stat().st_uid + 1), {})

    def test_busctl_variants_and_real_metadata_map_to_plugin_contract(self):
        self.assertEqual(unpack({'type': 'a{sv}', 'data': [{'Volume': {'type': 'd', 'data': .7}}]}), [{'Volume': .7}])
        local = convert_state(PROPS, 'Handheld', 3, time.monotonic())
        state = local_playback(local, 'local:flatpak')
        self.assertEqual(state['source'], 'flatpak')
        self.assertEqual(state['track']['uri'], URI)
        self.assertEqual(state['track']['subtitle'], 'Artist')
        self.assertEqual(state['device']['volume'], 70)
        self.assertEqual(state['repeat'], 'track')
        self.assertTrue(state['disallows']['skipping_prev'])
        self.assertLess(state['progress'], 11000)

    def test_metadata_uri_cannot_open_arbitrary_file_or_web_page(self):
        for value in (None, 'file:///etc/passwd', 'https://evil.example/track/' + 'a'*22, URI+';command'):
            self.assertIsNone(track_uri(value))
        self.assertEqual(track_uri(URI), URI)

    def test_soloist_stream_cannot_be_mistaken_for_desktop_spotify(self):
        client = {'id': 3, 'type': 'PipeWire:Interface:Client', 'info': {'props': {'application.process.binary': 'soloist'}}}
        node = {'type': 'PipeWire:Interface:Node', 'info': {'state': 'running', 'props': {
            'media.class': 'Stream/Output/Audio', 'application.name': 'Spotify', 'client.id': 3}}}
        self.assertFalse(desktop_audio([client, node]))
        client['info']['props']['application.process.binary'] = 'spotify'
        self.assertTrue(desktop_audio([client, node], running=True))
        node['info']['state'] = 'suspended'
        self.assertFalse(desktop_audio([client, node], running=True))


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.player = FlatpakPlayer(self.store)
        self.player.supported = True
        self.player.owner = ':1.55'
        self.player.instance = '123'
        self.player.props = copy.deepcopy(PROPS)
        self.player.received = time.monotonic()
        self.player.audio_present = self.player.audio_running = True
        self.player._bus = AsyncMock()
        self.player._bus_owner = AsyncMock(return_value=':1.55')
        self.player.refresh = AsyncMock()

    async def asyncTearDown(self):
        await self.player.stop(persist=False)
        self.temp.cleanup()

    def remote(self, id='handheld', **values):
        return {'playing': True, 'progress': 10000, 'track': {'uri': URI},
                'device': {'id': id, 'name': 'Handheld', 'active': True, 'type': 'Computer'}, **values}

    async def test_local_pause_is_addressed_to_verified_unique_bus_owner(self):
        await self.player.local_command('pause')
        self.assertEqual(self.player._bus.await_args.args[0], ':1.55')
        self.assertEqual(self.player._bus.await_args.args[3], 'Pause')

    async def test_successful_void_bus_reply_is_accepted_but_empty_read_is_rejected(self):
        self.player._run = AsyncMock(return_value=(0, b''))
        for method in ('Pause', 'Play', 'Next', 'Previous', 'Set', 'SetPosition', 'Raise'):
            self.assertEqual(await FlatpakPlayer._bus(self.player, ':1.55', '/', 'interface', method), [])
        with self.assertRaises(SpotifyError):
            await FlatpakPlayer._bus(self.player, ':1.55', '/', 'interface', 'GetAll')

    async def test_replaced_bus_owner_prevents_command(self):
        self.player._bus_owner.return_value = ':1.56'
        with self.assertRaises(SpotifyError):
            await self.player.local_command('pause')
        self.player._bus.assert_not_awaited()

    async def test_uncertain_write_cannot_fall_back_to_duplicate_web_command(self):
        self.player._bus.side_effect = SpotifyError('timeout', 'local_unavailable')
        with self.assertRaises(SpotifyError) as error:
            await self.player.local_command('next')
        self.assertEqual(error.exception.code, 'local_uncertain')

    async def test_accepted_write_survives_failed_readback(self):
        self.player.refresh.side_effect = SpotifyError('read failed', 'local_unavailable')
        self.assertTrue((await self.player.local_command('pause'))['accepted'])
        self.assertIsNone(self.player.owner)

    async def test_volume_and_position_use_correct_units(self):
        await self.player.local_command('volume', 40)
        self.assertEqual(self.player._bus.await_args.args[-3:], ('Volume', 'd', '0.4'))
        await self.player.local_command('seek', 15000)
        self.assertEqual(self.player._bus.await_args.args[-1], '15000000')

    async def test_invalid_controls_never_reach_bus(self):
        for command, value in (('volume', float('nan')), ('volume', -1), ('seek', float('inf')), ('repeat', 'anything'), ('shuffle', 1)):
            with self.assertRaises(SpotifyError):
                await self.player.local_command(command, value)
        self.player._bus.assert_not_awaited()

    async def test_queue_and_context_remain_web_api_operations(self):
        with self.assertRaises(SpotifyError) as error:
            await self.player.local_command('queue', URI)
        self.assertEqual(error.exception.code, 'local_unavailable')
        self.assertIsNone(self.player.local_queue())

    async def test_unload_keeps_app_running_but_explicit_stop_targets_only_instance(self):
        self.player._run = AsyncMock(return_value=(0, b''))
        await self.player.stop(persist=False)
        self.player._run.assert_not_awaited()
        self.player.instance = '123'
        await self.player.stop(persist=True)
        self.player._run.assert_awaited_once_with('flatpak', 'kill', '123')

    async def test_first_binding_requires_playing_matching_track_and_local_audio(self):
        self.player.audio_running = False
        self.player.observe_remote(self.remote())
        self.assertIsNone(self.player.connect_id)
        self.player.audio_running = True
        self.player.observe_remote(self.remote(progress=90000))
        self.assertIsNone(self.player.connect_id)
        self.player.observe_remote(self.remote())
        self.assertEqual(self.player.connect_id, 'handheld')
        self.assertEqual(self.store.data['flatpak_device_id'], 'handheld')

    async def test_engine_switch_closes_app_without_disabling_auto_start(self):
        self.store.update(player_enabled=True)
        self.player._run = AsyncMock(return_value=(0, b''))
        self.player.instance = '123'
        await self.player.stop(persist=False, close_app=True)
        self.player._run.assert_awaited_once_with('flatpak', 'kill', '123')
        self.assertTrue(self.store.data['player_enabled'])

    async def test_remote_transfer_cannot_rebind_desktop_to_pc(self):
        self.player.observe_remote(self.remote())
        self.player.observe_remote(self.remote('pc'))
        self.assertEqual(self.player.connect_id, 'handheld')
        self.assertFalse(self.player.local_active)

    async def test_known_paused_desktop_can_be_selected_before_it_creates_an_audio_stream(self):
        self.player.observe_remote(self.remote())
        self.player.audio_running = self.player.audio_present = False
        self.player.props['PlaybackStatus'] = 'Paused'
        self.player.observe_remote(self.remote(playing=False))
        self.assertTrue(self.player.local_active)
        self.player.observe_remote(self.remote('pc', playing=False))
        self.assertFalse(self.player.local_active)

    async def test_connect_registration_rejection_can_retry_but_uncertain_write_cannot(self):
        self.player.connect_id = 'handheld'
        self.player.audio_running = self.player.audio_present = self.player.active_hint = False
        self.player.api = type('API', (), {'request': AsyncMock()})()
        for code, expected in [('not_found', 'local_unavailable'), ('offline', 'offline')]:
            self.player.api.request.side_effect = SpotifyError('Unavailable', code)
            with self.assertRaises(SpotifyError) as caught:
                await self.player.activate_local()
            self.assertEqual(caught.exception.code, expected)

    async def test_stale_metadata_never_overrides_current_device(self):
        self.player.received -= 10
        self.assertIsNone(self.player.local_snapshot())

    async def test_activation_uses_bound_id_and_never_issues_play(self):
        self.player.observe_remote(self.remote())
        self.player.observe_remote(self.remote('pc'))
        self.player.api = type('API', (), {'request': AsyncMock()})()
        await self.player.activate_local()
        self.player.api.request.assert_awaited_once_with('PUT', '/me/player', {}, {'device_ids': ['handheld'], 'play': False})
        self.player._bus.assert_not_awaited()


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def test_new_and_legacy_installations_default_to_soloist(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'settings')
            for settings in ({}, {'soloist_paired': True}, {'player_engine': 'unknown'}):
                store.update(**settings)
                router = Player(store, Path(directory) / 'runtime')
                self.assertEqual(router.kind, 'soloist')
                self.assertIs(router.backend, router.soloist)

    async def test_explicit_engine_choice_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'settings'
            store = Store(settings)
            router = Player(store, Path(directory) / 'runtime')
            router.soloist.stop = AsyncMock()
            router.flatpak.initialize = AsyncMock()
            router.flatpak.stop = AsyncMock()
            for kind in ('flatpak', 'soloist'):
                await router.set_engine(kind)
                reloaded = Player(Store(settings), Path(directory) / 'runtime')
                self.assertEqual(reloaded.kind, kind)
                self.assertEqual(reloaded.status()['engine'], kind)

    async def test_engine_selection_preserves_soloist_key_and_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'settings')
            store.update(soloist_key='private-key', soloist_paired=True, player_enabled=True)
            router = Player(store, Path(directory) / 'runtime')
            router.soloist.stop = AsyncMock()
            router.soloist.start = AsyncMock()
            router.flatpak.initialize = AsyncMock()
            router.flatpak.start = AsyncMock()
            router.flatpak.stop = AsyncMock()
            router.flatpak.installed = True
            await router.set_engine('flatpak')
            self.assertEqual(router.local_device, 'local:flatpak')
            router.soloist.stop.assert_awaited_once_with(persist=False)
            router.flatpak.start.assert_awaited_once_with(persist=False)
            self.assertEqual(store.data['soloist_key'], 'private-key')
            self.assertTrue(store.data['soloist_paired'])
            await router.set_engine('soloist')
            self.assertEqual(router.kind, 'soloist')
            router.flatpak.stop.assert_awaited_once_with(persist=False, close_app=True)

    async def test_failed_choice_save_restores_previous_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.update(player_enabled=True)
            router = Player(store, Path(directory) / 'runtime')
            router.soloist.stop, router.soloist.start = AsyncMock(), AsyncMock()
            with patch.object(store, 'update', side_effect=OSError):
                with self.assertRaises(SpotifyError):
                    await router.set_engine('flatpak')
            self.assertEqual(router.kind, 'soloist')
            router.soloist.start.assert_awaited_once_with(persist=False)
