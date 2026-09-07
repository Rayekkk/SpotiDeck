# Changelog

## Unreleased

- Replace the Spotify logo in Decky's plugin menu with SpotiDeck's own handheld/music icon.
- Add Spotify Flatpak as an alternative playback engine, with local MPRIS controls,
  desktop app launch/stop, verified Connect association and the existing audio balance.
- Keep Soloist as the default and recommended player, with a resource-use explanation
  beside the player selector and persistent explicit engine choices.
- Close the Spotify Flatpak app when switching back to Soloist to release its resources.
- Preserve Soloist configuration when changing engines; keep Spotify running when
  QAM closes and manage downloads and quality through the desktop app.
- Recover a verified orphaned Soloist process left by a Decky reload and wait for
  local readiness when selecting the handheld again.

## 1.0.0 - initial release

- Fix game volume control on AYANEO 3 by excluding internal ALSA loopback streams
  and tolerating unrelated PipeWire registry misses while verifying applied gains.
  Validate both slider modes on the running game and pass 325 backend tests on AYANEO.

- Add manual GitHub release checks and verified ZIP downloads in Settings, following
  Legion Go 2 Companion's update flow. Save downloads to the user's Downloads folder
  for installation through Decky, with digest, archive and version validation.

- Fix single-click device selection with automatic confirmation, stale-read protection,
  local activation and an explicit destination for sessions with no current song.
- Apply saved balance before Soloist can produce audio and after automatic selection,
  including when QAM has not been opened.
- Cancel queued seek/balance adjustments when the device changes; suspend music sliders
  during transport operations and request episode metadata for remote playback.
- Drain cache loaders after cancelled callers, clean up partial startup failures, enforce
  private ownership/permissions for existing settings and reject nonfinite JSON values.
- Correct Python 3.12+ test-server teardown and remove a scheduling-dependent test race.

- Select the owned SpotiDeck receiver automatically at startup without sending play.
  Recover a previously paired Soloist process stuck during network startup, and cancel
  automatic selection when the user chooses another device or stops the player.
- Add local Soloist playback events and control, bounded connection/process recovery,
  and protection against stale local state or commands reaching another Connect device.
- Add artists, podcasts, recent listening, personal top items, pinned collections and search history.
- Preserve playlist/album context when selecting a track, including a forward queue for Liked Songs.
- Add library save/remove states and playlist creation, addition, removal and reordering with revision checks.
- Add a backend sleep timer which keeps running while QAM is closed.
- Add phone OAuth over a temporary device-local HTTPS listener and locally generated QR codes;
  retain the existing handheld/SSH loopback login and request permissions for the new library features.
- Keep the accepted compact main panel; expose additional features in library and contextual views.

- Make startup responsive with immediate local snapshots, independent background profile/playback reads, faster readiness polling and shared retry cooldowns after a failed token refresh. Preserve account state, rate limits and transport acknowledgements.

- Adopt SpotiDeck as the plugin name across Decky, Spotify Connect, packaging, preview and documentation; migrate the earlier alpha's settings and player data during development deployment.

- Correct Balance so the centre keeps both sources at 100%; the left half lowers Spotify only and the right half lowers other audio only. Display the resulting source volumes in the plugin and preview.

- Add Spotify-style native seek and volume sliders; move track position to the player and Playback device between Search and More.
- Add separate Spotify and Other audio controls, with an optional persistent Game / Spotify balance centred by default.
- Control non-Spotify playback streams using reversible PipeWire soft gains without changing system output devices or capture volume; follow newly opened apps and restore on unload.
- Keep slider input responsive during slow requests and immediately reflect acknowledged seek/volume changes.

- Fix delayed pause/resume feedback: acknowledge playback state immediately, release controls independently of status reads, and reconcile delayed Spotify state without restoring stale transport restrictions.
- Accept successful Web API command responses with non-JSON bodies; keep strict JSON handling for data reads and OAuth token exchanges.
- Preserve profile/library caches on pause/resume and add regressions for slow reads, quick toggles, failed commands and bounded reconciliation.

- Create an original Spotify-inspired interface inside Decky.
- Use a compact player, vertical music lists and A/B/X actions.
- Keep advanced playback options, queue and saved albums under More.
- Add OAuth PKCE with a single-use loopback callback and private token storage.
- Implement current Spotify API pagination, playback controls and request backoff.
- Add optional official Soloist installation and user-owned player lifecycle.
- Add a standalone, clearly labelled visual preview with original sample artwork.
- Use Decky's `titleView` contract and preserve panel state across QAM closure with
  `alwaysRender`, while pausing hidden polling.
- Fix backend imports when Decky starts it from another working directory and
  derive the local player's runtime environment from its non-root user session.
- Load the system CA bundle when Decky's frozen Python cannot find its default CA
  file or directory, retaining verified HTTPS for requests and player downloads.
- Use a plain text input for the Soloist API key so Steam's keyboard allows clipboard
  paste. Clear the input after saving without displaying the previously stored key.
- Add a development SSH deployment helper with strict known-host checks, staging
  outside the plugin directory, worker shutdown verification and code rollback
  that preserves settings.
- Document optional Windows sign-in through a trusted SSH loopback tunnel.
- Verify installation and actual QAM rendering on Legion Go 2 (83N0), SteamOS 3.10
  and Decky Loader 3.2.8, with a UID 1000 plugin worker.
- Complete live Spotify OAuth through Windows and SSH forwarding; load the account
  profile and playlists with artwork through the production backend.
- Install official Soloist through the live Decky RPC over verified HTTPS; confirm
  version 1.3.8.17, build 20260906, successful `--version` execution and resolved libraries.
- Pass 320 backend tests on the handheld's system Python 3.14, plus 75 local UI
  tests, type checking and the production build.
- Confirm A/B/D-pad navigation and audible game/Spotify balance with the device owner.
