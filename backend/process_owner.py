"""Retire only a verified orphan from this plugin's private Soloist session."""

import asyncio
import os
import platform
import select
import signal
from pathlib import Path

from .spotify import SpotifyError


def _identity(proc, binary, session, uid):
    """Never identify a process by its display name or a PID file alone."""
    if proc.stat().st_uid != uid:
        return None
    executable = os.readlink(proc / 'exe')
    if executable not in (str(binary.resolve()), str(binary.resolve()) + ' (deleted)'):
        return None
    args = (proc / 'cmdline').read_bytes().split(b'\0')
    for flag, value in ((b'--data-dir', os.fsencode(session)),
                        (b'--device-name', b'SpotiDeck'), (b'--ws', b'127.0.0.1:0')):
        if args.count(flag) != 1 or args.index(flag) + 1 >= len(args) or args[args.index(flag) + 1] != value:
            return None
    status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
    if any(int(value) != uid for value in status.get('Uid', '').split()) or not status.get('Uid'):
        return None
    return int(status.get('PPid', '-1').strip())


async def retire_orphan(binary, session, pid_text):
    if platform.system() != 'Linux' or not pid_text or not pid_text.isascii() or not pid_text.isdecimal():
        return
    pid = int(pid_text)
    if pid <= 1:
        return
    proc = Path('/proc') / str(pid)
    descriptor = None
    try:
        # Pin the process before inspecting /proc, so PID reuse cannot redirect
        # a signal to a different application while we await shutdown.
        descriptor = os.pidfd_open(pid)
        parent = _identity(proc, binary, session, os.geteuid())
        if parent is None:
            return  # Stale PID file pointing at a different process.
        if parent != 1:
            raise SpotifyError('Another SpotiDeck backend still owns the local player. Retry after it closes.', 'player')
        for sig, timeout in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
            signal.pidfd_send_signal(descriptor, sig)
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if select.select([descriptor], [], [], 0)[0]:
                    return
                await asyncio.sleep(0.05)
        raise SpotifyError('The previous local player could not stop. Restart Decky and retry.', 'player')
    except (FileNotFoundError, ProcessLookupError):
        return
    except (OSError, ValueError, AttributeError):
        raise SpotifyError('Cannot verify the previous local player safely. Restart Decky and retry.', 'player') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
