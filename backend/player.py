import asyncio
import copy
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from .network import https_opener, is_certificate_error
from .spotify import NoRedirect, SpotifyError
from .soloist import SoloistClient, command_messages
from .process_owner import retire_orphan

DOWNLOADS = {
    "x86_64": "https://soloist-builds.spotifycdn.com/soloist_release_x86_64.tar.gz",
    "aarch64": "https://soloist-builds.spotifycdn.com/soloist_release_arm64.tar.gz",
}


def player_environment():
    """Give a non-root child its desktop session, even from Decky's service."""
    environment = os.environ.copy()
    if platform.system() != "Linux":
        return environment
    import pwd

    uid = os.geteuid()
    environment["HOME"] = pwd.getpwuid(uid).pw_dir

    def owned_directory(path):
        if not path or not path.startswith("/"):
            return False
        try:
            metadata = os.stat(path)
            return stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == uid
        except (OSError, ValueError):
            return False

    if not owned_directory(environment.get("XDG_RUNTIME_DIR")):
        runtime = f"/run/user/{uid}"
        if owned_directory(runtime):
            environment["XDG_RUNTIME_DIR"] = runtime
        else:
            environment.pop("XDG_RUNTIME_DIR", None)
    return environment


def extract_binary(archive, destination):
    """Extract only a regular ELF executable; never extract archive paths or links."""
    with tarfile.open(archive, "r:gz") as bundle:
        matches = [item for item in bundle.getmembers() if Path(item.name).name == "soloist"]
        if len(matches) != 1 or not matches[0].isfile() or not 16 <= matches[0].size <= 150 * 1024 * 1024:
            raise SpotifyError("The player download did not contain a valid executable.", "player")
        member = matches[0]
        source = bundle.extractfile(member)
        if source is None:
            raise SpotifyError("Cannot read the player download.", "player")
        with source, open(destination, "wb") as output:
            magic = source.read(4)
            if magic != b"\x7fELF":
                raise SpotifyError("The player download is not a Linux executable.", "player")
            output.write(magic)
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())


