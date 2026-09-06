"""Reversible on-device directory migration used by deploy_live.py."""

from pathlib import Path


class DeploymentMigration:
    def __init__(self, homebrew: Path, stage: Path):
        self.homebrew = Path(homebrew)
        self.stage = Path(stage)
        self.target = self.homebrew / "plugins" / "SpotiDeck"
        self.legacy = self.homebrew / "plugins" / "Spotify"
        self.previous = self.stage / "previous-plugin"
        self.failed = self.stage / "failed-plugin"
        self.old_source = None
        self.state_moves = []
        self.saved_plugin = False
        self.moved_state = []
        self.installed = False
        self.prepared_layout = None

    @staticmethod
    def _directory(path: Path, *, required=False):
        if not path.is_absolute() or path.resolve() != path or path.is_symlink():
            raise RuntimeError(f"Unsafe deployment path: {path}")
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"Expected a directory: {path}")
        if required and not path.is_dir():
            raise RuntimeError(f"Missing deployment directory: {path}")

    def _layout(self):
        self._directory(self.homebrew, required=True)
        self._directory(self.stage, required=True)
        if self.stage == self.homebrew or self.homebrew in self.stage.parents:
            raise RuntimeError("Deployment staging must be outside homebrew")
        layout = {}
        for category in ("plugins", "settings", "data"):
            parent = self.homebrew / category
            self._directory(parent, required=True)
            for name in ("Spotify", "SpotiDeck"):
                path = parent / name
                self._directory(path)
                layout[(category, name)] = path.exists()
        for path in (self.previous, self.failed):
            self._directory(path)
            if path.exists():
                raise RuntimeError(f"Deployment backup already exists: {path}")
        old = any(exists for (_, name), exists in layout.items() if name == "Spotify")
        new = any(exists for (_, name), exists in layout.items() if name == "SpotiDeck")
        if old and new:
            raise RuntimeError("Both Spotify and SpotiDeck directories exist; refusing an ambiguous migration")
        return layout

    def prepare(self):
        """Validate the whole plan without changing any directory or service."""
        if self.changed:
            raise RuntimeError("Migration has already started")
        self.prepared_layout = self._layout()
        self.old_source = next((p for p in (self.legacy, self.target) if p.exists()), None)
        self.state_moves = [
            (self.homebrew / category / "Spotify", self.homebrew / category / "SpotiDeck")
            for category in ("settings", "data")
            if self.prepared_layout[(category, "Spotify")]
        ]

    @property
    def changed(self):
        return self.saved_plugin or bool(self.moved_state) or self.installed

    def _move(self, source: Path, destination: Path):
        # Each operation is one rename: private data and permissions are preserved.
        self._directory(source, required=True)
        self._directory(destination.parent, required=True)
        self._directory(destination)
        if destination.exists():
            raise RuntimeError(f"Refusing to overwrite deployment directory: {destination}")
        source.rename(destination)

    def install(self, incoming: Path):
        """Caller must have stopped and drained Decky before invoking this."""
        if self.prepared_layout is None or self._layout() != self.prepared_layout:
            raise RuntimeError("Deployment directories changed after preflight")
        incoming = Path(incoming)
        self._directory(incoming, required=True)
        if incoming != self.stage / "unpacked" / "SpotiDeck":
            raise RuntimeError("Incoming plugin is outside the verified staging directory")
        if self.old_source:
            self._move(self.old_source, self.previous)
            self.saved_plugin = True
        for source, destination in self.state_moves:
            self._move(source, destination)
            self.moved_state.append((source, destination))
        self._move(incoming, self.target)
        self.installed = True

    def rollback(self):
        """Restore original paths, retaining failed source outside the plugin scan."""
        errors = []
        if self.installed:
            try:
                self._move(self.target, self.failed)
                self.installed = False
            except Exception as error:
                errors.append(str(error))
        for source, destination in list(reversed(self.moved_state)):
            try:
                self._move(destination, source)
                self.moved_state.remove((source, destination))
            except Exception as error:
                errors.append(str(error))
        if self.saved_plugin:
            try:
                self._move(self.previous, self.old_source)
                self.saved_plugin = False
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("Migration rollback failed: " + "; ".join(errors))

    def summary(self):
        return {
            "previous_plugin": str(self.previous) if self.saved_plugin else None,
            "previous_plugin_folder": str(self.old_source) if self.old_source else None,
            "migrated_state": [str(destination) for _, destination in self.moved_state],
        }
