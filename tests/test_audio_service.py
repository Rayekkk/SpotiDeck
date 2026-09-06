"""Audio/Spotify transaction behavior without an audio server or user account."""

import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.service import Service, playback
from backend.spotify import SpotifyError


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.fail_mode_save = False

    def update(self, **values):
        if self.fail_mode_save and 'audio_mode' in values:
            self.fail_mode_save = False
            raise OSError('Settings disk unavailable')
        self.data = {**self.data, **values}


class FakeMixer:
    def __init__(self, store):
        self.store = store
        self.volume = 90
        self.supported = True
        self.error = None
        self.fail_next_write = False
        self.entered = None
        self.release = None
        self.set_volume = AsyncMock(side_effect=self._set_volume)
        self.close = AsyncMock()

    def status(self):
        return {'supported': self.supported, 'otherVolume': self.volume,
                'otherStreams': 2, 'error': self.error}

    async def _set_volume(self, value):
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        if self.fail_next_write:
            self.fail_next_write = False
            raise SpotifyError('Mixer rejected the change.', 'audio')
        self.store.update(audio_other_volume=value)
        self.volume = value
        return self.status()


class AudioServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.mixer = FakeMixer(self.store)
        self.state = {
            'is_playing': False, 'progress_ms': 10000, 'timestamp': 1,
            'item': {'type': 'track', 'id': '0' * 22, 'uri': 'spotify:track:' + '0' * 22,
                     'name': 'Test track', 'duration_ms': 200000},
            'device': {'id': 'handheld', 'name': 'SpotiDeck', 'type': 'Computer',
                       'is_active': True, 'is_restricted': False,
                       'volume_percent': 55, 'supports_volume': True},
        }
        self.fail_music_write = False
        self.music_writes = []
        self.volume_targets = []
        self.device_choices = [copy.deepcopy(self.state['device']),
                               {**self.state['device'], 'id': 'phone', 'name': 'Phone', 'volume_percent': 80}]
        self.stale_transfer = False
        self.events = []
        self.spotify = SimpleNamespace(store=self.store, connected=True, pending=None,
                                       auth_error=None, epoch=0)
        self.spotify.request = AsyncMock(side_effect=self.spotify_request)
        self.player = SimpleNamespace(status=lambda: {'installed': True, 'running': True,
            'hasKey': True, 'error': None, 'supported': True})
        self.service = Service(self.spotify, self.player, self.mixer)

    async def asyncTearDown(self):
        await self.service.close()

    async def spotify_request(self, method, path, params=None, body=None):
        if method == 'GET' and path == '/me':
            return {'display_name': 'Test listener'}
        if method == 'GET' and path == '/me/player':
            return copy.deepcopy(self.state)
        if method == 'GET' and path == '/me/player/devices':
            return {'devices': copy.deepcopy(self.device_choices)}
        if method == 'PUT' and path == '/me/player':
            if not self.stale_transfer:
                self.state['device'] = copy.deepcopy(next(device for device in self.device_choices if device['id'] == body['device_ids'][0]))
            self.state['is_playing'] = False
            return None
        if method == 'PUT' and path == '/me/player/volume':
            self.assertIn(params['device_id'], ('handheld', 'phone'))
            self.volume_targets.append((params['device_id'], params['volume_percent']))
            self.music_writes.append(params['volume_percent'])
            self.events.append(('volume', params['volume_percent']))
            if self.fail_music_write:
                self.fail_music_write = False
                raise SpotifyError('Spotify volume is unavailable.', 'remote')
            if self.state['device'] and self.state['device']['id'] == params['device_id']:
                self.state['device']['volume_percent'] = params['volume_percent']
            for choice in self.device_choices:
                if choice['id'] == params['device_id']:
                    choice['volume_percent'] = params['volume_percent']
            return None
        if method == 'PUT' and path == '/me/player/pause':
            self.events.append(('pause', None))
            self.state['is_playing'] = False
            return None
        raise AssertionError(f'Unexpected Spotify request: {method} {path}')

    async def test_snapshot_defaults_and_local_status_are_available_without_login(self):
        self.spotify.connected = False
        result = await self.service.snapshot()
        self.assertEqual(result['audio'], {'mode': 'separate', 'balance': 50,
            'supported': True, 'otherVolume': 90, 'otherStreams': 2, 'error': None})
        self.spotify.request.assert_not_awaited()

    async def test_other_audio_is_independent_of_spotify_and_accepts_both_endpoints(self):
        self.spotify.connected = False
        for value in (0, 100):
            await self.service.audio('other', value)
            self.assertEqual(self.mixer.volume, value)
            self.assertEqual(self.store.data['audio_other_volume'], value)
        self.spotify.request.assert_not_awaited()

    async def test_first_balance_mode_keeps_both_full_and_persists_centre(self):
        await self.service.audio('mode', 'balance')
        self.assertEqual(self.state['device']['volume_percent'], 100)
        self.assertEqual(self.mixer.volume, 100)
        self.assertEqual(self.store.data['audio_mode'], 'balance')
        self.assertEqual(self.store.data['audio_balance'], 50)
        result = await self.service.snapshot()
        self.assertEqual(result['audio']['balance'], 50)
        self.assertEqual(result['playback']['device']['volume'], 100)

    async def test_returning_to_balance_uses_the_saved_position(self):
        self.store.update(audio_mode='separate', audio_balance=75)
        await self.service.audio('mode', 'balance')
        self.assertEqual(self.state['device']['volume_percent'], 100)
        self.assertEqual(self.mixer.volume, 50)
        self.assertEqual(self.store.data['audio_balance'], 75)

    async def test_switching_to_separate_retains_both_current_gains(self):
        self.store.update(audio_mode='balance', audio_balance=65)
        self.mixer.volume = 35
        self.state['device']['volume_percent'] = 65
        await self.service.audio('mode', 'separate')
        self.assertEqual(self.store.data['audio_mode'], 'separate')
        self.assertEqual(self.store.data['audio_balance'], 65)
        self.assertEqual(self.mixer.volume, 35)
        self.assertEqual(self.state['device']['volume_percent'], 65)
        self.mixer.set_volume.assert_not_awaited()
        self.assertEqual(self.music_writes, [])

    async def test_balance_attenuates_only_one_source_on_each_side_of_centre(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        for value, music, other in ((0, 0, 100), (25, 50, 100), (50, 100, 100),
                                    (75, 100, 50), (100, 100, 0),
                                    (49, 98, 100), (51, 100, 98)):
            with self.subTest(value=value):
                await self.service.audio('balance', value)
                self.assertEqual(self.state['device']['volume_percent'], music)
                self.assertEqual(self.mixer.volume, other)
                self.assertEqual(self.store.data['audio_balance'], value)

    async def test_rejects_invalid_gain_values_without_side_effects(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        for action in ('other', 'balance'):
            for value in (-1, 101, True, False, 10.5, '50', None):
                with self.subTest(action=action, value=value):
                    with self.assertRaises(SpotifyError):
                        await self.service.audio(action, value)
        self.mixer.set_volume.assert_not_awaited()
        self.assertEqual(self.music_writes, [])
        self.assertEqual(self.store.data, {'audio_mode': 'balance', 'audio_balance': 50})

    async def test_rejects_unknown_actions_and_modes(self):
        for action, value in (('missing', 30), ('mode', 'crossfade'), ('mode', None), ('mode', True)):
            with self.subTest(action=action, value=value):
                with self.assertRaises(SpotifyError):
                    await self.service.audio(action, value)
        self.mixer.set_volume.assert_not_awaited()
        self.assertEqual(self.music_writes, [])
        self.assertEqual(self.store.data, {})

    async def test_balance_change_requires_selected_balance_mode(self):
        with self.assertRaises(SpotifyError):
            await self.service.audio('balance', 30)
        self.mixer.set_volume.assert_not_awaited()
        self.assertEqual(self.music_writes, [])

    async def test_balance_requires_a_supported_mixer(self):
        self.mixer.supported = False
        with self.assertRaises(SpotifyError):
            await self.service.audio('mode', 'balance')
        self.assertNotEqual(self.store.data.get('audio_mode'), 'balance')
        self.assertEqual(self.music_writes, [])

    async def test_balance_requires_a_controllable_spotify_device(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        normal = copy.deepcopy(self.state['device'])
        for device in (None, {**normal, 'is_restricted': True}, {**normal, 'supports_volume': False}):
            with self.subTest(device=device):
                self.state['device'] = device
                self.service.invalidate()
                with self.assertRaises(SpotifyError):
                    await self.service.audio('balance', 75)
        self.mixer.set_volume.assert_not_awaited()
        self.assertEqual(self.music_writes, [])
        self.assertEqual(self.store.data['audio_balance'], 50)

    async def test_cannot_select_balance_mode_before_it_can_be_applied(self):
        self.state['device'] = None
        with self.assertRaises(SpotifyError):
            await self.service.audio('mode', 'balance')
        self.assertEqual(self.store.data, {})
        self.mixer.set_volume.assert_not_awaited()
        self.assertEqual(self.music_writes, [])

    async def test_separate_other_command_is_rejected_while_balance_is_selected(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        with self.assertRaises(SpotifyError):
            await self.service.audio('other', 30)
        self.mixer.set_volume.assert_not_awaited()
        self.assertEqual(self.music_writes, [])

    async def test_spotify_failure_does_not_change_other_audio_or_save_the_mode(self):
        self.fail_music_write = True
        with self.assertRaises(SpotifyError):
            await self.service.audio('mode', 'balance')
        self.assertEqual(self.state['device']['volume_percent'], 55)
        self.assertEqual(self.mixer.volume, 90)
        self.assertNotEqual(self.store.data.get('audio_mode'), 'balance')
        self.mixer.set_volume.assert_not_awaited()

    async def test_second_write_failure_restores_previous_music_and_other_gains(self):
        self.store.update(audio_mode='separate', audio_balance=50)
        self.mixer.fail_next_write = True
        with self.assertRaises(SpotifyError):
            await self.service.audio('mode', 'balance')
        self.assertEqual(self.state['device']['volume_percent'], 55)
        self.assertEqual(self.mixer.volume, 90)
        self.assertEqual(self.store.data['audio_mode'], 'separate')
        self.assertEqual(self.store.data['audio_balance'], 50)
        self.assertEqual(self.music_writes, [100, 55])

    async def test_failed_mode_save_rolls_back_both_successful_volume_writes(self):
        self.store.update(audio_mode='separate', audio_balance=50)
        self.store.fail_mode_save = True
        with self.assertRaises(SpotifyError):
            await self.service.audio('mode', 'balance')
        self.assertEqual(self.state['device']['volume_percent'], 55)
        self.assertEqual(self.mixer.volume, 90)
        self.assertEqual(self.store.data['audio_other_volume'], 90)
        self.assertEqual(self.store.data['audio_mode'], 'separate')
        self.assertEqual(self.store.data['audio_balance'], 50)

    async def test_balance_transaction_is_serialized_with_spotify_commands(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        self.mixer.entered = asyncio.Event()
        self.mixer.release = asyncio.Event()
        balance = asyncio.create_task(self.service.audio('balance', 80))
        command = None
        try:
            await asyncio.wait_for(self.mixer.entered.wait(), 1)
            command = asyncio.create_task(self.service.command('pause'))
            await asyncio.sleep(0)
            self.assertFalse(command.done())
            self.assertNotIn(('pause', None), self.events)
            self.mixer.release.set()
            await asyncio.wait_for(asyncio.gather(balance, command), 1)
            self.assertEqual(self.events, [('volume', 100), ('pause', None)])
            self.assertEqual(self.mixer.volume, 40)
        finally:
            self.mixer.release.set()
            for task in (balance, command):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (balance, command) if task), return_exceptions=True)

    async def test_local_other_volume_does_not_wait_for_a_spotify_command(self):
        async with self.service.command_lock:
            await asyncio.wait_for(self.service.audio('other', 40), 1)
        self.assertEqual(self.mixer.volume, 40)
        self.spotify.request.assert_not_awaited()

    async def test_reports_incomplete_rollback_without_saving_the_failed_mode(self):
        self.mixer.set_volume = AsyncMock(side_effect=[
            SpotifyError('Write failed.', 'audio'), SpotifyError('Restore failed.', 'audio')])
        with self.assertRaises(SpotifyError) as raised:
            await self.service.audio('mode', 'balance')
        self.assertIn('could not be restored', str(raised.exception))
        self.assertNotEqual(self.store.data.get('audio_mode'), 'balance')
        self.assertEqual(self.state['device']['volume_percent'], 55)

    async def test_audio_status_error_does_not_break_playback_snapshot(self):
        self.mixer.error = 'The local audio server is unavailable.'
        result = await self.service.snapshot()
        self.assertEqual(result['audio']['error'], self.mixer.error)
        self.assertEqual(result['playback']['device']['id'], 'handheld')
        self.assertIsNone(result['playbackError'])

    async def test_close_awaits_the_mixer_so_it_can_restore_owned_streams(self):
        await self.service.close()
        self.mixer.close.assert_awaited_once()

    async def test_saved_balance_applies_on_first_snapshot_only_and_does_not_fight_external_volume(self):
        self.store.update(audio_mode='balance', audio_balance=75)
        first = await self.service.snapshot()
        self.assertEqual(first['playback']['device']['volume'], 100)
        self.assertEqual(self.mixer.volume, 50)
        self.assertEqual(self.volume_targets, [('handheld', 100)])
        self.state['device']['volume_percent'] = 20
        self.service.cache.pop('playback', None)
        second = await self.service.snapshot()
        self.assertEqual(second['playback']['device']['volume'], 20)
        self.assertEqual(self.volume_targets, [('handheld', 100)])

    async def test_newly_observed_device_gets_saved_balance_once(self):
        self.store.update(audio_mode='balance', audio_balance=35)
        await self.service.snapshot()
        self.state['device'] = copy.deepcopy(self.device_choices[1])
        self.service.cache.pop('playback', None)
        result = await self.service.snapshot()
        self.assertEqual(result['playback']['device']['volume'], 70)
        self.assertEqual(self.volume_targets, [('handheld', 70), ('phone', 70)])
        await self.service.snapshot()
        self.assertEqual(len(self.volume_targets), 2)

    async def test_transfer_applies_saved_endpoint_directly_to_selected_device(self):
        self.store.update(audio_mode='balance', audio_balance=0)
        await self.service.snapshot()
        await self.service.command('transfer', 'phone')
        await self.service._device_selection_task
        if self.service._quick_balance_task:
            await self.service._quick_balance_task
        self.assertEqual(self.volume_targets, [('handheld', 0), ('phone', 0)])
        self.assertEqual(self.state['device']['volume_percent'], 0)
        self.assertEqual(self.mixer.volume, 100)
        result = await self.service.snapshot()
        self.assertEqual(result['playback']['device']['id'], 'phone')
        self.assertIsNone(self.service.balance_transfer)
        self.assertEqual(len(self.volume_targets), 2)

    async def test_transfer_does_not_reapply_balance_to_old_device_from_lagging_cloud(self):
        self.store.update(audio_mode='balance', audio_balance=65)
        await self.service.snapshot()
        self.stale_transfer = True
        await self.service.command('transfer', 'phone')
        await self.service.snapshot()
        self.assertEqual(self.volume_targets, [('handheld', 100)])
        self.assertTrue(self.service.device_selection_status()['pending'])

    async def test_old_generation_and_epoch_reads_cannot_apply_balance_to_a_device(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        for field in ('generation', 'epoch'):
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow_request(method, path, params=None, body=None):
                if path == '/me/player' and method == 'GET':
                    entered.set()
                    await release.wait()
                return await self.spotify_request(method, path, params, body)
            self.spotify.request.side_effect = slow_request
            self.service.cache.clear()
            read = asyncio.create_task(self.service.snapshot())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if field == 'generation':
                    self.service.invalidate()
                else:
                    self.spotify.epoch += 1
                release.set()
                await asyncio.wait_for(read, 1)
                self.assertEqual(self.volume_targets, [])
            finally:
                release.set()
                await asyncio.gather(read, return_exceptions=True)

    async def test_target_without_volume_control_reports_balance_error_without_writes(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        self.state['device']['supports_volume'] = False
        result = await self.service.snapshot()
        self.assertIn('balance cannot be applied', result['audio']['error'])
        self.assertIsNone(result['playbackError'])
        self.assertEqual(self.volume_targets, [])
        self.mixer.set_volume.assert_not_awaited()

    async def test_failed_startup_sync_keeps_playback_usable_and_bounds_retries(self):
        self.store.update(audio_mode='balance', audio_balance=50)
        self.fail_music_write = True
        result = await self.service.snapshot()
        self.assertIsNotNone(result['playback'])
        self.assertIn('volume is unavailable', result['audio']['error'])
        await self.service.snapshot()
        self.assertEqual(len(self.volume_targets), 1)
        self.service.balance_retry_at = 0
        retried = await self.service.snapshot()
        self.assertIsNone(retried['audio']['error'])
        self.assertEqual(self.volume_targets, [('handheld', 100), ('handheld', 100)])

    async def test_rollback_uses_original_device_when_new_snapshot_changes_target(self):
        newer = playback({**self.state, 'device': self.device_choices[1]})
        calls = 0
        async def fail_after_new_snapshot(value):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.service.cache['playback'] = (time.monotonic() + 10, newer)
                raise SpotifyError('Mixer unavailable.', 'audio')
        self.mixer.set_volume.side_effect = fail_after_new_snapshot
        with self.assertRaises(SpotifyError):
            await self.service.audio('mode', 'balance')
        self.assertEqual(self.volume_targets, [('handheld', 100), ('handheld', 55)])
        self.assertEqual(self.service.cache['playback'][1]['device']['id'], 'phone')

    async def test_volume_ack_survives_new_snapshot_with_no_device(self):
        async def request_with_missing_device(method, path, params=None, body=None):
            result = await self.spotify_request(method, path, params, body)
            if method == 'PUT' and path == '/me/player/volume':
                self.service.cache['playback'] = (time.monotonic() + 10, {'device': None})
            return result
        self.spotify.request.side_effect = request_with_missing_device
        await self.service.audio('mode', 'balance')
        self.assertEqual(self.volume_targets, [('handheld', 100)])
        self.assertEqual(self.service.cache['playback'][1], {'device': None})


if __name__ == '__main__':
    unittest.main()
