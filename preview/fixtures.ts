import type { Client, Device, Media, Snapshot } from '../src/types';
import { balanceVolumes } from '../src/audio';

// Fictional music and original vector covers, for the browser preview only.
// This module is never imported by or packaged with the Decky plugin.
const artwork = (title: string, subtitle: string, colors: string[], shape: 'sun' | 'wave' | 'arch' | 'grid') => {
  const [bg, accent, ink] = colors;
  const illustration = shape === 'sun' ? `<circle cx="205" cy="153" r="82" fill="${accent}"/><path d="M0 218 70 148 145 226 225 180 320 240V320H0Z" fill="${ink}"/><path d="M0 257 80 240 170 268 270 219 320 240v80H0Z" fill="${bg}" opacity=".6"/>`
    : shape === 'arch' ? `<path d="M66 330V155a94 94 0 0 1 188 0v175" fill="none" stroke="${accent}" stroke-width="45"/><path d="M121 330V168a40 40 0 0 1 80 0v162" fill="${ink}"/><circle cx="228" cy="132" r="23" fill="${ink}"/>`
    : shape === 'grid' ? `<g stroke="${accent}" fill="none" opacity=".65">${Array.from({ length: 11 }, (_, i) => `<path d="M${i * 50 - 90} 160 160 320M${i * 50 - 90} 320 160 160M0 ${180 + i * 18}h320"/>`).join('')}</g><circle cx="160" cy="167" r="63" fill="${accent}"/><path d="M0 208h320v10H0Zm0-20h320v8H0Zm0-19h320v5H0Z" fill="${bg}"/>`
    : `<g fill="none" stroke="${accent}" stroke-width="16">${Array.from({ length: 7 }, (_, i) => `<path d="M-40 ${130 + i * 28}Q80 ${45 + i * 28} 160 ${130 + i * 28}T370 ${130 + i * 28}"/>`).join('')}</g>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320"><rect width="320" height="320" fill="${bg}"/>${illustration}<text x="23" y="38" fill="${ink}" font-family="Arial,sans-serif" font-size="10" letter-spacing="3" font-weight="700">${subtitle}</text><text x="21" y="76" fill="${ink}" font-family="Arial,sans-serif" font-size="31" letter-spacing="-1.7" font-weight="900">${title}</text></svg>`);
};
const id = (n: number) => String(n).padStart(22, '0');
const covers = [
  artwork('NIGHT DRIVE', 'AFTER DARK', ['#13213a', '#c6b5fa', '#faf0db'], 'grid'),
  artwork('SLOW SUNDAY', 'TAKE YOUR TIME', ['#edceb0', '#b57251', '#412d2b'], 'arch'),
  artwork('DEEP FOCUS', 'STAY IN THE FLOW', ['#29493d', '#8ab99c', '#e6e7cf'], 'wave'),
  artwork('GOLDEN HOUR', 'A LITTLE LONGER', ['#ad432e', '#f4ac56', '#f9e6c1'], 'sun'),
  artwork('LATE CHECKOUT', 'NO NEED TO RUSH', ['#b6bfd9', '#566794', '#1c2946'], 'wave'),
  artwork('ON REPEAT', 'ALL YOUR FAVORITES', ['#d5e671', '#6c853a', '#1b382e'], 'arch'),
];
export const playlists: Media[] = ['Night Drive', 'Slow Sunday', 'Deep Focus', 'Golden Hour', 'Late Checkout', 'On Repeat'].map((title, i) => ({
  id: id(i + 1), uri: `spotify:playlist:${id(i + 1)}`, kind: 'playlist', title,
  subtitle: ['Synths for the open road', 'Easy sounds. No alarms.', 'Less noise. More focus.', 'A warm end to the day', 'Take the scenic route', 'The songs you come back to'][i], image: covers[i], total: 24, editable: true,
}));
export const tracks: Media[] = ['Midnight City Lights', 'Silver Horizon', 'Stay a Little Longer', 'Low Tide', 'Afterglow', 'Signals', 'The Long Way Home', 'Fading Into Blue'].map((title, i) => ({
  id: id(i + 101), uri: `spotify:track:${id(i + 101)}`, kind: 'track', title,
  subtitle: ['The Midnight Coast', 'Soft Signal', 'Luna Park', 'Still North'][i % 4], image: covers[i % covers.length], duration: 242000 - i * 8300, explicit: i === 2, playable: true,
}));
export const artists: Media[] = ['The Midnight Coast', 'Soft Signal', 'Luna Park', 'Still North'].map((title, i) => ({ id: id(i + 201), uri: `spotify:artist:${id(i + 201)}`, kind: 'artist', title, subtitle: 'Artist', image: covers[i] }));
export const shows: Media[] = [{ id: id(301), uri: `spotify:show:${id(301)}`, kind: 'show', title: 'The Player Lounge', subtitle: 'Conversations about games and music', image: covers[4] }];
export const episodes: Media[] = ['Music Behind the Game', 'The Art of Exploration', 'A Quiet Adventure'].map((title, i) => ({ id: id(i + 401), uri: `spotify:episode:${id(i + 401)}`, kind: 'episode', title, subtitle: shows[0].title, image: shows[0].image, duration: 1800000 + i * 200000, resumePosition: i === 0 ? 120000 : 0, playable: true }));
export function previewClient(connected = true): Client {
  const devices: Device[] = [{ id: 'decky', name: 'SpotiDeck', type: 'Computer', active: true, restricted: false, volume: 55, supportsVolume: true }, { id: 'speakers', name: 'Living room', type: 'Speaker', active: false, restricted: false, volume: 35, supportsVolume: true }];
  const state: Snapshot = { connected, connecting: false, clientId: '', authError: null, playbackError: null, profile: { name: 'Rayek', image: null },
    playback: connected ? { track: tracks[0], playing: true, progress: 86000, shuffle: false, repeat: 'off', device: devices[0], disallows: {}, updatedAt: Date.now() } : null,
    player: { installed: true, running: connected, hasKey: true, error: null, supported: true },
    audio: { mode: 'separate', otherVolume: 100, balance: 50, supported: true, otherStreams: 1, error: null },
    pins: [], searchHistory: [], sleepTimer: { active: false, endsAt: null, remaining: 0, error: null } };
  let queue = tracks.slice(1, 6);
  const copy = <T,>(data: T): T => JSON.parse(JSON.stringify(data));
  const owned = copy(playlists);
  const albums: Media[] = playlists.map(p => ({ ...p, kind: 'album', uri: p.uri.replace('playlist', 'album') }));
  const content = new Map(owned.map(p => [p.id, copy(tracks)]));
  const saved = new Set<string>();
  const pins: Media[] = [];
  let revision = 1;
  return {
    async snapshot() { if (state.sleepTimer?.active && state.sleepTimer.endsAt) { state.sleepTimer.remaining = Math.max(0, Math.ceil((state.sleepTimer.endsAt - Date.now()) / 1000)); if (!state.sleepTimer.remaining) { state.sleepTimer.active = false; if (state.playback) state.playback.playing = false; } } return copy(state); },
    async library(kind, offset = 0) { const items = kind === 'pinned' ? pins : kind === 'liked' || kind === 'recent' || kind === 'top_tracks' ? tracks.map((item, position) => ({ ...item, position })) : kind === 'albums' ? albums : kind === 'artists' || kind === 'top_artists' ? artists : kind === 'shows' ? shows : kind === 'episodes' ? episodes : owned; return copy({ items: items.slice(Number(offset)), total: items.length, next: null }); },
    async search(query, kind) { state.searchHistory = [query, ...(state.searchHistory ?? []).filter(value => value !== query)].slice(0, 8); const items = kind === 'track' ? tracks : kind === 'artist' ? artists : kind === 'show' ? shows : kind === 'episode' ? episodes : kind === 'album' ? albums : owned; return copy({ items: items.filter(p => `${p.title} ${p.subtitle}`.toLowerCase().includes(query.toLowerCase())), next: null, total: null }); },
    async tracks(item, offset = 0) { const items = item.kind === 'artist' ? albums.slice(0, 3) : item.kind === 'show' ? episodes : item.kind === 'playlist' ? content.get(item.id) ?? tracks : tracks; return copy({ items: items.map((track, position) => ({ ...track, position, ...(item.kind === 'playlist' || item.kind === 'album' ? { contextUri: item.uri } : {}) })).slice(Number(offset)), next: null, total: items.length, contextUri: item.uri, editable: item.kind === 'playlist', snapshotId: String(revision) }); },
    async libraryState(uris) { return { states: Object.fromEntries(uris.map(uri => [uri, saved.has(uri)])) }; },
    async pin(action, value) { const index = pins.findIndex(item => item.uri === value.uri); if (action === 'remove' && index >= 0) pins.splice(index, 1); if (action === 'add' && index < 0) pins.push(copy(value)); state.pins = pins.map(item => item.uri); return copy(pins); },
    async playlist(action, value) {
      if (action === 'create') { const item: Media = { id: id(owned.length + 500), uri: `spotify:playlist:${id(owned.length + 500)}`, kind: 'playlist', title: value.name!, subtitle: value.description ?? '', image: null, editable: true }; owned.push(item); content.set(item.id, []); return { item: copy(item) }; }
      const items = content.get(value.id!) ?? [];
      if (action === 'add') for (const uri of value.uris ?? []) { const item = [...tracks, ...episodes].find(track => track.uri === uri); if (item) items.push(copy(item)); }
      if (action === 'remove') items.splice(value.position!, 1);
      if (action === 'reorder') { const [item] = items.splice(value.position!, 1); const target = value.insertBefore! > value.position! ? value.insertBefore! - 1 : value.insertBefore!; if (item) items.splice(target, 0, item); }
      content.set(value.id!, items); return { snapshotId: String(++revision) };
    },
    async timer(minutes) { state.sleepTimer = { active: minutes > 0, endsAt: minutes ? Date.now() + minutes * 60000 : null, remaining: minutes * 60, error: null }; return copy(state.sleepTimer); },
    async preferences() { state.searchHistory = []; },
    async updatesCheck() { return { success: true, current_version: '1.0.0', update_available: false, download_available: false, no_release: true }; },
    async updatesDownload() { return { success: false, error: 'Downloads are available in the installed Decky plugin.' }; },
    async devices() { return copy(devices); }, async queue() { return copy(queue); },
    async command(command, value) {
      const p = state.playback;
      if (!p) return;
      if (p.playing) p.progress = Math.min(p.track?.duration ?? 0, p.progress + Date.now() - p.updatedAt);
      if (command === 'pause') p.playing = false;
      if (command === 'resume') p.playing = true;
      if (command === 'next' || command === 'previous') { const current = tracks.findIndex(t => t.uri === p.track?.uri); p.track = tracks[(current + (command === 'next' ? 1 : tracks.length - 1)) % tracks.length]; p.progress = 0; }
      if (command === 'play') { const request = value as { uri: string; contextUri?: string; position?: number; source?: string; positionMs?: number }; p.track = [...tracks, ...episodes].find(t => t.uri === request.uri) ?? tracks[0]; p.playing = true; p.progress = request.positionMs ?? 0; if (request.contextUri || request.source === 'liked') { const list = request.contextUri?.startsWith('spotify:playlist:') ? content.get(request.contextUri.split(':')[2]) ?? tracks : tracks; queue = list.slice((request.position ?? 0) + 1); } }
      if (command === 'save') saved.add(String(value));
      if (command === 'unsave') saved.delete(String(value));
      if (command === 'seek') p.progress = value as number;
      if (command === 'volume' && p.device) p.device.volume = value as number;
      if (command === 'shuffle') p.shuffle = value as boolean;
      if (command === 'repeat') p.repeat = value as 'off' | 'context' | 'track';
      if (command === 'queue') { const track = [...tracks, ...episodes].find(t => t.uri === value); if (track) queue.push(track); }
      if (command === 'transfer') { devices.forEach(d => d.active = d.id === value); p.device = devices.find(d => d.active) ?? null; p.playing = false; }
      p.updatedAt = Date.now();
    },
    async connect() { throw new Error('This is a visual preview. Connect your account in the installed Decky plugin.'); },
    async cancelConnect() { state.connecting = false; },
    async disconnect() { state.connected = false; state.playback = null; },
    async player(action) { if (action === 'install') state.player.installed = true; if (action === 'key') state.player.hasKey = true; if (action === 'start') state.player.running = true; if (action === 'stop') state.player.running = false; },
    async audio(action, value) {
      if (action === 'mode') state.audio.mode = value as 'separate' | 'balance';
      if (action === 'other') state.audio.otherVolume = Number(value);
      if (action === 'balance') state.audio.balance = Number(value);
      if (action === 'balance' || (action === 'mode' && value === 'balance')) {
        const volumes = balanceVolumes(state.audio.balance);
        state.audio.otherVolume = volumes.other;
        if (state.playback?.device) state.playback.device.volume = volumes.spotify;
      }
    },
  };
}
