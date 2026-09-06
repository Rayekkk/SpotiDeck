import asyncio
import copy
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.service import Service
from backend.spotify import SpotifyError
from backend.storage import Store


class DeviceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old = {'id': 'old', 'name': 'Computer', 'is_active': True, 'volume_percent': 70}
        self.target = {'id': 'new', 'name': 'Phone', 'is_active': False, 'volume_percent': 60}
        self.read_device = copy.deepcopy(self.old)
        self.reads = 0
        self.writes = []
        self.local = None
        self.spotify = SimpleNamespace(store=Store(self.directory.name), epoch=0, connected=True,
                                       pending=None, auth_error=None, backoff_until=0)
        self.spotify.request = AsyncMock(side_effect=self.request)
        self.player = SimpleNamespace(status=lambda: {'running': True}, local_snapshot=lambda: self.local)
        self.service = Service(self.spotify, self.player)
        self.service.DEVICE_SELECTION_POLL = 0.005
        self.service.DEVICE_SELECTION_TIMEOUT = 0.15

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def request(self, method, path, params=None, body=None):
        if method == 'GET' and path == '/me/player/devices':
            return {'devices': [copy.deepcopy(self.old), copy.deepcopy(self.target)]}
        if method == 'GET' and path == '/me/player':
            self.reads += 1
            return {'device': copy.deepcopy(self.read_device), 'is_playing': False}
        if method == 'GET' and path == '/me':
            return {'id': 'listener'}
        self.writes.append((method, path, params, body))

    async def settle(self):
        await self.service._device_selection_task

    async def test_single_transfer_survives_lagging_reads_and_updates_stale_device_list(self):
        await self.service.command('transfer', 'new')
        self.assertTrue(self.service.device_selection_status()['pending'])
        snapshot = await self.service.quick_snapshot()
        self.assertIsNone(snapshot['playback'])
        with self.assertRaises(SpotifyError) as raised:
            await self.service.command('resume')
        self.assertEqual(raised.exception.code, 'pending')
        while self.reads < 2:
            await asyncio.sleep(0.005)
        self.read_device = {**self.target, 'is_active': True}
        await self.settle()
        self.assertFalse(self.service.device_selection_status()['pending'])
        self.assertIsNone(self.service.device_selection_status()['error'])
        choices = await self.service.devices()
        self.assertEqual([d['id'] for d in choices if d['active']], ['new'])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0][3], {'device_ids': ['new'], 'play': False})
        self.assertEqual((await self.service.quick_snapshot())['playback']['device']['id'], 'new')

    async def test_failed_confirmation_never_resends_transfer_or_claims_success(self):
        await self.service.command('transfer', 'new')
        await self.settle()
        self.assertEqual(len(self.writes), 1)
        status = self.service.device_selection_status()
        self.assertFalse(status['pending'])
        self.assertIn('not confirmed', status['error'])

    async def test_rejected_transfer_retains_actual_device_and_does_not_start_confirmation(self):
        original = self.request
        async def reject(method, path, params=None, body=None):
            if method == 'PUT':
                raise SpotifyError('Device refused transfer', 'restricted')
            return await original(method, path, params, body)
        self.spotify.request.side_effect = reject
        with self.assertRaises(SpotifyError):
            await self.service.command('transfer', 'new')
        self.assertIsNone(self.service.device_selection_status())
        self.assertIsNone(self.service._device_selection_task)

    async def test_later_device_selection_cancels_earlier_confirmation(self):
        await self.service.command('transfer', 'new')
        first = self.service._device_selection_task
        await self.service.command('transfer', 'old')
        await self.settle()
        self.assertTrue(first.done())
        self.assertEqual(self.service.device_selection_status()['device']['id'], 'old')
        self.assertEqual(len(self.writes), 2)

    async def test_account_change_discards_pending_confirmation(self):
        await self.service.command('transfer', 'new')
        self.spotify.epoch += 1
        await self.settle()
        self.assertIsNone(self.service.device_selection_status())
        self.assertNotIn('playback', self.service.cache)

    async def test_close_cancels_polling_without_more_api_calls(self):
        await self.service.command('transfer', 'new')
        await self.service.close()
        count = self.spotify.request.await_count
        await asyncio.sleep(0.01)
        self.assertEqual(self.spotify.request.await_count, count)

    async def test_owned_device_is_selected_locally_even_when_cloud_devices_are_offline(self):
        self.player.owned_device_id = lambda: 'local-id'
        self.player.status = lambda: {'running': True, 'ready': True}
        self.player.local_active = False
        async def activate(**kwargs):
            self.local = {'is_active': True, 'status': 'paused', 'device_name': 'SpotiDeck', 'volume': 80}
            self.player.local_active = True
        self.player.activate_local = AsyncMock(side_effect=activate)
        self.spotify.request.side_effect = SpotifyError('Offline', 'offline')
        await self.service.command('transfer', 'local-id')
        self.player.activate_local.assert_awaited_once()
        self.assertFalse(self.service.device_selection_status()['pending'])
        self.assertEqual((await self.service.quick_snapshot())['playback']['source'], 'soloist')
        self.assertEqual(self.writes, [])

    async def test_old_read_for_the_requested_id_cannot_confirm_a_new_transfer(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.request
        async def gated(method, path, params=None, body=None):
            if method == 'GET' and path == '/me/player' and not entered.is_set():
                entered.set()
                await release.wait()
                return {'device': {**self.target, 'is_active': True}}
            return await original(method, path, params, body)
        self.spotify.request.side_effect = gated
        old = asyncio.create_task(self.service._playback_load())
        await entered.wait()
        await self.service.command('transfer', 'new')
        release.set()
        self.assertIsNone(await old)
        self.assertTrue(self.service.device_selection_status()['pending'])

    async def test_empty_session_selects_destination_without_starting_or_waiting_for_music(self):
        from backend.service import playback
        self.service.cache['playback'] = (time.monotonic() + 10, playback({'device': self.old}))
        await self.service.command('transfer', 'new')
        status = self.service.device_selection_status()
        self.assertTrue(status['waitingForTrack'])
        self.assertFalse(status['pending'])
        self.assertIsNone(self.service._device_selection_task)
        snapshot = await self.service.quick_snapshot()
        self.assertEqual(snapshot['playback']['device']['id'], 'new')
        self.assertFalse(snapshot['playback']['device']['active'])
        self.assertEqual((await self.service._playback_load())['device']['id'], 'new')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0][3], {'device_ids': ['new'], 'play': False})
        # A later user-initiated play addresses exactly the chosen receiver.
        await self.service.command('play', {'uri': 'spotify:track:' + 'A' * 22})
        self.assertEqual(self.writes[-1][2]['device_id'], 'new')
        self.assertTrue(self.service.device_selection_status()['pending'])
        self.read_device = {**self.target, 'is_active': True}
        await self.settle()
        self.assertFalse(self.service.device_selection_status()['waitingForTrack'])
        self.assertIsNone(self.service.device_selection_status()['error'])

    async def test_actual_external_playback_replaces_idle_destination(self):
        from backend.service import playback
        self.service.cache['playback'] = (time.monotonic() + 10, playback({'device': self.old}))
        await self.service.command('transfer', 'new')
        self.spotify.request.return_value = None
        async def external(*args, **kwargs):
            return {'device': self.old, 'item': {'type': 'track', 'id': 'A' * 22,
                    'uri': 'spotify:track:' + 'A' * 22, 'name': 'External'}, 'is_playing': True}
        self.spotify.request.side_effect = external
        state = await self.service._playback_load()
        self.assertEqual(state['device']['id'], 'old')
        self.assertTrue(state['playing'])
        self.assertIsNone(self.service.device_selection_status())
