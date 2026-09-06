export type MediaKind = 'track' | 'playlist' | 'album' | 'artist' | 'show' | 'episode';
export type LibraryKind = 'playlists' | 'albums' | 'liked' | 'artists' | 'shows' | 'episodes' | 'recent' | 'top_tracks' | 'top_artists' | 'pinned';
export type PageOffset = number | string;
export interface Media {
  id: string; uri: string; kind: MediaKind;
  title: string; subtitle: string; image: string | null;
  duration?: number; total?: number | null; explicit?: boolean; playable?: boolean;
  saved?: boolean; editable?: boolean; position?: number; contextUri?: string;
  resumePosition?: number; fullyPlayed?: boolean; playedAt?: string;
  source?: 'liked';
}
export interface Page { items: Media[]; next: PageOffset | null; total: number | null; snapshotId?: string; editable?: boolean; contextUri?: string }
export interface SleepTimer { active: boolean; endsAt: number | null; remaining: number; error: string | null }
export interface PlaylistMutation { name?: string; description?: string; public?: boolean; id?: string; uris?: string[]; uri?: string; position?: number; insertBefore?: number; snapshotId?: string }
export interface Connection { url: string; redirect: string; mode?: 'handheld' | 'phone'; expiresAt?: number; certificateFingerprint?: string }
export interface Device { id: string; name: string; type: string; active: boolean; restricted: boolean; volume: number | null; supportsVolume: boolean }
export interface DeviceSelection { device: Device; pending: boolean; error: string | null; waitingForTrack?: boolean }
export interface Playback {
  track: Media | null; playing: boolean; progress: number; shuffle: boolean;
  repeat: 'off' | 'context' | 'track'; device: Device | null;
  disallows: Record<string, boolean>; updatedAt: number;
  sourceTimestamp?: number | null;
}
export interface AudioState {
  mode: 'separate' | 'balance'; otherVolume: number; balance: number;
  supported: boolean; otherStreams: number; error: string | null;
}
export interface Snapshot {
  connected: boolean; connecting: boolean; authError: string | null;
  clientId: string; profile: { name: string; image: string | null } | null;
  playback: Playback | null; playbackError: string | null;
  refreshing?: boolean; playbackPending?: boolean; retryAfter?: number;
  deviceSelection?: DeviceSelection | null;
  player: { installed: boolean; running: boolean; hasKey: boolean; error: string | null; supported: boolean; wsConnected?: boolean; ready?: boolean; active?: boolean; recovering?: boolean; selecting?: boolean; selectionError?: string | null };
  audio: AudioState;
  pins?: string[]; searchHistory?: string[]; sleepTimer?: SleepTimer; needsReauthorization?: boolean;
}
export interface Client {
  updatesCheck(): Promise<UpdateCheck>;
  updatesDownload(version: string): Promise<UpdateDownload>;
  snapshot(): Promise<Snapshot>;
  library(kind: LibraryKind, offset?: PageOffset): Promise<Page>;
  search(query: string, kind: MediaKind, offset?: PageOffset): Promise<Page>;
  tracks(item: Media, offset?: PageOffset): Promise<Page>;
  libraryState(uris: string[]): Promise<{ states: Record<string, boolean> }>;
  pin(action: 'add' | 'remove', value: Media): Promise<Media[]>;
  playlist(action: 'create' | 'add' | 'remove' | 'reorder', value: PlaylistMutation): Promise<{ item?: Media; snapshotId?: string }>;
  timer(minutes: number): Promise<SleepTimer>;
  preferences(action: 'clear_search_history'): Promise<unknown>;
  devices(): Promise<Device[]>;
  queue(): Promise<Media[]>;
  command(command: string, value?: unknown): Promise<void>;
  connect(clientId: string, mode?: 'handheld' | 'phone'): Promise<Connection>;
  cancelConnect(): Promise<void>;
  disconnect(): Promise<void>;
  player(action: 'install' | 'start' | 'stop' | 'key', key?: string): Promise<void>;
  audio(action: 'mode' | 'other' | 'balance', value: string | number): Promise<void>;
}

export interface UpdateCheck {
  success: boolean; current_version: string; latest_version?: string;
  update_available: boolean; download_available: boolean; no_release?: boolean;
  release_url?: string; asset_name?: string; size?: number; checked_at?: number; error?: string;
}
export interface UpdateDownload {
  success: boolean; path?: string; version?: string; sha256?: string; error?: string;
}
