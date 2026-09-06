import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from backend.audio import AudioMixer, parse_snapshot
from backend.spotify import SpotifyError
from backend.storage import Store


def stream(node_id=10, serial=100, volumes=None, name='Game', **properties):
    channels = volumes if volumes is not None else [1.0, 0.5]
    return {'id': node_id, 'type': 'PipeWire:Interface:Node', 'info': {
        'props': {'object.serial': serial, 'media.class': 'Stream/Output/Audio',
                  'application.name': name, 'node.name': name, **properties},
        'params': {'Props': [{'channelVolumes': channels, 'softVolumes': copy.deepcopy(channels),
                              'channelMap': ['FL', 'FR'], 'volume': 1.0, 'mute': False, 'softMute': False}]}}}


class Graph:
    def __init__(self, *nodes):
        self.objects = [{'id': 0, 'type': 'PipeWire:Interface:Core', 'info': {'cookie': 999}}, *nodes]
        self.calls = []
        self.writes = []
        self.before_dump = None
        self.fail_write = None
        self.soft_modes = set()

    async def run(self, *args):
        self.calls.append(args)
        if args[0] == 'pw-dump':
            if self.before_dump:
                callback, self.before_dump = self.before_dump, None
                callback()
            return json.dumps(self.objects).encode()
        self.writes.append(args)
        if self.fail_write and self.fail_write == len(self.writes):
            raise SpotifyError('Synthetic volume write failure', 'audio')
        node = next(o for o in self.objects if o['id'] == int(args[2]))
        values = json.loads(args[4])
        node['info']['params']['Props'][0].update(values)
        key = node['id'], node['info']['props']['object.serial']
        if 'softVolumes' in values or 'softMute' in values:
            self.soft_modes.add(key)
        elif 'channelVolumes' in values or 'mute' in values:
            self.soft_modes.discard(key)
        return b''

    def values(self, node_id=10):
        node = next(o for o in self.objects if o['id'] == node_id)
        key = node['id'], node['info']['props']['object.serial']
        return node['info']['params']['Props'][0]['softVolumes' if key in self.soft_modes else 'channelVolumes']

    def channels(self, node_id=10):
        return next(o for o in self.objects if o['id'] == node_id)['info']['params']['Props'][0]['channelVolumes']


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.update(access_token='private-test-token')
        self.graph = Graph(stream())
        self.mixer = self.make_mixer()

    def make_mixer(self, owned_pid=lambda: None):
        mixer = AudioMixer(self.store, owned_pid)
        mixer.supported = True
        mixer._run = self.graph.run
        return mixer

    async def asyncTearDown(self):
        await self.mixer.close()
        self.temp.cleanup()

    async def test_default_start_and_close_make_no_audio_or_settings_writes(self):
        before = self.store.path.read_bytes()
        await self.mixer.start()
        self.assertEqual(self.mixer.status()['otherStreams'], 1)
        self.assertIsNone(self.mixer._monitor)
        await self.mixer.close()
        self.assertEqual(self.graph.writes, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    async def test_preserves_balance_original_gain_and_unrelated_properties(self):
        props = self.graph.objects[1]['info']['params']['Props'][0]
        props.update(volume=0.8, mute=True)
        await self.mixer.set_volume(50)
        self.assertEqual(self.graph.values(), [0.5, 0.25])
        self.assertEqual(self.graph.channels(), [1, 0.5])
        self.assertEqual((props['volume'], props['mute']), (0.8, True))
        self.assertTrue(props['softMute'])
        await self.mixer.set_volume(0)
        self.assertEqual(self.graph.values(), [0, 0])
        await self.mixer.set_volume(100)
        self.assertEqual(self.graph.values(), [1, 0.5])
        self.assertEqual(self.store.data['audio_restore'], [])
        self.assertEqual(self.store.data['access_token'], 'private-test-token')
        self.assertTrue(all(set(json.loads(c[4])) == {'softVolumes', 'softMute'} for c in self.graph.writes[:-1]))
        self.assertEqual(json.loads(self.graph.writes[-1][4]), {'channelVolumes': [1, 0.5]})

    async def test_spotify_client_owned_pid_and_names_are_excluded(self):
        self.graph.objects.extend([
            {'id': 20, 'type': 'PipeWire:Interface:Client', 'info': {'props': {'pipewire.sec.pid': 1234}}},
            stream(21, 121, name='Renamed player', **{'client.id': 20}),
            stream(22, 122, name='Spotify'),
            stream(23, 123, name='Other name', **{'application.process.binary': '/usr/bin/soloist'}),
            stream(24, 124, name='Flatpak', **{'application.id': 'com.spotify.Client'}),
            stream(25, 125, name='Sink', **{'media.class': 'Audio/Sink'}),
            stream(26, 126, name='Mic', **{'media.class': 'Stream/Input/Audio'}),
            stream(27, 127, name='Capture', **{'media.class': 'Audio/Source'}),
            stream(28, 128, name='SpotiDeck'),
            stream(29, 129, name='Audio stream', **{'node.name': 'SpotiDeck'}),
            stream(30, 130, name='Spotify for Decky'),
        ])
        self.mixer.owned_player_pid = lambda: 1234
        await self.mixer.set_volume(0)
        self.assertEqual([int(c[2]) for c in self.graph.writes], [10])

    async def test_later_game_is_attenuated_once_and_restored_on_unload(self):
        await self.mixer.set_volume(50)
        self.graph.objects.append(stream(11, 101, [0.8, 0.4], 'New game'))
        await self.mixer._apply(50)
        await self.mixer._apply(50)
        self.assertEqual(self.graph.values(10), [0.5, 0.25])
        self.assertEqual(self.graph.values(11), [0.4, 0.2])
        self.assertEqual(self.graph.channels(10), [1, 0.5])
        self.assertEqual(self.graph.channels(11), [0.8, 0.4])
        await self.mixer.close()
        self.assertEqual(self.graph.values(10), [1, 0.5])
        self.assertEqual(self.graph.values(11), [0.8, 0.4])
        self.assertEqual(self.store.data['audio_other_volume'], 50)

    async def test_reused_object_id_does_not_restore_an_unrelated_stream(self):
        await self.mixer.set_volume(0)
        self.graph.objects[1] = stream(10, 200, [0.7, 0.3], 'Another game')
        await self.mixer.close()
        self.assertEqual(self.graph.values(), [0.7, 0.3])
        self.assertEqual(len(self.graph.writes), 1)

    async def test_changed_core_cookie_does_not_restore_reused_serial(self):
        await self.mixer.set_volume(0)
        self.graph.objects[0]['info']['cookie'] = 1000
        self.graph.objects[1] = stream(10, 100, [0.7, 0.3], 'Another game')
        await self.mixer.close()
        self.assertEqual(self.graph.values(), [0.7, 0.3])

    async def test_write_rechecks_serial_before_addressing_transient_id(self):
        cookie, nodes = parse_snapshot(self.graph.objects)
        node = next(iter(nodes.values()))
        self.graph.before_dump = lambda: self.graph.objects[1]['info']['props'].update({'object.serial': 500})
        changed = await self.mixer._write(node, [1, 0.5], [0, 0])
        self.assertFalse(changed)
        self.assertEqual(self.graph.writes, [])

    async def test_another_soft_mixers_gain_is_preserved_during_poll_and_unload(self):
        await self.mixer.set_volume(50)
        self.graph.values()[:] = [0.7, 0.6]
        await self.mixer._apply(50)
        await self.mixer._apply(50)
        await self.mixer.close()
        self.assertEqual(self.graph.values(), [0.7, 0.6])
        self.assertEqual(len(self.graph.writes), 1)

    async def test_explicit_adjustment_reclaims_an_externally_changed_baseline(self):
        await self.mixer.set_volume(50)
        self.graph.channels()[:] = [0.8, 0.4]
        await self.mixer.set_volume(25)
        self.assertEqual(self.graph.values(), [0.2, 0.1])
        await self.mixer.close()
        self.assertEqual(self.graph.values(), [0.8, 0.4])

    async def test_recreated_game_has_original_standard_volume_after_zero_gain(self):
        await self.mixer.set_volume(0)
        self.assertEqual(self.graph.channels(), [1, 0.5])
        self.graph.objects[1] = stream(11, 200, [1, 0.5])
        self.assertEqual(self.graph.values(11), [1, 0.5])
        await self.mixer._apply(0)
        self.assertEqual(self.graph.values(11), [0, 0])
        await self.mixer.set_volume(100)
        self.assertEqual(self.graph.values(11), [1, 0.5])

    async def test_missing_stream_does_not_need_persistent_repair(self):
        await self.mixer.set_volume(0)
        self.graph.objects.pop()
        await self.mixer.set_volume(100)
        self.assertEqual(self.store.data['audio_restore'], [])
        self.graph.objects.append(stream(11, 200, [1, 0.5]))
        await self.mixer._apply(100)
        self.assertEqual(self.graph.values(11), [1, 0.5])
        self.assertEqual(self.store.data['audio_restore'], [])

    async def test_recreated_stream_with_external_volume_is_not_restored(self):
        await self.mixer.set_volume(0)
        self.graph.objects[1] = stream(11, 200, [0.2, 0.3])
        await self.mixer.close()
        self.assertEqual(self.graph.values(11), [0.2, 0.3])

    async def test_same_name_recreated_streams_keep_their_own_standard_volumes(self):
        await self.mixer.set_volume(0)
        self.graph.objects[1:] = [stream(11, 200, [0.3, 0.2]), stream(12, 201, [1, 0.5])]
        await self.mixer.close()
        self.assertEqual(self.graph.values(11), [0.3, 0.2])
        self.assertEqual(self.graph.values(12), [1, 0.5])
        self.assertEqual(len(self.graph.writes), 1)

    async def test_interrupted_session_restores_before_reapplying_saved_gain(self):
        await self.mixer.set_volume(50)
        recovered = self.make_mixer()
        await recovered.start()
        self.assertEqual(self.graph.values(), [0.5, 0.25])
        await recovered.close()
        self.assertEqual(self.graph.values(), [1, 0.5])
        self.mixer._records.clear()  # Model the old worker as already gone.

    async def test_standard_volume_changes_stay_intact_with_relative_attenuation(self):
        await self.mixer.set_volume(50)
        self.graph.channels()[:] = [0.8, 0.4]
        self.graph.soft_modes.clear()  # Normal PipeWire volume writes reset soft mode.
        await self.mixer._apply(50)
        self.assertEqual(self.graph.channels(), [0.8, 0.4])
        self.assertEqual(self.graph.values(), [0.4, 0.2])
        await self.mixer.close()
        self.assertEqual(self.graph.values(), [0.8, 0.4])

    async def test_same_value_standard_write_cannot_permanently_disable_attenuation(self):
        await self.mixer.set_volume(0)
        self.graph.soft_modes.clear()
        self.assertEqual(self.graph.values(), [1, 0.5])
        await self.mixer._apply(0)
        self.assertEqual(self.graph.values(), [0, 0])

    async def test_latest_normal_mute_is_mirrored_and_preserved_on_unload(self):
        await self.mixer.set_volume(50)
        props = self.graph.objects[1]['info']['params']['Props'][0]
        props['mute'] = True
        self.graph.soft_modes.clear()
        await self.mixer._apply(50)
        self.assertTrue(props['softMute'])
        await self.mixer.close()
        self.assertTrue(props['mute'])
        self.assertEqual(self.graph.soft_modes, set())

    async def test_partial_failure_rolls_back_successful_streams(self):
        self.graph.objects.append(stream(11, 101, [0.8, 0.4], 'Second game'))
        self.graph.fail_write = 2
        with self.assertRaises(SpotifyError):
            await self.mixer.set_volume(0)
        self.assertEqual(self.graph.values(10), [1, 0.5])
        self.assertEqual(self.graph.values(11), [0.8, 0.4])
        self.assertEqual(self.mixer.volume, 100)
        self.assertIsNotNone(self.mixer.error)

    async def test_unload_restores_remaining_streams_when_one_write_fails(self):
        self.graph.objects.append(stream(11, 101, [0.8, 0.4], 'Second game'))
        await self.mixer.set_volume(0)
        self.graph.fail_write = len(self.graph.writes) + 1
        await self.mixer.close()
        self.assertEqual(self.graph.values(10), [0, 0])
        self.assertEqual(self.graph.values(11), [0.8, 0.4])
        self.assertEqual([r['id'] for r in self.store.data['audio_restore']], [10])
        self.assertIsNotNone(self.mixer.error)
        await self.mixer.close()
        self.assertEqual(self.graph.values(10), [1, 0.5])
        self.assertEqual(self.store.data['audio_restore'], [])

    async def test_unload_restores_all_streams_even_if_settings_cannot_be_saved(self):
        self.graph.objects.append(stream(11, 101, [0.8, 0.4], 'Second game'))
        await self.mixer.set_volume(0)
        with patch.object(self.store, 'update', side_effect=OSError('disk full')):
            await self.mixer.close()
        self.assertEqual(self.graph.values(10), [1, 0.5])
        self.assertEqual(self.graph.values(11), [0.8, 0.4])
        self.assertIsNotNone(self.mixer.error)

    async def test_ledger_write_failure_prevents_unrecoverable_volume_write(self):
        with patch.object(self.store, 'update', side_effect=OSError('disk full')):
            with self.assertRaises(SpotifyError):
                await self.mixer.set_volume(0)
        self.assertEqual(self.graph.writes, [])
        self.assertEqual(self.graph.values(), [1, 0.5])

    async def test_invalid_percentages_never_reach_pipewire(self):
        for value in (-1, 101, 1.5, True, '50', None):
            with self.assertRaises(SpotifyError):
                await self.mixer.set_volume(value)
        self.assertEqual(self.graph.calls, [])

    async def test_background_monitor_handles_future_stream_and_stops(self):
        with patch('backend.audio.POLL_SECONDS', 0.01):
            await self.mixer.start()
            await self.mixer.set_volume(0)
            self.graph.objects.append(stream(11, 101, [0.8, 0.4], 'Second game'))
            for _ in range(30):
                if self.graph.values(11) == [0, 0]:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.graph.values(11), [0, 0])
            monitor = self.mixer._monitor
            await self.mixer.close()
            self.assertTrue(monitor.done())
            self.assertEqual(self.graph.values(11), [0.8, 0.4])

    async def test_unsupported_platform_returns_status_without_start_failure(self):
        self.mixer.supported = False
        await self.mixer.start()
        self.assertFalse(self.mixer.status()['supported'])
        with self.assertRaises(SpotifyError):
            await self.mixer.set_volume(50)
        self.assertEqual(self.graph.calls, [])


class AudioParsingTests(unittest.TestCase):
    def test_invalid_graph_or_missing_session_is_rejected(self):
        for value in ({}, [], [stream()]):
            with self.assertRaises(SpotifyError):
                parse_snapshot(value)

    def test_nonfinite_out_of_range_or_empty_channels_are_not_controlled(self):
        for value in ([], [float('nan')], [float('inf')], [-1], [11], [True], '1'):
            graph = Graph(stream(volumes=value))
            self.assertEqual(parse_snapshot(graph.objects)[1], {})

    def test_untrusted_or_oversized_saved_ledger_is_ignored(self):
        self.assertEqual(AudioMixer._load_records('bad'), [])
        self.assertEqual(AudioMixer._load_records([{}] * 65), [])
        self.assertEqual(AudioMixer._load_records([{'base': [1], 'last': [0], 'fingerprint': 'x' * 64}]), [])


if __name__ == '__main__':
    unittest.main()
