import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.process_owner import _identity, retire_orphan
from backend.spotify import SpotifyError


class IdentityTests(unittest.TestCase):
    def test_identity_requires_matching_executable_user_session_and_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            binary, session = proc / 'soloist', proc / 'session'
            args = [b'soloist', b'--data-dir', str(session).encode(),
                    b'--device-name', b'SpotiDeck', b'--ws', b'127.0.0.1:0']
            (proc / 'cmdline').write_bytes(b'\0'.join(args))
            (proc / 'status').write_text('PPid:\t1\nUid:\t1000\t1000\t1000\t1000\n')
            with patch('backend.process_owner.os.readlink', return_value=str(binary.resolve())), \
                 patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=1000)):
                self.assertEqual(_identity(proc, binary, session, 1000), 1)
                self.assertIsNone(_identity(proc, binary, session / 'other', 1000))
                self.assertIsNone(_identity(proc, binary, session, 2000))
                self.assertIsNone(_identity(proc, binary.with_name('other'), session, 1000))
                (proc / 'cmdline').write_bytes(b'soloist\0--type=crashpad-handler\0')
                self.assertIsNone(_identity(proc, binary, session, 1000))


class RetirementTests(unittest.IsolatedAsyncioTestCase):
    async def run_retirement(self, parent, ready=True):
        with patch('backend.process_owner.platform.system', return_value='Linux'), \
             patch('backend.process_owner.os.pidfd_open', return_value=42, create=True) as opened, \
             patch('backend.process_owner.os.geteuid', return_value=1000, create=True), \
             patch('backend.process_owner._identity', return_value=parent), \
             patch('backend.process_owner.signal.SIGKILL', 9, create=True), \
             patch('backend.process_owner.signal.pidfd_send_signal', create=True) as sent, \
             patch('backend.process_owner.select.select', return_value=([42] if ready else [], [], [])), \
             patch('backend.process_owner.os.close') as closed:
            try:
                await retire_orphan(Path('/plugin/soloist'), Path('/plugin/session'), '1234')
            finally:
                opened.assert_called_once_with(1234)
                closed.assert_called_once_with(42)
                if parent != 1:
                    sent.assert_not_called()
            return sent.call_args_list

    async def test_only_orphan_is_signalled_via_pinned_process_descriptor(self):
        calls = await self.run_retirement(1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], 42)

    async def test_another_live_backend_is_preserved(self):
        with self.assertRaises(SpotifyError):
            await self.run_retirement(55)

    async def test_unrelated_reused_pid_is_preserved(self):
        self.assertEqual(await self.run_retirement(None), [])

    async def test_stale_pid_is_harmless(self):
        with patch('backend.process_owner.platform.system', return_value='Linux'), \
             patch('backend.process_owner.os.pidfd_open', side_effect=ProcessLookupError, create=True), \
             patch('backend.process_owner.signal.pidfd_send_signal', create=True) as sent:
            await retire_orphan(Path('/plugin/soloist'), Path('/plugin/session'), '1234')
            sent.assert_not_called()