class Player:
    def __init__(self, store, directory):
        self.store = store
        self.directory = Path(directory)
        self.binary = self.directory / "player" / "soloist"
        self.process = None
        self.error = None
        self.lock = asyncio.Lock()
        self.local_control_lock = asyncio.Lock()
        self.monitor = None
        self.local = None
        self.stderr_task = None
        self.recovery = None
        self.recovering = False
        self._wanted = False
        self._generation = 0
        self._attempts = 0
        self._fatal = False
        self._started_at = 0
        self._login_attempts = 0
        self.supported = platform.system() == "Linux" and platform.machine() in DOWNLOADS

    RECOVERY_DELAYS = (1, 3, 10)
    LOGIN_GRACE = 15
    LOGIN_RESTART_LIMIT = 2
    SELECTION_READY_TIMEOUT = 8

    def status(self):
        return {"installed": self.binary.is_file() and not self.binary.is_symlink(),
                "running": self.process is not None and self.process.returncode is None,
                "hasKey": bool(self.store.data.get("soloist_key")), "error": self.error,
                "paired": self.store.data.get("soloist_paired") is True,
                "supported": self.supported,
                "wsConnected": bool(self.local and self.local.connected),
                "ready": bool(self.local and self.local.connected and self.local.logged_in),
                "active": self.local_active, "recovering": self.recovering or bool(self.local and self.local.task
                    and not self.local.task.done() and not self.local.connected)}

    @property
    def local_active(self):
        return bool(self.local and self.local.connected and self.local.logged_in and self.local.active
                    and self.process and self.process.returncode is None)

    def local_snapshot(self):
        return self.local.snapshot() if self.local and self.status()["running"] else None

    def local_queue(self):
        if not self.local_active or self.local.queue is None:
            return None
        return copy.deepcopy(self.local.queue.get("upcoming", []))

    def _recovery_pending(self):
        return bool(self.recovering or (self.recovery and not self.recovery.done())
                    or (self._wanted and self.monitor and not self.monitor.done()))

    async def local_command(self, command, value=None):
        messages = command_messages(command, value)
        async with self.local_control_lock:
            local = self.local
            if not self.local_active or local is None:
                raise SpotifyError("The local player is not the active playback device.", "local_unavailable")
            for operation, fields in messages:
                await local.request(operation, fields)
        return {"accepted": True}

    async def activate_local(self, *, allowed=None):
        """Explicitly select this owned Soloist receiver without sending play."""
        async with self.local_control_lock:
            if allowed is not None and not allowed():
                raise SpotifyError("Local device selection was cancelled.", "local_cancelled")
            # Reloads or a temporary disconnection must not require a separate
            # trip to Settings. Respect an explicit Stop and never send play.
            if self.store.data.get("player_enabled") is True:
                await self.start(persist=False)
            local, process = self.local, self.process
            generation = self._generation

            def current_request():
                return (self.local is local and self.process is process and process is not None
                        and process.returncode is None and generation == self._generation
                        and (allowed is None or allowed()))

            deadline = time.monotonic() + self.SELECTION_READY_TIMEOUT
            while (local is not None and current_request() and
                   (not local.connected or not local.logged_in) and time.monotonic() < deadline):
                await asyncio.sleep(0.05)
            if local is None or not current_request() or not local.connected or not local.logged_in:
                raise SpotifyError("The local player is not ready for device selection.", "local_unavailable")
            return await local.activate(allowed=current_request)

    def owned_device_id(self):
        value = self._runtime_text(self.directory / "session" / ".device_id")
        if value and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", value):
            return value
        return None

    async def recover_login(self, *, allowed=None):
        """Retry a stuck, previously paired boot only after the caller proves connectivity."""
        async with self.lock:
            local = self.local
            if (not self._wanted or not self.status()["running"] or local is None or not local.connected
                    or local.logged_in or self._fatal or self.store.data.get("soloist_paired") is not True
                    or self._login_attempts >= self.LOGIN_RESTART_LIMIT
                    or time.monotonic() - self._started_at < self.LOGIN_GRACE
                    or (allowed is not None and not allowed())):
                return False
            self._login_attempts += 1
            cancelled = False

            async def restart():
                await self._stop_locked(persist=False)
                if cancelled or (allowed is not None and not allowed()):
                    return False
                self._wanted = True
                await self._launch(self._generation)
                return True

            transition = asyncio.create_task(restart())
            try:
                return await asyncio.shield(transition)
            except asyncio.CancelledError:
                # Finish tearing down (or owning a just-spawned child) before releasing
                # the lock. An explicit remote selection must not leave an orphan.
                cancelled = True
                await asyncio.gather(transition, return_exceptions=True)
                await self._stop_locked(persist=False)
                raise

    async def install(self):
        if not self.supported:
            raise SpotifyError("The local player requires Linux x86_64 or AArch64.", "player")
        async with self.lock:
            if self.status()["running"] or self._recovery_pending():
                raise SpotifyError("Stop the local player before updating it.", "player")
            await asyncio.to_thread(self._install)
            self.error = None

    def _install(self):
        self.binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Temporary files live beside the destination, so replacement is atomic.
        with tempfile.TemporaryDirectory(prefix=".install-", dir=self.binary.parent) as temp:
            archive = Path(temp) / "download.tar.gz"
            staged = Path(temp) / "soloist"
            try:
                request = urllib.request.Request(DOWNLOADS[platform.machine()], headers={"User-Agent": "SpotiDeck/1.0.0"})
                with https_opener(NoRedirect).open(request, timeout=30) as response, archive.open("wb") as output:
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 200 * 1024 * 1024:
                            raise SpotifyError("The player download exceeded the size limit.", "player")
                        output.write(chunk)
                extract_binary(archive, staged)
                os.chmod(staged, 0o700)
                os.replace(staged, self.binary)
            except SpotifyError:
                raise
            except (OSError, tarfile.TarError) as error:
                if is_certificate_error(error):
                    raise SpotifyError("Could not verify Spotify's secure download. Check your handheld's date and system certificates.", "player") from None
                raise SpotifyError("Could not download the player from Spotify. Check your connection and retry.", "player") from None

    async def save_key(self, key):
        if not isinstance(key, str) or not 8 <= len(key) <= 4096 or any(c.isspace() for c in key):
            raise SpotifyError("Paste a valid personal Spotify Soloist key.", "player")
        async with self.lock:
            if self.status()["running"] or self._recovery_pending():
                raise SpotifyError("Stop the player before changing its key.", "player")
            self.store.update(soloist_key=key)

    async def start(self, persist=True):
        async with self.lock:
            if not self.supported:
                raise SpotifyError("The local player requires a supported Linux handheld.", "player")
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                raise SpotifyError("The audio player must run as the desktop user. Install this plugin without the root flag.", "player")
            if self.status()["running"]:
                if self.local and self.local.task and self.local.task.done():
                    self.local.closed = False
                    self.local.start()
                return
            if not self.status()["installed"] or not self.store.data.get("soloist_key"):
                raise SpotifyError("Install the player and save your Soloist key first.", "player")
            self._wanted = True
            self._generation += 1
            self._attempts = 0
            await self._cancel_recovery()
            self.error = None
            await self._launch(self._generation)
            if persist:
                self.store.update(player_enabled=True)

    def _session_directory(self, name):
        path = self.directory / name
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.is_symlink() or path.resolve().parent != self.directory.resolve():
            raise SpotifyError("The player data directory is unsafe.", "player")
        metadata = path.stat()
        if os.name == "posix" and metadata.st_uid != os.geteuid():
            raise SpotifyError("The player data directory belongs to another user.", "player")
        if os.name == "posix":
            path.chmod(0o700)
        return path

    def _runtime_text(self, path):
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            if path.is_symlink():
                return None
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "r", encoding="ascii") as source:
                metadata = os.fstat(source.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64
                        or (os.name == "posix" and metadata.st_uid != os.geteuid())):
                    return None
                return source.read(65).strip()
        except (OSError, ValueError, UnicodeError):
            return None

    def _endpoint(self, process, generation):
        if generation != self._generation or self.process is not process or process.returncode is not None:
            return None
        state = self.directory / "session"
        address, port = self._runtime_text(state / "ws.addr"), self._runtime_text(state / "ws.port")
        if address != "127.0.0.1" or not port or not port.isascii() or not port.isdecimal():
            return None
        # A PID file, when supplied by this Soloist build, must identify our child.
        pid_path = state / "soloist.pid"
        if pid_path.exists() or pid_path.is_symlink():
            pid = self._runtime_text(pid_path)
            if pid != str(getattr(process, "pid", "")):
                return None
        value = int(port)
        return value if 1 <= value <= 65535 else None

    async def _launch(self, generation):
        # A manual Start can win the race with the previous child's exit watcher.
        # Retire its subscriptions before overwriting their references.
        if self.local:
            await self.local.close()
            self.local = None
        for task in (self.monitor, self.stderr_task):
            if task and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.monitor = self.stderr_task = None
        state, cache = self._session_directory("session"), self._session_directory("cache")
        # A killed/reloaded Decky worker can leave Soloist holding the session
        # lock. Retire that verified orphan before clearing its discovery files.
        await retire_orphan(self.binary, state, self._runtime_text(state / "soloist.pid"))
        # Old runtime discovery must never attach us to a previous child/socket.
        for name in ("ws.addr", "ws.port"):
            path = state / name
            if path.is_symlink():
                raise SpotifyError("The player endpoint file is unsafe.", "player")
            path.unlink(missing_ok=True)
        self._fatal = False
        balance = self.store.data.get("audio_balance", 50)
        initial_volume = min(100, 2 * balance) if (self.store.data.get("audio_mode") == "balance"
            and type(balance) is int and 0 <= balance <= 100) else 50
        try:
            process = await asyncio.create_subprocess_exec(str(self.binary), "--device-name", "SpotiDeck",
                "--api-key", self.store.data["soloist_key"], "--data-dir", str(state), "--cache-dir", str(cache),
                "--cache-size", "256", "--initial-volume", str(initial_volume), "--ws", "127.0.0.1:0",
                env=player_environment(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except OSError:
            raise SpotifyError("Could not start the player. Update it and check your Linux audio session.", "player") from None
        self.process = process
        self._started_at = time.monotonic()
        local = SoloistClient(lambda: self._endpoint(process, generation),
                              lambda: self._wanted and self._generation == generation and self.process is process and process.returncode is None)
        self.local = local
        def remember_login():
            if (self.local is local and self.process is process and self._generation == generation
                    and self.store.data.get("soloist_paired") is not True):
                try:
                    self.store.update(soloist_paired=True)
                except (OSError, ValueError):
                    self.error = "Could not save the local pairing status. Check free space and settings permissions."
        local.on_login = remember_login
        local.start()
        stream = getattr(process, "stderr", None)
        self.stderr_task = asyncio.create_task(self._read_stderr(stream, generation)) if stream else None
        self.monitor = asyncio.create_task(self._watch(process, generation, local, self.stderr_task))
        self.recovering = False

    async def _read_stderr(self, stream, generation):
        # Inspect bounded chunks solely to classify fatal startup errors. Never retain/log secrets.
        tail = b""
        while generation == self._generation:
            chunk = await stream.read(4096)
            if not chunk:
                return
            text = (tail + chunk).lower()
            if any(marker in text for marker in (b"invalid api key", b"invalid api-key", b"invalid key",
                                                  b"api key expired", b"api key rejected", b"unauthorized api",
                                                  b"build expired", b"build has expired")):
                self._fatal = True
            tail = text[-128:]

    @staticmethod
    def _exit_message(code):
        return "This Spotify player build has expired. Install an update in Settings." if code == 10 else "The local player stopped. Check your key and update the player in Settings."

    async def _watch(self, process, generation=None, local=None, stderr_task=None):
        code = await process.wait()
        if generation is None:
            generation = self._generation
        if self.process is not process or generation != self._generation:
            return
        if local:
            await local.close()
        if stderr_task:
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), 0.5)
            except (asyncio.TimeoutError, OSError):
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
        if self.process is not process or generation != self._generation or not self._wanted:
            return
        self.error = self._exit_message(code)
        # Early general failure commonly means an invalid key/configuration. Do not loop it.
        fatal = self._fatal or code in (0, 10) or (code == 1 and time.monotonic() - self._started_at < 10)
        if time.monotonic() - self._started_at > 300:
            self._attempts = 0
        if self._wanted and not fatal and self._attempts < len(self.RECOVERY_DELAYS):
            self.recovering = True
            self.recovery = asyncio.create_task(self._recover(generation, process))

    async def _recover(self, generation, previous):
        delay = self.RECOVERY_DELAYS[self._attempts]
        self._attempts += 1
        await asyncio.sleep(delay)
        async with self.lock:
            if not self._wanted or generation != self._generation or self.process is not previous:
                return
            try:
                await self._launch(generation)
                self.error = None
            except (SpotifyError, OSError):
                self.recovering = False
                self.error = "The local player could not recover. Start it again from Settings."

    async def _cancel_recovery(self):
        task, self.recovery = self.recovery, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.recovering = False

    async def stop(self, persist=True):
        async with self.lock:
            await self._stop_locked(persist)

    async def _stop_locked(self, persist=True):
        self._wanted = False
        self._generation += 1
        await self._cancel_recovery()
        local, self.local = self.local, None
        if local:
            await local.close()
        process, self.process = self.process, None
        if process and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            self.stderr_task = None
        if persist:
            self.store.update(player_enabled=False)
        self.error = None

    async def forget_session(self):
        await self.stop()
        async with self.lock:
            session = self.directory / "session"
            root = self.directory.resolve()
            if session.is_symlink() or session.resolve().parent != root:
                raise SpotifyError("Cannot clear the player session because its path is unsafe.", "player")
            if session.exists():
                await asyncio.to_thread(shutil.rmtree, session)
            self.store.update(soloist_key=None, soloist_paired=False)
