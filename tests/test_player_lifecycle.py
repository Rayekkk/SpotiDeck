import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.player import Player
from backend.spotify import SpotifyError
from backend.storage import Store


class Process:
    sequence = 100

    def __init__(self):
        Process.sequence += 1
        self.pid = Process.sequence
        self.returncode = None
        self.stderr = asyncio.StreamReader()
        self.done = asyncio.Event()
        self.terminated = 0

    async def wait(self):
        await self.done.wait()
        return self.returncode

    def exit(self, code):
        self.returncode = code
        self.stderr.feed_eof()
        self.done.set()

    def terminate(self):
        self.terminated += 1
        self.exit(0)

    def kill(self):
        self.exit(-9)


class Local:
    def __init__(self, endpoint, alive):
        self.endpoint, self.alive = endpoint, alive
        self.connected, self.logged_in, self.active = False, False, False
        self.task = None
        self.closed = False
        self.queue = None
        self.request = AsyncMock(return_value={"accepted": True})
        self.activate = AsyncMock(return_value={"accepted": True, "active": True})

    def start(self):
        self.task = asyncio.create_task(asyncio.Event().wait())

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def snapshot(self):
        return {"is_active": self.active} if self.connected else None


class PlayerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.player = Player(Store(Path(self.temp.name) / "settings"), Path(self.temp.name) / "runtime")
        self.player.supported = True
        self.player.RECOVERY_DELAYS = (0.01, 0.01, 0.01)
        self.player.SELECTION_READY_TIMEOUT = 0.1
        self.player.binary.parent.mkdir(parents=True)
        self.player.binary.write_bytes(b"test-player")
        self.player.store.update(soloist_key="test-personal-key")
        self.processes, self.arguments = [], []

        async def spawn(*args, **kwargs):
            process = Process()
            self.processes.append(process)
            self.arguments.append((args, kwargs))
            return process

        self.patches = [patch("backend.player.asyncio.create_subprocess_exec", side_effect=spawn),
                        patch("backend.player.player_environment", return_value={}),
                        patch("backend.player.SoloistClient", Local),
                        patch("backend.player.os.geteuid", return_value=1000, create=True)]
        for context in self.patches:
            context.start()

    async def asyncTearDown(self):
        await self.player.stop(persist=False)
        for context in reversed(self.patches):
            context.stop()
        self.temp.cleanup()

    async def until(self, condition):
        async def waiting():
            while not condition():
                await asyncio.sleep(0.002)
        await asyncio.wait_for(waiting(), 1)

    async def test_start_is_nonblocking_and_uses_private_dynamic_socket(self):
        began = time.monotonic()
        await self.player.start()
        self.assertLess(time.monotonic() - began, 0.2)
        args, options = self.arguments[0]
        self.assertEqual(args[args.index("--ws") + 1], "127.0.0.1:0")
        self.assertTrue(self.player.status()["running"])
        self.assertFalse(self.player.status()["ready"])
        self.assertTrue(self.player.status()["recovering"])
        self.assertNotIn("test-personal-key", str(self.player.status()))
        self.assertEqual(options["stderr"], asyncio.subprocess.PIPE)

    async def test_saved_balance_is_applied_before_player_can_produce_audio(self):
        for balance, expected in ((0, 0), (25, 50), (50, 100), (100, 100)):
            with self.subTest(balance=balance):
                self.player.store.update(audio_mode="balance", audio_balance=balance)
                await self.player.start()
                args, _ = self.arguments[-1]
                self.assertEqual(args[args.index("--initial-volume") + 1], str(expected))
                await self.player.stop(persist=False)

    async def test_stale_runtime_is_cleared_without_removing_pairing(self):
        state = self.player.directory / "session"
        state.mkdir(parents=True)
        for name, content in (("ws.addr", "127.0.0.1"), ("ws.port", "12345"), ("credentials", "pairing")):
            (state / name).write_text(content)
        await self.player.start()
        self.assertFalse((state / "ws.port").exists())
        self.assertFalse((state / "ws.addr").exists())
        self.assertEqual((state / "credentials").read_text(), "pairing")
        self.assertIsNone(self.player.local.endpoint())

    async def test_endpoint_requires_loopback_valid_port_and_matching_owned_process(self):
        await self.player.start()
        state, local = self.player.directory / "session", self.player.local
        (state / "ws.addr").write_text("127.0.0.1")
        (state / "ws.port").write_text("43890")
        self.assertEqual(local.endpoint(), 43890)
        (state / "ws.addr").write_text("0.0.0.0")
        self.assertIsNone(local.endpoint())
        (state / "ws.addr").write_text("127.0.0.1")
        (state / "ws.port").write_text("99999")
        self.assertIsNone(local.endpoint())
        (state / "ws.port").write_text("43890")
        (state / "soloist.pid").write_text("999999")
        self.assertIsNone(local.endpoint())
        (state / "soloist.pid").write_text(str(self.processes[0].pid))
        self.assertEqual(local.endpoint(), 43890)
        self.player._generation += 1
        self.assertIsNone(local.endpoint())

    async def test_crash_recovery_is_bounded_to_three_restarts(self):
        await self.player.start()
        for expected in (2, 3, 4):
            old_local = self.player.local
            self.processes[-1].exit(-11)
            await self.until(lambda: len(self.processes) == expected)
            self.assertTrue(old_local.closed)
        self.processes[-1].exit(-11)
        await self.player.monitor
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.processes), 4)
        self.assertFalse(self.player.status()["running"])
        self.assertFalse(self.player.recovering)
        self.assertIsNotNone(self.player.error)

    async def test_expired_or_invalid_startup_does_not_retry(self):
        for code in (10, 1, 0):
            await self.player.start()
            count = len(self.processes)
            self.processes[-1].exit(code)
            await self.player.monitor
            await asyncio.sleep(0.015)
            self.assertEqual(len(self.processes), count)
            self.assertFalse(self.player.recovering)
        self.assertTrue(self.player.store.data["player_enabled"])

    async def test_fatal_key_message_never_leaks_and_stops_late_failure_retry(self):
        await self.player.start()
        self.player._started_at -= 30
        self.processes[-1].stderr.feed_data(b"Invalid API key: test-personal-key\n")
        self.processes[-1].exit(1)
        await self.player.monitor
        self.assertEqual(len(self.processes), 1)
        self.assertFalse(self.player.recovering)
        self.assertNotIn("test-personal-key", str(self.player.status()))

    async def test_explicit_stop_cancels_pending_restart_and_cannot_resurrect_child(self):
        await self.player.start()
        local = self.player.local
        self.processes[0].exit(-11)
        await self.player.monitor
        self.assertTrue(self.player.recovering)
        await self.player.stop()
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(local.closed)
        self.assertFalse(self.player.store.data["player_enabled"])
        self.assertIsNone(self.player.recovery)
        self.assertIsNone(self.player.monitor)

    async def test_unload_stops_tasks_but_preserves_startup_preference(self):
        await self.player.start()
        local, monitor, stderr = self.player.local, self.player.monitor, self.player.stderr_task
        await self.player.stop(persist=False)
        self.assertTrue(self.player.store.data["player_enabled"])
        self.assertTrue(local.task.done())
        self.assertTrue(monitor.done())
        self.assertTrue(stderr.done())
        self.assertEqual(self.processes[0].terminated, 1)

    async def test_old_process_exit_cannot_overwrite_manual_restart_state(self):
        await self.player.start()
        old, generation, local = self.processes[0], self.player._generation, self.player.local
        old.exit(-11)
        await self.player.start()
        await self.player._watch(old, generation, local)
        self.assertIs(self.player.process, self.processes[1])
        self.assertIsNone(self.player.error)
        self.assertIsNone(self.player.recovery)
        self.assertTrue(local.closed)
        self.assertTrue(local.task.done())

    async def test_local_control_only_dispatches_when_owned_receiver_is_active(self):
        await self.player.start()
        with self.assertRaises(SpotifyError):
            await self.player.local_command("pause")
        self.player.local.request.assert_not_awaited()
        self.player.local.connected = self.player.local.logged_in = self.player.local.active = True
        await self.player.local_command("repeat", "track")
        self.assertEqual([call.args for call in self.player.local.request.await_args_list], [
            ("set_repeat_context", {"enabled": False}), ("set_repeat_track", {"enabled": True})])
        self.assertTrue(self.player.status()["ready"])
        self.assertTrue(self.player.status()["active"])

    async def test_key_change_and_install_require_stop_during_recovery(self):
        await self.player.start()
        self.player.RECOVERY_DELAYS = (10,)
        self.processes[0].exit(-11)
        await self.player.monitor
        self.assertTrue(self.player._recovery_pending())
        with self.assertRaises(SpotifyError):
            await self.player.save_key("new-personal-key")
        with self.assertRaises(SpotifyError):
            await self.player.install()
        self.assertEqual(self.player.store.data["soloist_key"], "test-personal-key")
        await self.player.stop()
        await self.player.save_key("new-personal-key")
        self.assertEqual(self.player.store.data["soloist_key"], "new-personal-key")
        self.assertIsNone(self.player.recovery)

    async def test_update_cannot_race_monitor_between_exit_and_scheduling_recovery(self):
        await self.player.start()
        self.processes[0].exit(-11)
        with self.assertRaises(SpotifyError):
            await self.player.save_key("new-personal-key")
        with self.assertRaises(SpotifyError):
            await self.player.install()
        await self.player.stop()
        self.assertEqual(len(self.processes), 1)

    async def test_queue_snapshot_cannot_leak_or_mutate_inactive_receiver_state(self):
        await self.player.start()
        self.player.local.queue = {"upcoming": [{"item": {"uri": "example"}}]}
        self.assertIsNone(self.player.local_queue())
        self.player.local.connected = self.player.local.logged_in = self.player.local.active = True
        items = self.player.local_queue()
        items[0]["item"]["uri"] = "modified"
        self.assertEqual(self.player.local_queue()[0]["item"]["uri"], "example")

    async def test_local_activation_checks_owned_process_and_preserves_inactive_commands(self):
        await self.player.start()
        with self.assertRaises(SpotifyError):
            await self.player.activate_local()
        self.player.local.connected = self.player.local.logged_in = True
        result = await self.player.activate_local(allowed=lambda: True)
        self.assertTrue(result["active"])
        self.player.local.request.assert_not_awaited()
        guard = self.player.local.activate.await_args.kwargs["allowed"]
        self.assertTrue(guard())
        await self.player.stop()
        self.assertFalse(guard())

    async def test_device_id_is_exact_and_validated_not_derived_from_display_name(self):
        await self.player.start()
        path = self.player.directory / "session" / ".device_id"
        for value in ("b957bf3b-8bca-4e8e-a1c3-cf835f91fa81", "a" * 40):
            path.write_text(value)
            self.assertEqual(self.player.owned_device_id(), value)
        for value in ("SpotiDeck", "../other", "a" * 65, "1234"):
            path.write_text(value)
            self.assertIsNone(self.player.owned_device_id())

    async def test_selection_restarts_enabled_player_and_waits_for_login(self):
        self.player.store.update(player_enabled=True)
        task = asyncio.create_task(self.player.activate_local())
        await self.until(lambda: self.player.local is not None)
        self.player.local.connected = self.player.local.logged_in = True
        self.assertTrue((await task)['active'])
        self.player.local.request.assert_not_awaited()
        self.assertEqual(len(self.processes), 1)

    async def test_selection_does_not_restart_explicitly_stopped_player(self):
        await self.player.start()
        await self.player.stop()
        with self.assertRaises(SpotifyError):
            await self.player.activate_local()
        self.assertEqual(len(self.processes), 1)

    async def test_selection_cancelled_while_waiting_cannot_activate(self):
        await self.player.start()
        allowed = True
        local = self.player.local
        task = asyncio.create_task(self.player.activate_local(allowed=lambda: allowed))
        await asyncio.sleep(0)
        allowed = False
        local.connected = local.logged_in = True
        with self.assertRaises(SpotifyError):
            await task
        local.activate.assert_not_awaited()

    async def test_start_retires_orphan_before_removing_discovery_files(self):
        state = self.player.directory / 'session'
        state.mkdir(parents=True)
        (state / 'soloist.pid').write_text('123456')
        (state / 'ws.port').write_text('43210')
        async def retire(binary, session, pid):
            self.assertEqual(pid, '123456')
            self.assertEqual((session / 'ws.port').read_text(), '43210')
            raise SpotifyError('Previous owner is still live', 'player')
        with patch('backend.player.retire_orphan', side_effect=retire):
            with self.assertRaises(SpotifyError):
                await self.player.start()
        self.assertEqual(self.processes, [])
        self.assertTrue((state / 'ws.port').exists())

    async def test_successful_owned_login_persists_pairing_once(self):
        await self.player.start()
        self.assertFalse(self.player.status()["paired"])
        with patch.object(self.player.store, "update", wraps=self.player.store.update) as update:
            self.player.local.on_login()
            self.player.local.on_login()
        update.assert_called_once_with(soloist_paired=True)
        self.assertTrue(self.player.status()["paired"])
        old_callback = self.player.local.on_login
        await self.player.stop()
        self.player.store.update(soloist_paired=False)
        old_callback()
        self.assertFalse(self.player.status()["paired"])

    async def prepare_stuck_login(self, paired=True):
        await self.player.start()
        self.player.store.update(soloist_paired=paired)
        self.player.local.connected = True
        self.player._started_at -= 20

    async def test_stuck_paired_boot_recovers_only_twice_and_preserves_saved_session(self):
        await self.prepare_stuck_login()
        session = self.player.directory / "session" / "credentials"
        session.write_text("paired-session")
        for expected in (2, 3):
            self.assertTrue(await self.player.recover_login(allowed=lambda: True))
            self.assertEqual(len(self.processes), expected)
            self.assertEqual(session.read_text(), "paired-session")
            self.player.local.connected = True
            self.player._started_at -= 20
        self.assertFalse(await self.player.recover_login(allowed=lambda: True))
        self.assertEqual(len(self.processes), 3)
        self.assertTrue(self.player.store.data["player_enabled"])

    async def test_login_recovery_requires_pairing_grace_authorization_and_nonfatal_state(self):
        await self.prepare_stuck_login(paired=False)
        self.assertFalse(await self.player.recover_login(allowed=lambda: True))
        self.player.store.update(soloist_paired=True)
        self.assertFalse(await self.player.recover_login(allowed=lambda: False))
        self.player._started_at = time.monotonic()
        self.assertFalse(await self.player.recover_login(allowed=lambda: True))
        self.player._started_at -= 20
        self.player._fatal = True
        self.assertFalse(await self.player.recover_login(allowed=lambda: True))
        self.player._fatal = False
        self.player.local.logged_in = True
        self.assertFalse(await self.player.recover_login(allowed=lambda: True))
        self.assertEqual(len(self.processes), 1)

    async def test_cancelling_login_recovery_finishes_stop_without_restart_or_orphan(self):
        await self.prepare_stuck_login()
        entered, finish = asyncio.Event(), asyncio.Event()
        close = self.player.local.close

        async def slow_close():
            entered.set()
            await finish.wait()
            await close()

        self.player.local.close = slow_close
        task = asyncio.create_task(self.player.recover_login(allowed=lambda: True))
        await entered.wait()
        task.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.processes[0].terminated, 1)
        self.assertIsNone(self.player.process)
        self.assertIsNone(self.player.monitor)
        self.assertIsNone(self.player.stderr_task)
        self.assertFalse(self.player._wanted)

    async def test_forget_session_clears_pairing_recovery_flag(self):
        await self.prepare_stuck_login()
        await self.player.forget_session()
        self.assertFalse(self.player.store.data["soloist_paired"])


if __name__ == "__main__":
    unittest.main()
