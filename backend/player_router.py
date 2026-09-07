"""Keep engine selection separate from Spotify account and mixer settings."""
import asyncio

from .player import Player as SoloistPlayer
from .flatpak_player import FlatpakPlayer, LOCAL as FLATPAK_DEVICE
from .local_state import LOCAL_DEVICE
from .spotify import SpotifyError


class Player:
    def __init__(self, store, directory):
        self.store = store
        self.soloist = SoloistPlayer(store, directory)
        self.flatpak = FlatpakPlayer(store)
        self.kind = 'flatpak' if store.data.get('player_engine') == 'flatpak' else 'soloist'
        self.transition = asyncio.Lock()

    @property
    def backend(self):
        return self.flatpak if self.kind == 'flatpak' else self.soloist

    @property
    def local_device(self):
        return FLATPAK_DEVICE if self.kind == 'flatpak' else LOCAL_DEVICE

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def status(self):
        return {**self.backend.status(), 'engine': self.kind}

    @property
    def error(self):
        return self.backend.error

    @error.setter
    def error(self, value):
        self.backend.error = value

    def bind_spotify(self, spotify):
        self.flatpak.api = spotify

    def observe_remote(self, state):
        if self.kind == 'flatpak':
            self.flatpak.observe_remote(state)

    async def initialize(self):
        if self.kind == 'flatpak':
            await self.flatpak.initialize()

    async def set_engine(self, kind):
        if kind not in ('soloist', 'flatpak'):
            raise SpotifyError('Choose Soloist or Spotify Flatpak.', 'invalid')
        async with self.transition:
            if kind == self.kind:
                return
            enabled = self.store.data.get('player_enabled') is True
            if self.kind == 'flatpak':
                await self.flatpak.stop(persist=False, close_app=True)
            else:
                await self.soloist.stop(persist=False)
            try:
                self.store.update(player_engine=kind)
            except (OSError, ValueError):
                if enabled:
                    await self.backend.start(persist=False)
                raise SpotifyError('Could not save the player choice.', 'player') from None
            self.kind = kind
            await self.initialize()
            status = self.status()
            if enabled and status['installed'] and status['hasKey']:
                try:
                    await self.backend.start(persist=False)
                except SpotifyError as error:
                    self.backend.error = str(error)

    async def open_app(self):
        if self.kind != 'flatpak':
            raise SpotifyError('Select Spotify Flatpak first.', 'invalid')
        await self.flatpak.start(show=True)
