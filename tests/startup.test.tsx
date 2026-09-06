import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { App } from '../src/App';
import { previewClient, tracks } from '../preview/fixtures';
import type { Snapshot } from '../src/types';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

const pending = (state: Snapshot): Snapshot => ({ ...state, profile: null, playback: null,
  refreshing: true, playbackPending: true, retryAfter: 0 });
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('SpotiDeck startup', () => {
  it('shows automatic local selection without inventing an active device or enabling controls', async () => {
    vi.useFakeTimers(); const client = previewClient(); const ready = await client.snapshot();
    const selecting: Snapshot = { ...ready, playback: null, refreshing: false, playbackPending: false, retryAfter: 30,
      player: { ...ready.player, selecting: true, active: false, ready: false } };
    client.snapshot = vi.fn().mockResolvedValueOnce(selecting).mockResolvedValue({ ...ready, player: { ...ready.player, selecting: false, active: true, wsConnected: true } });
    render(<App client={client}/>); await act(async () => {});
    expect(screen.getByRole('button', { name: 'Playback device' }).textContent).toContain('SpotiDeck · Connecting');
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('slider', { name: 'Music volume' }) as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByText('Choose')).toBeNull();
    await advance(1000);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Playback device' }).textContent).toBe('Playback deviceSpotiDeck');
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('shows selection failure honestly and leaves manual device choice available', async () => {
    vi.useFakeTimers(); const client = previewClient(); const state = await client.snapshot();
    client.snapshot = vi.fn().mockResolvedValue({ ...state, playback: null, player: { ...state.player, selecting: false, selectionError: 'SpotiDeck could not connect. Choose a device or retry setup.' } });
    render(<App client={client}/>); await act(async () => {});
    expect(screen.getByRole('button', { name: 'Playback device' }).textContent).toContain('Choose');
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('SpotiDeck could not connect. Choose a device or retry setup.')).toBeTruthy();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Playback device' })); });
    expect(screen.getByRole('button', { name: 'Refresh devices' })).toBeTruthy();
  });

  it('keeps the confirmed remote device name while local recovery runs', async () => {
    const client = previewClient(); const state = await client.snapshot();
    state.playback!.device!.name = 'Living room'; state.player.recovering = true; state.player.active = false;
    client.snapshot = vi.fn().mockResolvedValue(state); render(<App client={client}/>);
    expect((await screen.findByRole('button', { name: 'Playback device' })).textContent).toContain('Living room');
    expect(screen.queryByText('SpotiDeck · Connecting')).toBeNull();
  });

  it('refreshes device results after activation and ignores a slower list from before it', async () => {
    vi.useFakeTimers(); const client = previewClient(); const ready = await client.snapshot();
    const old = deferred<Awaited<ReturnType<typeof client.devices>>>();
    client.snapshot = vi.fn().mockResolvedValueOnce({ ...ready, playback: null, player: { ...ready.player, selecting: true, active: false } })
      .mockResolvedValue({ ...ready, player: { ...ready.player, selecting: false, active: true, wsConnected: true } });
    client.devices = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue([ready.playback!.device!]);
    render(<App client={client}/>); await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Playback device' })); });
    expect(screen.getByText('Connecting SpotiDeck automatically…')).toBeTruthy();
    await advance(1000);
    expect(client.devices).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Listen on SpotiDeck' })).toBeTruthy();
    await act(async () => { old.resolve([]); });
    expect(screen.getByRole('button', { name: 'Listen on SpotiDeck' })).toBeTruthy();
    expect(screen.queryByText('Connecting SpotiDeck automatically…')).toBeNull();
  });

  it('recovers from a starting backend after one second and returns to normal polling', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    client.snapshot = vi.fn().mockRejectedValueOnce(Object.assign(new Error('SpotiDeck is starting.'), { code: 'startup', retryAfter: 0 }))
      .mockResolvedValue(state);
    render(<App client={client}/>);
    await act(async () => {});
    expect(screen.getByRole('button', { name: 'Retry connection' })).toBeTruthy();
    await advance(999);
    expect(client.snapshot).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    await advance(14999);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(client.snapshot).toHaveBeenCalledTimes(3);
  });

  it('keeps playlists, search and settings reachable while cloud playback is pending', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    client.snapshot = vi.fn().mockResolvedValue(pending(await client.snapshot()));
    render(<App client={client}/>);
    await act(async () => {});
    expect(screen.getByRole('status').textContent).toBe('Connecting to Spotify…');
    expect(screen.queryByText('Nothing playing')).toBeNull();
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Playlists' })); });
    expect(screen.getByRole('button', { name: 'Open Liked Songs' })).toBeTruthy();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Go back' })); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Search' })); });
    expect(screen.getByLabelText('Search Spotify')).toBeTruthy();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Go back' })); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'More' })); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Settings' })); });
    expect(screen.getByRole('heading', { name: 'Spotify account' })).toBeTruthy();
  });

  it('does not duplicate a pending initial RPC when the panel is reopened', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockReturnValue(status.promise);
    const view = render(<App client={client}/>);
    await advance(20000);
    expect(client.snapshot).toHaveBeenCalledTimes(1);
    view.rerender(<App client={client} visible={false}/>);
    view.rerender(<App client={client} visible/>);
    await advance(20000);
    expect(client.snapshot).toHaveBeenCalledTimes(1);
    await act(async () => { status.resolve(state); });
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
  });

  it('stops fast polling when hidden and after playback is confirmed', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    client.snapshot = vi.fn().mockResolvedValueOnce(pending(state)).mockResolvedValueOnce(pending(state)).mockResolvedValue(state);
    const view = render(<App client={client}/>);
    await act(async () => {});
    await advance(1000);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    view.rerender(<App client={client} visible={false}/>);
    await advance(30000);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    view.rerender(<App client={client} visible/>);
    await act(async () => {});
    expect(client.snapshot).toHaveBeenCalledTimes(3);
    expect(screen.queryByText('Connecting to Spotify…')).toBeNull();
    await advance(14999);
    expect(client.snapshot).toHaveBeenCalledTimes(3);
    await advance(1);
    expect(client.snapshot).toHaveBeenCalledTimes(4);
  });

  it('does not announce an empty player until the backend confirms it', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    client.snapshot = vi.fn().mockResolvedValueOnce(pending(state))
      .mockResolvedValue({ ...state, playback: null, refreshing: false, playbackPending: false, retryAfter: 0 });
    render(<App client={client}/>);
    await act(async () => {});
    expect(screen.queryByText('Nothing playing')).toBeNull();
    await advance(1000);
    expect(screen.getByRole('heading', { name: 'Nothing playing' })).toBeTruthy();
    await advance(14999);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
  });

  it('honors a pending snapshot cooldown instead of polling every second', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    client.snapshot = vi.fn().mockResolvedValueOnce({ ...pending(state), retryAfter: 5 }).mockResolvedValue(state);
    render(<App client={client}/>);
    await act(async () => {});
    await advance(4999);
    expect(client.snapshot).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
  });

  it('honors a rate-limit RPC cooldown even when Retry is pressed', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    client.snapshot = vi.fn().mockRejectedValueOnce(Object.assign(new Error('Please wait before retrying.'), { code: 'rate_limit', retryAfter: 30 }))
      .mockResolvedValue(state);
    render(<App client={client}/>);
    await act(async () => {});
    await advance(29000);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry connection' })); });
    expect(client.snapshot).toHaveBeenCalledTimes(1);
    await advance(999);
    expect(client.snapshot).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
  });

  it('keeps acknowledged pause through a pending response and rejects an older startup read', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    const oldRead = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce({ ...state, refreshing: true })
      .mockReturnValueOnce(oldRead.promise).mockResolvedValue(pending(state));
    client.command = vi.fn().mockResolvedValue(undefined);
    const view = render(<App client={client}/>);
    await act(async () => {});
    await advance(1000);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Pause' })); });
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(false);
    const position = view.container.querySelector('.sp-time-labels')!.textContent;
    await act(async () => { oldRead.resolve({ ...state, playback: { ...state.playback!, track: tracks[1] } }); });
    expect(screen.queryByRole('heading', { name: 'Silver Horizon' })).toBeNull();
    await advance(2000);
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(false);
    expect(view.container.querySelector('.sp-time-labels')!.textContent).toBe(position);
  });
});
