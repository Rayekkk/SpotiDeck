import json
import os
import tempfile
from pathlib import Path


class Store:
    """Private, atomic settings. Never store credentials in the plugin directory."""

    def __init__(self, directory):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise ValueError("The settings directory must not be a symbolic link.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            if self.directory.stat().st_uid != os.geteuid():
                raise ValueError("The settings directory belongs to another user.")
            self.directory.chmod(0o700)
        self.path = self.directory / "account.json"
        if self.path.is_symlink():
            raise ValueError("The settings file must not be a symbolic link.")
        self.data = {}
        if self.path.exists():
            if not self.path.is_file() or self.path.stat().st_size > 65536:
                raise ValueError("The settings file is invalid.")
            if os.name == "posix":
                if self.path.stat().st_uid != os.geteuid():
                    raise ValueError("The settings file belongs to another user.")
                self.path.chmod(0o600)
            with self.path.open(encoding="utf-8") as handle:
                self.data = json.load(handle, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid settings number.")))
            if not isinstance(self.data, dict):
                raise ValueError("The settings file is invalid.")

    def update(self, **values):
        updated = {**self.data, **values}
        content = json.dumps(updated, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(content) > 65536 or self.path.is_symlink() or self.directory.is_symlink():
            raise ValueError("Cannot save account settings.")
        fd, name = tempfile.mkstemp(prefix=".account-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
            self.data = updated
        finally:
            if os.path.exists(name):
                os.unlink(name)
