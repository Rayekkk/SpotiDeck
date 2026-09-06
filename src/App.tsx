import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Action, Group, Input, Range, openExternal } from './platform';
import { Icon } from './icons';
import type { IconName } from './icons';
import type { AudioState, Client, Connection, Device, LibraryKind, Media, MediaKind, Page, PageOffset, Playback, Snapshot } from './types';
import QRCode from 'qrcode';
import { styles } from './style';
import { balanceVolumes } from './audio';
import { UpdateSection } from './updates';

type Route = 'main' | 'playlists' | 'albums' | 'search' | 'more' | 'collection' | 'queue' | 'devices' | 'settings' | 'library' | 'browse' | 'actions' | 'add-playlist' | 'create-playlist' | 'timer';
type Run = (task: () => Promise<unknown>, done?: () => void) => Promise<void>;
const liked: Media = { id: 'liked', uri: '', kind: 'playlist', title: 'Liked Songs', subtitle: 'Your saved tracks', image: null };
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Please try again.';
const retryMilliseconds = (seconds: unknown) => typeof seconds === 'number' && Number.isFinite(seconds) ? Math.max(0, seconds * 1000) : 0;
export const formatTime = (ms: number) => { const seconds = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`; };
const playbackPosition = (playback: Playback | null, now: number) => Math.min(playback?.track?.duration ?? 0, Math.max(0, (playback?.progress ?? 0) + (playback?.playing ? Math.max(0, now - playback.updatedAt) : 0)));

function Cover({ item, className = '', small = false }: { item: Media | null; className?: string; small?: boolean }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [item?.image]);
  return <div className={`sp-cover ${small ? 'small' : ''} ${item?.id === 'liked' ? 'liked' : ''} ${className}`}>
    {item?.image && !failed ? <img src={item.image} alt="" loading="lazy" onError={() => setFailed(true)}/> : <Icon name={item?.id === 'liked' ? 'heart' : 'music'} size={small ? 22 : 40}/>}
  </div>;
}
function IconButton({ icon, label, onClick, disabled, active, children, preferredFocus }: { icon: IconName; label: string; onClick(): void; disabled?: boolean; active?: boolean; children?: ReactNode; preferredFocus?: boolean }) {
  return <Action label={label} preferredFocus={preferredFocus} className={`sp-icon-button ${active ? 'is-on' : ''}`} disabled={disabled} selected={active} onClick={onClick}><Icon name={icon}/>{children}</Action>;
}
function Empty({ icon = 'music', title, children }: { icon?: IconName; title: string; children: ReactNode }) {
  return <div className="sp-empty"><Icon name={icon} size={38}/><h2>{title}</h2>{children}</div>;
}

function usePage(key: string, loader: (offset: PageOffset) => Promise<Page>, enabled = true) {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const pending = useRef(false);
  const currentLoader = useRef(loader);
  currentLoader.current = loader;
  async function load(offset: PageOffset = 0, background = false) {
    if (pending.current) return;
    pending.current = true;
    const current = generation.current;
    if (!background) setLoading(true); setError(null);
    try {
      const next = await currentLoader.current(offset);
      if (generation.current === current) setPage(old => offset ? { ...next, items: [...(old?.items ?? []), ...next.items] } : next);
    } catch (failure) { if (generation.current === current) setError(errorText(failure)); }
    finally { if (generation.current === current) { pending.current = false; setLoading(false); } }
  }
  useEffect(() => {
    generation.current++; pending.current = false;
    setPage(null); setError(null); setLoading(false);
    if (enabled) void load();
    return () => { generation.current++; pending.current = false; };
  }, [key, enabled]);
  return { page, error, loading, load };
}
function PageStatus({ data }: { data: ReturnType<typeof usePage> }) {
  return <>{data.error && <div role="alert" className="sp-message">{data.error}<Action label="Retry" className="sp-secondary" onClick={() => void data.load(data.page?.next ?? 0)}>Try again</Action></div>}
    {data.loading && <div role="status" className="sp-loading">Loading your music…</div>}
    {!data.loading && !data.error && data.page?.next != null && <Action label="Load more" className="sp-secondary sp-load-more" onClick={() => void data.load(data.page!.next!)}>Load more</Action>}</>;
}
const isPlayable = (item: Media) => item.kind === 'track' || item.kind === 'episode';
function MediaRow({ item, current, onOpen, onQueue, onOptions, disabled, preferredFocus, label }: { item: Media; current?: string; onOpen(): void; onQueue?(): void; onOptions?(): void; disabled?: boolean; preferredFocus?: boolean; label?: string }) {
  return <Group flow="row" className="sp-media-row"><Action label={label ?? (isPlayable(item) ? `Play ${item.title}` : `Open ${item.title}`)}
    className={`sp-row ${current === item.uri ? 'current' : ''}`} onClick={onOpen}
    onSecondary={onQueue} secondaryLabel="Add to queue" preferredFocus={preferredFocus} disabled={disabled || item.playable === false}>
    <Cover item={item} small/><div className="sp-row-text"><strong>{item.title}</strong><span className="sp-muted">{item.explicit && <b className="sp-explicit">E</b>}{item.subtitle}</span>{item.kind === 'episode' && (item.fullyPlayed || !!item.resumePosition) && <span className="sp-badge">{item.fullyPlayed ? 'Played' : `Resume at ${formatTime(item.resumePosition!)}`}</span>}</div>
    {current === item.uri ? <Icon name="volume" size={16}/> : item.duration ? <span className="sp-duration">{formatTime(item.duration)}</span> : <Icon name="back" size={16} className="sp-chevron"/>}
  </Action>{onOptions && <IconButton icon="more" label={`Options for ${item.title}`} onClick={onOptions} disabled={disabled}/>}</Group>;
}
function MenuRow({ label, icon, onClick, value, disabled, selected, preferredFocus }: { label: string; icon: IconName; onClick(): void; value?: string; disabled?: boolean; selected?: boolean; preferredFocus?: boolean }) {
  return <Action label={label} className="sp-menu-row" onClick={onClick} disabled={disabled} selected={selected} preferredFocus={preferredFocus}>
    <Icon name={icon} size={20}/><span>{label}</span>{value ? <small>{value}</small> : <Icon name="back" size={16} className="sp-chevron"/>}
  </Action>;
}

export function App({ client, visible = true }: { client: Client; visible?: boolean }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [route, setRoute] = useState<Route>('main');
  const [focusTarget, setFocusTarget] = useState('');
  const [collection, setCollection] = useState<Media | null>(null);
  const [libraryKind, setLibraryKind] = useState<LibraryKind>('pinned');
  const [actionItem, setActionItem] = useState<Media | null>(null);
  const [actionContext, setActionContext] = useState<{ parent: Media; page: Page } | null>(null);
  const history = useRef<{ route: Route; focus: string; collection: Media | null }[]>([]);
  const mounted = useRef(true);
  const operation = useRef(false);
  const sliderOperations = useRef(0);
  const revision = useRef(0);
  const read = useRef<Promise<void> | null>(null);
  const pollDelay = useRef(15000);
  const retryAt = useRef(0);
  const schedulePoll = useRef<(() => void) | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; revision.current++; }; }, []);
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(null), 3500); return () => clearTimeout(timer); }, [notice]);
  async function refresh() {
    if (operation.current || sliderOperations.current || !mounted.current) return;
    if (read.current) return read.current;
    if (Date.now() < retryAt.current) { schedulePoll.current?.(); return; }
    const rev = revision.current;
    const request = (async () => {
      try {
        const next = await client.snapshot();
        if (mounted.current && rev === revision.current) {
          // Pending and failed reads cannot erase known, acknowledged playback.
          setSnapshot(old => next.connected && !next.deviceSelection?.pending && (next.playbackPending || next.playbackError) && !next.playback ? { ...next, playback: old?.playback ?? null } : next);
          setSnapshotError(null);
          // Provider backoff is enforced by the backend; local playback events remain available.
          const cooldown = (next.player.wsConnected && next.player.active) || next.player.selecting || next.deviceSelection?.pending ? 0 : retryMilliseconds(next.retryAfter);
          retryAt.current = Date.now() + cooldown;
          pollDelay.current = cooldown ? Math.max(1000, cooldown) : next.refreshing || next.playbackPending || (next.player.wsConnected && next.player.active) || next.player.recovering || next.player.selecting || next.deviceSelection?.pending ? 1000 : next.connecting ? 2000 : 15000;
        }
      } catch (failure) {
        if (mounted.current && rev === revision.current) {
          setSnapshotError(errorText(failure));
          const details = failure as { code?: unknown; retryAfter?: unknown } | null;
          const cooldown = retryMilliseconds(details?.retryAfter);
          retryAt.current = Date.now() + cooldown;
          pollDelay.current = cooldown ? Math.max(1000, cooldown) : details?.code === 'startup' ? 1000 : 15000;
        }
      }
    })();
    read.current = request;
    try { await request; }
    finally {
      if (read.current === request) { read.current = null; schedulePoll.current?.(); }
    }
  }
  useEffect(() => {
    if (!visible) return;
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => {
      clearTimeout(timer);
      const remaining = retryAt.current - Date.now();
      timer = setTimeout(() => void refresh(), remaining > 0 ? Math.max(1000, remaining) : pollDelay.current);
    };
    schedulePoll.current = schedule;
    void refresh();
    return () => { if (schedulePoll.current === schedule) schedulePoll.current = null; clearTimeout(timer); };
  }, [client, visible]);
  const run: Run = async (task, done) => {
    if (operation.current) return;
    operation.current = true; setBusy(true); setError(null);
    // Detach pre-command reads: neither their result nor their latency may hold up controls.
    revision.current++;
    read.current = null;
    try {
      await task();
      if (mounted.current) done?.();
    } catch (failure) {
      if (mounted.current) setError(errorText(failure));
    }
    finally {
      revision.current++;
      operation.current = false;
      if (mounted.current) { setBusy(false); void refresh(); }
    }
  };
  const navigate = (next: Route, from = '') => { history.current.push({ route, focus: from, collection }); setFocusTarget(''); setRoute(next); setError(null); };
  const goBack = () => { const previous = history.current.pop(); setFocusTarget(previous?.focus ?? ''); setCollection(previous?.collection ?? null); setRoute(previous?.route ?? 'main'); setError(null); };
  const openMedia = (item: Media) => {
    if (isPlayable(item)) void run(() => client.command('play', { uri: item.uri, ...(item.source === 'liked' ? { source: 'liked', position: item.position } : item.contextUri ? { contextUri: item.contextUri, position: item.position } : {}), ...(item.kind === 'episode' && item.resumePosition ? { positionMs: item.resumePosition } : {}) }));
    else { navigate('collection', `Open ${item.title}`); setCollection(item); }
  };
  const showOptions = (item: Media, context: { parent: Media; page: Page } | null = null) => { setActionItem(item); setActionContext(context); navigate('actions', `Options for ${item.title}`); };
  const playback = snapshot?.playback ?? null;
  const selectingLocal = !!(snapshot?.player.selecting || snapshot?.player.recovering);
  const progress = useProgress(playback, visible);
  const blocked = busy || !!snapshot?.deviceSelection?.pending;
  const canControl = !blocked && !!playback?.device && !playback.device.restricted;
  const command = (name: string, value?: unknown) => void run(() => client.command(name, value), () => {
    if (name === 'queue') setNotice('Added to queue');
    if (name === 'pause' || name === 'resume') {
      const now = Date.now();
      setSnapshot(old => {
        if (!old?.playback) return old;
        const disallows = { ...old.playback.disallows };
        delete disallows[name === 'pause' ? 'resuming' : 'pausing'];
        return { ...old, playback: { ...old.playback, playing: name === 'resume', progress: playbackPosition(old.playback, now), updatedAt: now, disallows } };
      });
    }
  });
  // Sliders retain navigation and coalesce their own changes while an RPC is in flight.
  // Their acknowledgement updates known values before any slower playback read.
  const adjust = async (task: () => Promise<void>, acknowledge: (old: Snapshot) => Snapshot) => {
    sliderOperations.current++; revision.current++; read.current = null;
    setError(null);
    try {
      await task();
      if (mounted.current) setSnapshot(old => old ? acknowledge(old) : old);
    } catch (failure) { if (mounted.current) setError(errorText(failure)); }
    finally {
      sliderOperations.current--; revision.current++;
      if (mounted.current) void refresh();
    }
  };
  const adjustMusic = (name: 'seek' | 'volume', value: number) => adjust(() => client.command(name, value), old => {
    if (!old.playback || old.playback.device?.id !== playback?.device?.id || (name === 'seek' && old.playback.track?.uri !== playback?.track?.uri)) return old;
    const nextPlayback = name === 'seek' ? { ...old.playback, progress: value, updatedAt: Date.now() }
      : { ...old.playback, device: old.playback.device ? { ...old.playback.device, volume: value } : null };
    return { ...old, playback: nextPlayback };
  });
  const adjustAudio = (action: 'other' | 'balance', value: number) => adjust(() => client.audio(action, value), old => ({
    ...old, audio: { ...old.audio, error: null, ...(action === 'other' ? { otherVolume: value } : { balance: value, otherVolume: balanceVolumes(value).other }) },
    playback: action === 'balance' && old.playback?.device && old.playback.device.id === playback?.device?.id ? { ...old.playback, device: { ...old.playback.device, volume: balanceVolumes(value).spotify } } : old.playback,
  }));
  const setAudioMode = (mode: AudioState['mode']) => void run(() => client.audio('mode', mode), () => setSnapshot(old => old ? {
    ...old, audio: { ...old.audio, mode, error: null,
      ...(mode === 'balance' && old.playback?.device?.supportsVolume && !old.playback.device.restricted ? { otherVolume: balanceVolumes(old.audio.balance).other } : {}) },
    playback: mode === 'balance' && old.playback?.device?.supportsVolume && !old.playback.device.restricted
      ? { ...old.playback, device: { ...old.playback.device, volume: balanceVolumes(old.audio.balance).spotify } } : old.playback,
  } : old));
  const message = error || snapshotError || snapshot?.authError;
  const libraryLabels: Record<LibraryKind, string> = { playlists: 'Playlists', albums: 'Saved albums', liked: 'Liked Songs', artists: 'Followed artists', shows: 'Podcasts', episodes: 'Saved episodes', recent: 'Recently played', top_tracks: 'Your top songs', top_artists: 'Your top artists', pinned: 'Pinned collections' };
  const title = ({ main: 'Now playing', playlists: 'Playlists', albums: 'Saved albums', search: 'Search', more: 'More', collection: collection?.title ?? 'Collection', queue: 'Queue', devices: 'Playback device', settings: 'Settings', library: 'Your library', browse: libraryLabels[libraryKind], actions: actionItem?.title ?? 'Options', 'add-playlist': 'Add to playlist', 'create-playlist': 'Create playlist', timer: 'Sleep timer' })[route];
  return <Group key={route} className="sp-root" focusTarget={focusTarget} onBack={route !== 'main' ? goBack : undefined}>
    <style>{styles}</style>
    {route !== 'main' && <header className="sp-heading"><IconButton icon="back" label="Go back" onClick={goBack} preferredFocus={['search', 'devices', 'settings'].includes(route)}/><h2>{title}</h2></header>}
    {message && <div className="sp-banner" role="alert"><span>{message}</span>{error && <IconButton icon="close" label="Dismiss error" onClick={() => setError(null)}/>}</div>}
    {notice && <div className="sp-notice" role="status"><Icon name="check" size={15}/>{notice}</div>}
    <main className="sp-content">
      {!snapshot && !snapshotError && <div role="status" className="sp-loading">Loading SpotiDeck…</div>}
      {!snapshot && snapshotError && <Action className="sp-secondary" label="Retry connection" onClick={() => void refresh()}>Try again</Action>}
      {snapshot && <>
        {route === 'settings' ? <Settings snapshot={snapshot} client={client} run={run} busy={busy} setAudioMode={setAudioMode} onDisconnect={() => { setSnapshot({ ...snapshot, connected: false, playback: null, profile: null }); history.current = []; setFocusTarget(''); setRoute('main'); }}/>
        : !snapshot.connected ? <Empty icon="spotify" title="Connect Spotify"><p>Sign in to control playback and open your playlists.</p><Action label="Connect Spotify" className="sp-primary" preferredFocus onClick={() => navigate('settings', 'Connect Spotify')}>Connect Spotify</Action></Empty>
        : route === 'main' ? <>
          <Player playback={playback} playbackPending={!!snapshot.playbackPending || (selectingLocal && !playback?.device)} progress={progress} canControl={canControl} command={command} audio={snapshot.audio} adjustMusic={adjustMusic} adjustAudio={adjustAudio}/>
          <div className="sp-menu"><MenuRow label="Playlists" icon="library" onClick={() => navigate('playlists', 'Playlists')} preferredFocus={!playback?.device || playback.device.restricted}/><MenuRow label="Search" icon="search" onClick={() => navigate('search', 'Search')}/><MenuRow label="Playback device" icon="device" value={snapshot.deviceSelection?.pending ? `${snapshot.deviceSelection.device.name} · Connecting` : playback?.device?.name ?? (selectingLocal ? 'SpotiDeck · Connecting' : 'Choose')} onClick={() => navigate('devices', 'Playback device')}/><MenuRow label="More" icon="more" onClick={() => navigate('more', 'More')}/></div>
        </>
        : route === 'playlists' || route === 'albums' ? <Library kind={route} client={client} open={openMedia} options={showOptions} current={playback?.track?.uri} create={route === 'playlists' ? () => navigate('create-playlist', 'Create playlist') : undefined}/>
        : route === 'library' ? <div className="sp-menu">{(Object.keys(libraryLabels) as LibraryKind[]).map((kind, i) => <MenuRow key={kind} label={libraryLabels[kind]} icon={kind === 'pinned' ? 'pin' : kind.includes('artist') ? 'artist' : kind === 'shows' || kind === 'episodes' ? 'podcast' : kind === 'recent' ? 'clock' : kind === 'liked' ? 'heart' : 'library'} preferredFocus={i === 0} onClick={() => { setLibraryKind(kind); navigate('browse', libraryLabels[kind]); }}/>)}</div>
        : route === 'browse' ? <Library kind={libraryKind} client={client} open={openMedia} options={showOptions} current={playback?.track?.uri}/>
        : route === 'search' ? <Search client={client} open={openMedia} options={showOptions} queue={item => command('queue', item.uri)} busy={blocked} current={playback?.track?.uri} history={snapshot.searchHistory ?? []} run={run}/>
        : route === 'collection' && collection ? <Collection client={client} item={collection} play={openMedia} options={showOptions} queue={item => command('queue', item.uri)} playSelected={value => command('play', value)} playAll={() => command('play', { uri: collection.uri })} busy={blocked} current={playback?.track?.uri}/>
        : route === 'queue' ? <Queue client={client} current={playback?.track} play={openMedia} busy={blocked} live={visible && !!snapshot.player.wsConnected && !!snapshot.player.active} deviceId={playback?.device?.id}/>
        : route === 'devices' ? <Devices client={client} run={run} busy={busy} settings={() => navigate('settings', 'Player setup')} player={snapshot.player} selection={snapshot.deviceSelection} visible={visible}/>
        : route === 'more' ? <>
          <PlaybackOptions playback={playback} canControl={canControl} command={command} client={client} run={run} busy={busy}/>
          <div className="sp-menu"><MenuRow label="Queue" preferredFocus={!playback?.track} icon="queue" onClick={() => navigate('queue', 'Queue')}/>{playback?.track && <MenuRow label="Current track options" icon="more" onClick={() => showOptions(playback.track!)}/>}<MenuRow label="Your library" icon="library" onClick={() => navigate('library', 'Your library')}/><MenuRow label="Saved albums" icon="music" onClick={() => navigate('albums', 'Saved albums')}/><MenuRow label="Sleep timer" icon="clock" value={snapshot.sleepTimer?.active ? `${Math.ceil(snapshot.sleepTimer.remaining / 60)} min` : 'Off'} onClick={() => navigate('timer', 'Sleep timer')}/><MenuRow label="Settings" icon="settings" onClick={() => navigate('settings', 'Settings')}/></div>
        </>
        : route === 'actions' && actionItem ? <ItemActions client={client} item={actionItem} context={actionContext} snapshot={snapshot} run={run} busy={busy} addPlaylist={() => navigate('add-playlist', 'Add to playlist')} done={focus => { goBack(); if (focus) setFocusTarget(focus); }}/>
        : route === 'add-playlist' && actionItem ? <AddToPlaylist client={client} item={actionItem} run={run} busy={busy} create={() => navigate('create-playlist', 'Create playlist')} done={() => { setNotice('Added to playlist'); goBack(); }}/>
        : route === 'create-playlist' ? <CreatePlaylist client={client} run={run} busy={busy} done={() => { setNotice('Playlist created'); goBack(); }}/>
        : route === 'timer' ? <Timer snapshot={snapshot} client={client} run={run} busy={busy}/> : null}
        {snapshot.needsReauthorization && route !== 'settings' && <div className="sp-message" role="status">New library features need additional Spotify permissions.<Action label="Update Spotify permissions" className="sp-secondary" onClick={() => navigate('settings', 'Update Spotify permissions')}>Update permissions</Action></div>}
        {snapshot.player.recovering && route === 'main' && <p className="sp-audio-message" role="status">Reconnecting the local player…</p>}
        {snapshot.player.selectionError && route === 'main' && <p className="sp-audio-message" role="status">{snapshot.player.selectionError}</p>}
        {snapshot.deviceSelection?.error && route !== 'devices' && <p className="sp-message" role="status">{snapshot.deviceSelection.error}</p>}
        {snapshot.playbackError && route !== 'settings' && <div className="sp-message" role="status">{snapshot.playbackError}</div>}
      </>}
    </main>
  </Group>;
}

function Library({ client, kind, open, options, current, create }: { client: Client; kind: LibraryKind; open(item: Media): void; options(item: Media): void; current?: string; create?(): void }) {
  const data = usePage(`library-${kind}`, offset => client.library(kind, offset));
  return <><div className="sp-list">{kind === 'playlists' && <MediaRow item={liked} onOpen={() => open(liked)} preferredFocus/>}{data.page?.items.map((item, i) => <MediaRow key={`${item.id}-${i}`} item={item} current={current} onOpen={() => kind === 'liked' ? open({ ...item, source: 'liked' }) : open(item)} onOptions={() => options(item)} preferredFocus={kind !== 'playlists' && i === 0}/>)}</div>
    {create && <Action label="Create playlist" className="sp-secondary" onClick={create}><Icon name="plus" size={17}/>Create playlist</Action>}
    {!data.loading && !data.error && data.page?.items.length === 0 && <p className="sp-message">{kind === 'pinned' ? 'Open a collection’s Options and choose Pin collection to keep it here.' : 'Nothing saved here yet.'}</p>}<PageStatus data={data}/></>;
}

function Search({ client, open, options, queue, busy, current, history, run }: { client: Client; open(item: Media): void; options(item: Media): void; queue(item: Media): void; busy: boolean; current?: string; history: string[]; run: Run }) {
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<MediaKind>('track');
  const submit = () => setQuery(input.trim());
  const data = usePage(`search-${query}-${kind}`, offset => client.search(query, kind, offset), query.length > 0);
  const labels: Record<MediaKind, string> = { track: 'Songs', playlist: 'Playlists', album: 'Albums', artist: 'Artists', show: 'Podcasts', episode: 'Episodes' };
  return <><Group flow="row" className="sp-searchbar"><Input value={input} label="Search Spotify" placeholder="Music or podcasts" onChange={setInput} onSubmit={submit}/><IconButton icon="search" label="Find music" onClick={submit} disabled={!input.trim()}/></Group>
    {[['track', 'playlist', 'album'], ['artist', 'show', 'episode']].map((row, i) => <Group key={i} flow="row" className="sp-filters">{row.map(value => <Action label={labels[value as MediaKind]} className="sp-chip" selected={kind === value} key={value} onClick={() => setKind(value as MediaKind)}>{labels[value as MediaKind]}</Action>)}</Group>)}
    {!query ? <><p className="sp-message">Enter a name and choose Find music.</p>{history.length > 0 && <><h3 className="sp-section-heading">Recent searches</h3>{history.map(value => <MenuRow key={value} label={`Search again for ${value}`} icon="clock" value={value} onClick={() => { setInput(value); setQuery(value); }}/>) }<Action label="Clear search history" className="sp-small-link" disabled={busy} onClick={() => void run(() => client.preferences('clear_search_history'))}>Clear search history</Action></>}</> : <><p className="sp-hint">A · Play / open　 X · Add to queue</p><div className="sp-list">{data.page?.items.map((item, i) => <MediaRow key={`${item.id}-${i}`} item={item} current={current} onOpen={() => open(item)} onQueue={isPlayable(item) ? () => queue(item) : undefined} onOptions={() => options(item)} disabled={busy}/>)}</div>
    {data.page?.items.length === 0 && <Empty icon="search" title="No results found."><p>Try another spelling or a different search.</p></Empty>}<PageStatus data={data}/></>}
  </>;
}
function Collection({ client, item, play, options, queue, playSelected, playAll, busy, current }: { client: Client; item: Media; play(item: Media): void; options(item: Media, context?: { parent: Media; page: Page }): void; queue(item: Media): void; playSelected(value: unknown): void; playAll(): void; busy: boolean; current?: string }) {
  const data = usePage(`collection-${item.id}`, offset => item.id === 'liked' ? client.library('liked', offset) : client.tracks(item, offset));
  const select = (track: Media) => {
    if (!isPlayable(track)) { play(track); return; }
    if (item.id === 'liked') { playSelected({ uri: track.uri, source: 'liked', position: track.position ?? data.page!.items.indexOf(track) }); return; }
    if (item.kind === 'playlist' || item.kind === 'album') { playSelected({ uri: track.uri, contextUri: track.contextUri ?? data.page?.contextUri ?? item.uri, position: track.position ?? data.page!.items.indexOf(track) }); return; }
    play(track);
  };
  return <><div className="sp-collection-summary"><Cover item={item} small/><div><p>{item.subtitle}</p><span className="sp-muted">{data.page?.total != null ? `${data.page.total} ${item.kind === 'artist' ? 'releases' : item.kind === 'show' ? 'episodes' : 'songs'}` : ''}</span></div></div>
    {item.uri && <Group flow="row" className="sp-collection-controls">{(item.kind === 'album' || item.kind === 'playlist') && <Action label={`Play ${item.title}`} className="sp-primary sp-play-all" onClick={playAll} disabled={busy} preferredFocus><Icon name="play" size={20}/>Play</Action>}<Action label={`Options for ${item.title}`} className="sp-secondary" onClick={() => options(item)} disabled={busy}><Icon name="more" size={20}/>Options</Action></Group>}
    <p className="sp-hint">A · Play / open　 X · Add to queue</p>
    <div className="sp-list">{data.page?.items.map((track, i) => <MediaRow key={`${track.id}-${i}`} item={{ ...track, image: track.image ?? item.image }} current={current} onOpen={() => select(track)} onQueue={isPlayable(track) ? () => queue(track) : undefined} onOptions={() => options(track, { parent: item, page: data.page! })} disabled={busy} preferredFocus={(!item.uri || item.kind === 'artist' || item.kind === 'show') && i === 0}/>)}</div><PageStatus data={data}/></>;
}
function Queue({ client, current, play, busy, live, deviceId }: { client: Client; current?: Media | null; play(item: Media): void; busy: boolean; live: boolean; deviceId?: string }) {
  const data = usePage(`queue-${deviceId ?? ''}`, async () => ({ items: await client.queue(), next: null, total: null }));
  useEffect(() => { if (!live) return; const timer = setInterval(() => void data.load(0, true), 2000); return () => clearInterval(timer); }, [client, live]);
  return <>{current && <><h3 className="sp-section-heading">Now playing</h3><MediaRow item={current} current={current.uri} onOpen={() => play(current)} disabled={busy}/></>}
    <h3 className="sp-section-heading">Next up</h3><div className="sp-list">{data.page?.items.map((item, i) => <MediaRow key={`${item.id}-${i}`} item={item} onOpen={() => play(item)} disabled={busy} preferredFocus={i === 0}/>)}</div>{data.page?.items.length === 0 && <p className="sp-message">Your queue is empty. Add a song from Search or a playlist.</p>}<PageStatus data={data}/></>;
}

function Devices({ client, run, busy, settings, player, selection, visible }: { client: Client; run: Run; busy: boolean; settings(): void; player: Snapshot['player']; selection: Snapshot['deviceSelection']; visible: boolean }) {
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const generation = useRef(0);
  async function load() { const attempt = ++generation.current; try { const next = await client.devices(); if (alive.current && attempt === generation.current) { setDevices(next); setError(null); } } catch (failure) { if (alive.current && attempt === generation.current) setError(errorText(failure)); } }
  useEffect(() => {
    alive.current = true;
    if (!visible) return () => { alive.current = false; generation.current++; };
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    const poll = async () => { await load(); if (!stopped) timer = setTimeout(() => void poll(), selection?.pending ? 1000 : 3000); };
    void poll();
    return () => { stopped = true; alive.current = false; generation.current++; clearTimeout(timer); };
  }, [client, visible, player.selecting, player.active, selection?.device.id, selection?.pending, selection?.error]);
  const select = (device: Device) => {
    generation.current++;
    void run(() => client.command('transfer', device.id), () => void load());
  };
  return <><p className="sp-message">Choose where your music plays.</p>{player.selecting && <p className="sp-status" role="status">Connecting SpotiDeck automatically…</p>}{player.selectionError && <p className="sp-message" role="status">{player.selectionError}</p>}{selection?.error && <p className="sp-message" role="alert">{selection.error}</p>}{devices?.map(device => {
    const pending = !!selection?.pending && selection.device.id === device.id;
    const waiting = !!selection?.waitingForTrack && selection.device.id === device.id;
    return <Action key={device.id} label={`Listen on ${device.name}`} selected={device.active && !pending} className={`sp-device-row ${device.active && !pending ? 'active' : ''}`} disabled={busy || device.restricted || pending} onClick={() => select(device)}><Icon name="device" size={26}/><div><strong>{device.name}</strong><p>{pending ? 'Connecting…' : waiting ? 'Selected · Choose a song to play' : device.restricted ? 'Playback controls unavailable' : device.active ? 'Current device' : device.type}</p></div>{device.active && !pending && <Icon name="check" size={17}/>}</Action>;
  })}
    {devices?.length === 0 && !player.selecting && <p className="sp-message">No devices found. Start the local player or open Spotify on another device.</p>}{error && <p className="sp-message" role="alert">{error}</p>}
    <Action label="Refresh devices" className="sp-secondary" onClick={() => void load()}>Refresh devices</Action><div className="sp-divider"/><h3>Play on this handheld</h3><p className="sp-message">{player.installed ? 'The local player connects automatically after startup. Use Player setup to check its account and status.' : 'Set up the local Spotify player to listen while you game.'}</p><Action label="Player setup" className="sp-secondary" onClick={settings}>Player setup</Action></>;
}
function useProgress(playback: Playback | null, visible: boolean) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { setNow(Date.now()); if (!visible || !playback?.playing) return; const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, [playback, visible]);
  return playbackPosition(playback, now);
}
function DeferredRange({ value, max, step, label, disabled, change, kind = 'percent' }: { value: number; max: number; step?: number; label: string; disabled?: boolean; change(value: number): Promise<void>; kind?: 'percent' | 'time' | 'balance' }) {
  const [draft, setDraft] = useState(value);
  const [settled, setSettled] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const desired = useRef<number | null>(null);
  const sending = useRef(false);
  const alive = useRef(true);
  const current = useRef({ change, disabled });
  current.current = { change, disabled };
  useEffect(() => { if (!timer.current && !sending.current && desired.current === null) setDraft(value); }, [value, settled]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; desired.current = null; if (timer.current) clearTimeout(timer.current); }; }, []);
  useEffect(() => { if (disabled) { if (timer.current) clearTimeout(timer.current); timer.current = null; desired.current = null; setDraft(value); } }, [disabled]);
  async function flush() {
    if (sending.current || !alive.current || current.current.disabled || desired.current === null) return;
    const next = desired.current;
    desired.current = null; sending.current = true;
    try { await current.current.change(next); }
    finally {
      sending.current = false;
      if (alive.current) {
        if (desired.current !== null && !timer.current) void flush();
        else setSettled(old => old + 1);
      }
    }
  }
  return <Range value={draft} max={Math.max(1, max)} step={step} label={label} disabled={disabled}
    hideLabel={kind === 'time'} valueLabel={kind === 'percent' ? `${Math.round(draft)}%` : undefined}
    edgeLabels={kind === 'time' ? [formatTime(draft), formatTime(max)] : kind === 'balance' ? [`Game ${Math.round(balanceVolumes(draft).other)}%`, `Spotify ${Math.round(balanceVolumes(draft).spotify)}%`] : undefined}
    onChange={next => {
      setDraft(next); desired.current = next;
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => { timer.current = null; void flush(); }, 350);
    }}/>;
}
function Player({ playback, playbackPending, progress, canControl, command, audio, adjustMusic, adjustAudio }: { playback: Playback | null; playbackPending: boolean; progress: number; canControl: boolean; command(name: string, value?: unknown): void; audio: AudioState; adjustMusic(name: 'seek' | 'volume', value: number): Promise<void>; adjustAudio(action: 'other' | 'balance', value: number): Promise<void> }) {
  const track = playback?.track ?? null;
  const canAdjustMusic = canControl && !!playback?.device && !playback.device.restricted;
  const volumeSupported = canAdjustMusic && !!playback?.device?.supportsVolume;
  return <section className="sp-player" aria-label="Playback controls">
    <div className="sp-track"><Cover item={track}/><div><h1>{track?.title ?? (playbackPending ? 'Getting ready' : 'Nothing playing')}</h1><p className="sp-muted" role={!track && playbackPending ? 'status' : undefined}>{track?.subtitle ?? (playbackPending ? 'Connecting to Spotify…' : 'Choose music from your playlists.')}</p></div></div>
    <DeferredRange key={`${playback?.device?.id}:${track?.uri ?? 'no-track'}`} value={progress} max={track?.duration ?? 1} step={1000} label="Track position" kind="time" disabled={!canAdjustMusic || !track || playback?.disallows.seeking} change={value => adjustMusic('seek', value)}/>
    <Group flow="row" className="sp-transport">
      <Action label="Previous track" className="sp-transport-button" onClick={() => command('previous')} disabled={!canControl || playback?.disallows.skipping_prev}><Icon name="previous" size={24}/></Action>
      <Action label={playback?.playing ? 'Pause' : 'Play'} className="sp-transport-button sp-play" preferredFocus onClick={() => command(playback?.playing ? 'pause' : 'resume')} disabled={!canControl || playback?.disallows[playback.playing ? 'pausing' : 'resuming']}><Icon name={playback?.playing ? 'pause' : 'play'} size={26}/></Action>
      <Action label="Next track" className="sp-transport-button" onClick={() => command('next')} disabled={!canControl || playback?.disallows.skipping_next}><Icon name="next" size={24}/></Action>
    </Group>
    {track?.kind === 'episode' && <Group flow="row" className="sp-podcast-skip"><Action label="Rewind episode 15 seconds" className="sp-secondary" disabled={!canAdjustMusic || playback?.disallows.seeking} onClick={() => void adjustMusic('seek', Math.max(0, progress - 15000))}>−15 seconds</Action><Action label="Forward episode 15 seconds" className="sp-secondary" disabled={!canAdjustMusic || playback?.disallows.seeking} onClick={() => void adjustMusic('seek', Math.min(track.duration ?? progress + 15000, progress + 15000))}>+15 seconds</Action></Group>}
    <div className="sp-volume" key={audio.mode}>
      {audio.mode === 'balance' ? <DeferredRange key={`balance-${playback?.device?.id ?? 'none'}`} value={audio.balance} max={100} step={5} label="Game / Spotify balance" kind="balance" disabled={!audio.supported || !volumeSupported} change={value => adjustAudio('balance', value)}/>
        : <><DeferredRange key={`music-${playback?.device?.id ?? 'none'}`} value={playback?.device?.volume ?? 50} max={100} step={5} label="Music volume" disabled={!volumeSupported} change={value => adjustMusic('volume', value)}/>
          <DeferredRange value={audio.otherVolume} max={100} step={5} label="Other audio" disabled={!audio.supported} change={value => adjustAudio('other', value)}/></>}
      {audio.error && <p className="sp-audio-message" role="status">{audio.error}</p>}
      {!audio.supported && !audio.error && <p className="sp-audio-message">Other audio is unavailable on this device.</p>}
      {audio.mode === 'balance' && audio.supported && !volumeSupported && <p className="sp-audio-message">Choose a Spotify playback device with volume control to adjust the balance.</p>}
    </div>
  </section>;
}
function SaveRow({ client, item, run, busy }: { client: Client; item: Media; run: Run; busy: boolean }) {
  const [saved, setSaved] = useState<boolean | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true; setSaved(null); setFailure(null);
    void client.libraryState([item.uri]).then(result => { if (alive) setSaved(result.states[item.uri] ?? false); }, error => { if (alive) setFailure(errorText(error)); });
    return () => { alive = false; };
  }, [client, item.uri, attempt]);
  const target = item.kind === 'track' ? 'Liked Songs' : item.kind === 'episode' ? 'Saved episodes' : 'your library';
  return <><MenuRow label={saved ? `Remove from ${target}` : `Save to ${target}`} icon={saved ? 'check' : 'heart'} value={saved === null ? 'Checking…' : saved ? 'Saved' : 'Add'} disabled={busy || saved === null} onClick={() => void run(() => client.command(saved ? 'unsave' : 'save', item.uri), () => setSaved(!saved))}/>{failure && <div className="sp-message" role="alert">{failure}<Action label="Retry saved status" className="sp-small-link" onClick={() => setAttempt(value => value + 1)}>Check again</Action></div>}</>;
}
function ItemActions({ client, item, context, snapshot, run, busy, addPlaylist, done }: { client: Client; item: Media; context: { parent: Media; page: Page } | null; snapshot: Snapshot; run: Run; busy: boolean; addPlaylist(): void; done(focus?: string): void }) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const pinned = snapshot.pins?.includes(item.uri) ?? false;
  const editable = context?.parent.kind === 'playlist' && context.parent.id !== 'liked' && context.page.editable === true;
  const position = item.position;
  const canEdit = editable && position !== undefined && !!context.page.snapshotId;
  const modify = (action: 'remove' | 'reorder', insertBefore?: number) => void run(() => client.playlist(action, { id: context!.parent.id, uri: item.uri, position, snapshotId: context!.page.snapshotId, ...(insertBefore !== undefined ? { insertBefore } : {}) }), () => done(action === 'remove' ? `Play ${context!.parent.title}` : undefined));
  return <><div className="sp-collection-summary"><Cover item={item} small/><div><p>{item.title}</p><span className="sp-muted">{item.subtitle}</span></div></div><SaveRow client={client} item={item} run={run} busy={busy}/>
    {!isPlayable(item) && <MenuRow label={pinned ? 'Unpin collection' : 'Pin collection'} icon="pin" value={pinned ? 'Pinned' : 'Add'} disabled={busy} onClick={() => void run(() => client.pin(pinned ? 'remove' : 'add', item))}/>}
    {isPlayable(item) && <><MenuRow label="Add to queue" icon="queue" disabled={busy} onClick={() => void run(() => client.command('queue', item.uri), done)}/><MenuRow label="Add to playlist" icon="plus" disabled={busy} onClick={addPlaylist}/></>}
    {item.kind === 'episode' && <MenuRow label="Play episode from beginning" icon="play" disabled={busy} onClick={() => void run(() => client.command('play', { uri: item.uri, positionMs: 0 }), done)}/>}
    {canEdit && <><div className="sp-divider"/><h3 className="sp-section-heading">In {context.parent.title}</h3><MenuRow label="Move up in playlist" icon="up" disabled={busy || position === 0} onClick={() => modify('reorder', position! - 1)}/><MenuRow label="Move down in playlist" icon="down" disabled={busy || context.page.total == null || position! >= context.page.total - 1} onClick={() => modify('reorder', position! + 2)}/><MenuRow label={confirmRemove ? 'Confirm remove from playlist' : 'Remove from playlist'} icon="close" disabled={busy} onClick={() => confirmRemove ? modify('remove') : setConfirmRemove(true)}/></>}
    <Action label="Open in Spotify" className="sp-small-link" onClick={() => openExternal(`https://open.spotify.com/${item.kind}/${item.id}`)}>Open in Spotify ↗</Action>
  </>;
}
function AddToPlaylist({ client, item, run, busy, create, done }: { client: Client; item: Media; run: Run; busy: boolean; create(): void; done(): void }) {
  const data = usePage('editable-playlists', offset => client.library('playlists', offset));
  const available = data.page?.items.filter(playlist => playlist.editable === true) ?? [];
  return <><p className="sp-message">Choose one of your playlists for “{item.title}”.</p><div className="sp-list">{available.map((playlist, index) => <MediaRow key={playlist.uri} item={playlist} label={`Add to ${playlist.title}`} onOpen={() => void run(() => client.playlist('add', { id: playlist.id, uris: [item.uri] }), done)} disabled={busy} preferredFocus={index === 0}/>)}</div>{!data.loading && available.length === 0 && <p className="sp-message">No editable playlists on this page.</p>}<PageStatus data={data}/><Action label="Create playlist" className="sp-secondary" onClick={create}>Create playlist</Action></>;
}
function CreatePlaylist({ client, run, busy, done }: { client: Client; run: Run; busy: boolean; done(): void }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [isPublic, setPublic] = useState(false);
  const create = () => { if (name.trim()) void run(() => client.playlist('create', { name: name.trim(), description: description.trim(), public: isPublic }), done); };
  return <div className="sp-settings"><label>Playlist name</label><Input label="Playlist name" value={name} onChange={setName} placeholder="New playlist" onSubmit={create}/><label>Description (optional)</label><Input label="Playlist description" value={description} onChange={setDescription}/><MenuRow label="Public playlist" icon="library" value={isPublic ? 'On' : 'Off'} selected={isPublic} disabled={busy} onClick={() => setPublic(!isPublic)}/><Action label="Create new playlist" className="sp-primary sp-play-all" disabled={busy || !name.trim()} onClick={create}>Create playlist</Action></div>;
}
function Timer({ snapshot, client, run, busy }: { snapshot: Snapshot; client: Client; run: Run; busy: boolean }) {
  const [minutes, setMinutes] = useState('30');
  const [now, setNow] = useState(Date.now());
  const state = snapshot.sleepTimer;
  useEffect(() => { const clock = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(clock); }, []);
  const remaining = state?.endsAt ? Math.max(0, state.endsAt - now) : 0;
  const valid = /^\d+$/.test(minutes) && Number(minutes) >= 1 && Number(minutes) <= 240;
  return <><p className="sp-message">Pause music after a set time. The timer keeps running when QAM is closed.</p><p className="sp-status" role="status">{state?.active ? `${formatTime(remaining)} remaining` : 'Sleep timer is off'}</p>{[15, 30, 45, 60, 90].map((value, i) => <MenuRow key={value} label={`Pause after ${value} minutes`} icon="clock" preferredFocus={i === 0} disabled={busy} onClick={() => void run(() => client.timer(value))}/>)}<div className="sp-settings"><label>Custom duration in minutes</label><Input label="Sleep timer minutes" value={minutes} onChange={setMinutes}/><Action label="Set custom sleep timer" className="sp-secondary" disabled={busy || !valid} onClick={() => void run(() => client.timer(Number(minutes)))}>Set timer</Action></div>{state?.active && <Action label="Cancel sleep timer" className="sp-secondary" disabled={busy} onClick={() => void run(() => client.timer(0))}>Cancel timer</Action>}{state?.error && <p className="sp-message" role="alert">{state.error}</p>}</>;
}
function PlaybackOptions({ playback, canControl, command, client, run, busy }: { playback: Playback | null; canControl: boolean; command(name: string, value?: unknown): void; client: Client; run: Run; busy: boolean }) {
  if (!playback?.track) return null;
  return <section className="sp-options">
    <MenuRow label="Shuffle" icon="shuffle" value={playback.shuffle ? 'On' : 'Off'} selected={playback.shuffle} onClick={() => command('shuffle', !playback.shuffle)} disabled={!canControl || playback.disallows.toggling_shuffle} preferredFocus/>
    <MenuRow label="Repeat" icon="repeat" value={playback.repeat === 'off' ? 'Off' : playback.repeat === 'track' ? 'One song' : 'All'} selected={playback.repeat !== 'off'} onClick={() => command('repeat', playback.repeat === 'off' ? 'context' : playback.repeat === 'context' ? 'track' : 'off')} disabled={!canControl || playback.disallows[playback.repeat === 'context' ? 'toggling_repeat_track' : 'toggling_repeat_context']}/>
    <SaveRow client={client} item={playback.track} run={run} busy={busy}/>
  </section>;
}
function Settings({ snapshot, client, run, busy, onDisconnect, setAudioMode }: { snapshot: Snapshot; client: Client; run: Run; busy: boolean; onDisconnect(): void; setAudioMode(mode: AudioState['mode']): void }) {
  const [clientId, setClientId] = useState(snapshot.clientId);
  const [key, setKey] = useState('');
  const [confirmSignOut, setConfirmSignOut] = useState(false);
  const [mode, setMode] = useState<'handheld' | 'phone'>('handheld');
  const [connection, setConnection] = useState<Connection | null>(null);
  const [qr, setQr] = useState('');
  const [qrError, setQrError] = useState<string | null>(null);
  const [details, setDetails] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (snapshot.connected && !snapshot.connecting) setConnection(null); }, [snapshot.connected, snapshot.connecting]);
  useEffect(() => {
    let alive = true; setQr(''); setQrError(null);
    if (connection?.mode === 'phone') void QRCode.toDataURL(connection.url, { width: 360, margin: 4, errorCorrectionLevel: 'M' }).then(value => { if (alive) setQr(value); }, () => { if (alive) setQrError('Could not generate the sign-in code. Restart sign-in or use this handheld.'); });
    return () => { alive = false; };
  }, [connection]);
  useEffect(() => { if (!connection?.expiresAt) return; const clock = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(clock); }, [connection]);
  const expired = !!connection?.expiresAt && now >= connection.expiresAt;
  const begin = () => void run(async () => { const result = await client.connect(clientId.trim(), mode); setConnection(result); setNow(Date.now()); if (mode === 'handheld') openExternal(result.url); });
  return <div className="sp-settings">
    <section><h3>Volume sliders</h3><Group flow="row" className="sp-filters">
      <Action label="Separate volume sliders" className="sp-chip" selected={snapshot.audio.mode === 'separate'} disabled={busy || !snapshot.audio.supported} onClick={() => setAudioMode('separate')}>Separate</Action>
      <Action label="Game / Spotify balance mode" className="sp-chip" selected={snapshot.audio.mode === 'balance'} disabled={busy || !snapshot.audio.supported} onClick={() => setAudioMode('balance')}>Balance</Action>
    </Group><p>{snapshot.audio.mode === 'separate' ? 'Adjust Spotify and all other audio independently.' : 'At the center, both are at 100%. Move left to lower Spotify, or right to lower all other audio.'}</p></section>
    <div className="sp-divider"/>
    <section><h3>Spotify account</h3>{snapshot.connected && <><div className="sp-status">Connected as {snapshot.profile?.name ?? 'Spotify listener'}</div><Action label={confirmSignOut ? 'Confirm sign out' : 'Sign out'} className="sp-secondary" disabled={busy} onClick={() => confirmSignOut ? void run(() => client.disconnect(), onDisconnect) : setConfirmSignOut(true)}>{confirmSignOut ? 'Confirm sign out' : 'Sign out'}</Action></>}
      {(!snapshot.connected || snapshot.needsReauthorization) && <>{snapshot.needsReauthorization && <p role="status">Connect again to enable the new library and playlist permissions.</p>}<p>Create a Spotify developer app with Web API access, then paste its Client ID below.</p><Action label="Open Spotify developer dashboard" className="sp-secondary" onClick={() => openExternal('https://developer.spotify.com/dashboard')}>Open Spotify dashboard</Action><label>Client ID</label><Input label="Spotify Client ID" value={clientId} placeholder="Paste your Client ID" onChange={setClientId}/>
        <Group flow="row" className="sp-filters"><Action label="Sign in on this handheld" className="sp-chip" selected={mode === 'handheld'} disabled={busy || snapshot.connecting} onClick={() => setMode('handheld')}>This handheld</Action><Action label="Sign in with phone QR" className="sp-chip" selected={mode === 'phone'} disabled={busy || snapshot.connecting} onClick={() => setMode('phone')}>Phone QR</Action></Group>
        {mode === 'handheld' ? <><p>Add this exact redirect URI in your Spotify app settings:</p><code>http://127.0.0.1:43891/callback</code></> : <p>Keep your phone and handheld on the same Wi-Fi. Start sign-in to show the QR code and the redirect URI for your Spotify app.</p>}
        <Action label="Connect to Spotify" className="sp-primary" disabled={busy || !/^[a-fA-F0-9]{32}$/.test(clientId.trim())} onClick={begin}>{snapshot.connecting ? 'Restart Spotify sign-in' : snapshot.needsReauthorization ? 'Update Spotify permissions' : 'Connect to Spotify'}</Action>
        {connection?.mode === 'phone' && snapshot.connecting && !expired && <><p>Add this exact redirect URI to your Spotify app before continuing on your phone:</p><code>{connection.redirect}</code>{qr && <img className="sp-qr" src={qr} alt="Scan with your phone to connect Spotify"/>}<p>Your phone opens a secure page hosted on this handheld. Its local certificate needs to be trusted once, then choose Continue to Spotify.</p>{connection.expiresAt && <p role="status">Code expires in {formatTime(connection.expiresAt - now)}.</p>}<Action label="Show connection certificate" className="sp-small-link" onClick={() => setDetails(!details)}>{details ? 'Hide' : 'Show'} certificate fingerprint</Action>{details && <code className="sp-fingerprint">{connection.certificateFingerprint ?? 'Not available'}</code>}</>}
        {expired && <p role="status">This sign-in code expired. Restart sign-in for a new code.</p>}{qrError && <p role="alert">{qrError}</p>}
        {snapshot.connecting && <>{mode === 'handheld' && <p role="status">Finish signing in in the browser on this handheld. Your account will connect automatically.</p>}<Action label="Cancel Spotify sign-in" className="sp-secondary" disabled={busy} onClick={() => void run(() => client.cancelConnect(), () => setConnection(null))}>Cancel sign-in</Action></>}<p>Premium is required. Your password is entered only on Spotify's own website.</p></>}
    </section><div className="sp-divider"/><section><h3>Listen on this handheld</h3><p>The local player keeps music playing when you close Decky or launch a game.</p>{!snapshot.player.supported ? <p>This player requires a supported Linux handheld.</p> : <>
      <Action label="About Spotify Soloist" className="sp-small-link" onClick={() => openExternal('https://developer.spotify.com/documentation/soloist')}>Powered by Spotify Soloist ↗</Action>
      {!snapshot.player.installed ? <Action label="Install local player" className="sp-secondary" disabled={busy} onClick={() => void run(() => client.player('install'))}>{busy ? 'Working…' : 'Install player from Spotify'}</Action> : <div className="sp-status">Player installed</div>}
      <p>Generate your personal Soloist key in the Spotify dashboard. Each user needs their own key.</p><Action label="Open Soloist documentation" className="sp-small-link" onClick={() => openExternal('https://developer.spotify.com/documentation/soloist/concepts/authentication')}>Get a player key ↗</Action><label>{snapshot.player.hasKey ? 'Replace player key' : 'Player key'}</label><Input label="Soloist API key" value={key} placeholder={snapshot.player.hasKey ? 'Key saved on this handheld' : 'Paste your Soloist key'} onChange={setKey}/><Action label="Save player key" className="sp-secondary" disabled={busy || !key.trim()} onClick={() => void run(() => client.player('key', key.trim()), () => setKey(''))}>Save key</Action>
      {snapshot.player.installed && <Action label={snapshot.player.running ? 'Stop local player' : 'Start local player'} className="sp-primary" disabled={busy || !snapshot.player.hasKey} onClick={() => void run(() => client.player(snapshot.player.running ? 'stop' : 'start'))}>{snapshot.player.running ? 'Stop player' : 'Start player'}</Action>}
      {snapshot.player.running && <p>Select “SpotiDeck” in the Spotify app once to pair. The plugin then selects its local player automatically.</p>}
      {snapshot.player.installed && <Action label="Update player" className="sp-small-link" disabled={busy || snapshot.player.running} onClick={() => void run(() => client.player('install'))}>Update player from Spotify</Action>}
      <p>Soloist builds expire after 90 days. Stop the player and update it here when needed.</p></>}{snapshot.player.error && <p role="alert">{snapshot.player.error}</p>}</section>
    <div className="sp-divider"/><UpdateSection client={client}/>
    <p className="sp-note">SpotiDeck · 1.0.0<br/>Independent plugin. Not affiliated with Spotify.</p></div>;
}
