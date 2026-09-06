import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import { previewClient, tracks } from '../preview/fixtures';
import type { Page, Snapshot } from '../src/types';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('SpotiDeck interface', () => {
  it('shows a real first-run state without fictional music or enabled playback', async () => {
    render(<App client={previewClient(false)}/>);
    await screen.findByRole('heading', { name: 'Connect Spotify' });
    expect(screen.queryByText('Night Drive')).toBeNull();
    expect(screen.queryByLabelText('Pause')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Connect Spotify' }));
    expect(screen.getByLabelText('Spotify Client ID')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Connect to Spotify' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows compact playback immediately, pauses and skips', async () => {
    const client = previewClient();
    const command = vi.spyOn(client, 'command');
    render(<App client={client}/>);
    await screen.findByRole('button', { name: 'Pause' });
    expect(screen.getByRole('heading', { name: 'Midnight City Lights' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await screen.findByRole('button', { name: 'Play' });
    expect(command).toHaveBeenCalledWith('pause', undefined);
    await userEvent.click(screen.getByRole('button', { name: 'Next track' }));
    await screen.findByRole('heading', { name: 'Silver Horizon' });
    expect(screen.queryByRole('button', { name: 'Go back' })).toBeNull();
    expect(screen.getByRole('slider', { name: 'Track position' })).toBeTruthy();
    expect(screen.getByRole('slider', { name: 'Other audio' })).toBeTruthy();
    expect(screen.getAllByRole('button')).toHaveLength(7);
  });

  it('searches only on submit, then adds a result to queue with feedback', async () => {
    const client = previewClient();
    const search = vi.spyOn(client, 'search');
    const command = vi.spyOn(client, 'command');
    render(<App client={client}/>);
    await screen.findByRole('button', { name: 'Pause' });
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    await userEvent.type(screen.getByLabelText('Search Spotify'), 'Silver');
    expect(search).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Find music' }));
    const result = await screen.findByRole('button', { name: 'Play Silver Horizon' });
    result.focus();
    await userEvent.keyboard('x');
    await screen.findByText('Added to queue');
    expect(command).toHaveBeenCalledWith('queue', tracks[1].uri);
  });

  it('keeps an old search response from replacing newer results', async () => {
    const client = previewClient();
    let resolveOld!: (page: Page) => void;
    const pending = new Promise<Page>(resolve => { resolveOld = resolve; });
    client.search = vi.fn().mockReturnValueOnce(pending).mockResolvedValueOnce({ items: [tracks[1]], next: null, total: 1 });
    render(<App client={client}/>);
    await screen.findByRole('button', { name: 'Pause' });
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    const input = screen.getByLabelText('Search Spotify');
    await userEvent.type(input, 'Old{enter}');
    await userEvent.clear(input);
    await userEvent.type(input, 'New{enter}');
    await screen.findByRole('button', { name: 'Play Silver Horizon' });
    await act(async () => { resolveOld({ items: [tracks[2]], next: null, total: 1 }); await pending; });
    expect(screen.queryByRole('button', { name: 'Play Stay a Little Longer' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Play Silver Horizon' })).toBeTruthy();
  });

  it('browses playlists and liked tracks entirely within the panel', async () => {
    render(<App client={previewClient()}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Playlists' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Open Night Drive' }));
    await screen.findByRole('button', { name: 'Play Silver Horizon' });
    expect(screen.getByRole('heading', { name: 'Night Drive' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Go back' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Open Liked Songs' }));
    await screen.findByRole('button', { name: 'Play Silver Horizon' });
  });

  it('displays a denied playlist honestly and keeps its play-context button available', async () => {
    const client = previewClient();
    client.tracks = vi.fn().mockRejectedValue(new Error('Spotify denied access to this playlist.'));
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Playlists' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Open Night Drive' }));
    await screen.findByText('Spotify denied access to this playlist.');
    expect((screen.getByRole('button', { name: 'Play Night Drive' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('reports command failures without claiming a successful change', async () => {
    const client = previewClient();
    client.command = vi.fn().mockRejectedValue(new Error('Spotify is offline.'));
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Pause' }));
    await screen.findByRole('alert');
    expect(screen.getByText('Spotify is offline.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
  });

  it('acknowledges pause, freezes progress and unlocks resume before a slow status refresh', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    initial.playback!.disallows.resuming = true;
    const acknowledgement = deferred<void>();
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockReturnValue(status.promise);
    client.command = vi.fn().mockReturnValue(acknowledgement.promise);
    const view = render(<App client={client}/>);
    await act(async () => {});
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { acknowledgement.resolve(); });
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(false);
    const elapsed = view.container.querySelector('.sp-time-labels')!.textContent;
    const bar = (screen.getByRole('slider', { name: 'Track position' }) as HTMLInputElement).value;
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(view.container.querySelector('.sp-time-labels')!.textContent).toBe(elapsed);
    expect((screen.getByRole('slider', { name: 'Track position' }) as HTMLInputElement).value).toBe(bar);
    expect(client.snapshot).toHaveBeenCalledTimes(2);
  });

  it('allows quick resume and ignores reads that started before each acknowledged command', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    const oldPoll = deferred<Snapshot>();
    const afterPause = deferred<Snapshot>();
    const afterResume = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockReturnValueOnce(oldPoll.promise)
      .mockReturnValueOnce(afterPause.promise).mockReturnValue(afterResume.promise);
    client.command = vi.fn().mockResolvedValue(undefined);
    const view = render(<App client={client}/>);
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Pause' })); });
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { oldPoll.resolve({ ...initial, playback: { ...initial.playback!, track: tracks[1] } }); });
    expect(screen.queryByRole('heading', { name: 'Silver Horizon' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Play' })); });
    expect(client.command).toHaveBeenNthCalledWith(1, 'pause', undefined);
    expect(client.command).toHaveBeenNthCalledWith(2, 'resume', undefined);
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { afterPause.resolve({ ...initial, playback: { ...initial.playback!, playing: false } }); });
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
    const elapsed = view.container.querySelector('.sp-time-labels')!.textContent;
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(view.container.querySelector('.sp-time-labels')!.textContent).not.toBe(elapsed);
    expect(client.snapshot).toHaveBeenCalledTimes(4);
  });

  it('keeps playing after a rejected pause and unlocks retry while status is pending', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockReturnValue(status.promise);
    client.command = vi.fn().mockRejectedValueOnce(new Error('Pause was rejected.')).mockResolvedValue(undefined);
    const view = render(<App client={client}/>);
    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Pause' })); });
    expect(screen.getByRole('alert').textContent).toContain('Pause was rejected.');
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
    const elapsed = view.container.querySelector('.sp-time-labels')!.textContent;
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(view.container.querySelector('.sp-time-labels')!.textContent).not.toBe(elapsed);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Pause' })); });
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(false);
    expect(client.command).toHaveBeenCalledTimes(2);
  });

  it('keeps known playback controllable after a failed status request', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockRejectedValueOnce(new Error('Status request timed out.')).mockReturnValue(status.promise);
    client.command = vi.fn().mockResolvedValue(undefined);
    render(<App client={client}/>);
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByRole('alert').textContent).toContain('Status request timed out.');
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Pause' })); });
    expect((screen.getByRole('button', { name: 'Play' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('distinguishes a playback read failure from a confirmed missing device', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial)
      .mockResolvedValueOnce({ ...initial, playback: null, playbackError: 'Playback temporarily unavailable.' })
      .mockResolvedValue({ ...initial, playback: { ...initial.playback!, device: null } });
    render(<App client={client}/>);
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByText('Playback temporarily unavailable.')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('coalesces seeking instead of sending every slider movement', async () => {
    const client = previewClient();
    const command = vi.spyOn(client, 'command');
    render(<App client={client}/>);
    const slider = await screen.findByRole('slider', { name: 'Track position' });
    fireEvent.change(slider, { target: { value: '20000' } });
    fireEvent.change(slider, { target: { value: '40000' } });
    expect(command).not.toHaveBeenCalled();
    await waitFor(() => expect(command).toHaveBeenCalledWith('seek', 40000));
    expect(command).toHaveBeenCalledTimes(1);
  });

  it('sends no pending seek after navigating away', async () => {
    const client = previewClient();
    const command = vi.spyOn(client, 'command');
    render(<App client={client}/>);
    fireEvent.change(await screen.findByRole('slider', { name: 'Track position' }), { target: { value: '90000' } });
    await userEvent.click(screen.getByRole('button', { name: 'More' }));
    await new Promise(resolve => setTimeout(resolve, 400));
    expect(command).not.toHaveBeenCalled();
  });

  it('navigates transport horizontally and menus vertically, then restores focus on Back', async () => {
    render(<App client={previewClient()}/>);
    const pause = await screen.findByRole('button', { name: 'Pause' });
    expect(document.activeElement).toBe(pause);
    await userEvent.keyboard('{ArrowRight}');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Next track' }));
    await userEvent.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(screen.getByRole('slider', { name: 'Music volume' }));
    await userEvent.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(screen.getByRole('slider', { name: 'Other audio' }));
    await userEvent.keyboard('{ArrowDown}{Enter}');
    await screen.findByRole('button', { name: 'Open Liked Songs' });
    await userEvent.keyboard('{ArrowDown}{Enter}');
    await screen.findByRole('button', { name: 'Play Night Drive' });
    await userEvent.keyboard('{Escape}');
    const playlist = await screen.findByRole('button', { name: 'Open Night Drive' });
    expect(document.activeElement).toBe(playlist);
    await userEvent.keyboard('{Escape}');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Playlists' }));
  });

  it('keeps library navigation available without an active device', async () => {
    const client = previewClient();
    const state = await client.snapshot();
    state.playback!.device = null;
    client.snapshot = vi.fn().mockResolvedValue(state);
    render(<App client={client}/>);
    const playlists = await screen.findByRole('button', { name: 'Playlists' });
    expect(document.activeElement).toBe(playlists);
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.keyboard('{Enter}');
    await screen.findByRole('button', { name: 'Open Liked Songs' });
  });

  it('does not poll when hidden and never stops audio when the UI unmounts', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const snapshot = vi.spyOn(client, 'snapshot');
    const player = vi.spyOn(client, 'player');
    const view = render(<App client={client} visible={false}/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(snapshot).not.toHaveBeenCalled();
    view.rerender(<App client={client} visible/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(snapshot).toHaveBeenCalledTimes(1);
    view.rerender(<App client={client} visible={false}/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(snapshot).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(player).not.toHaveBeenCalled();
  });

  it('opens playback devices directly between Search and More and removes old More controls', async () => {
    render(<App client={previewClient()}/>);
    await screen.findByRole('button', { name: 'Pause' });
    expect(screen.getAllByRole('button').map(button => button.getAttribute('aria-label')).slice(-4))
      .toEqual(['Playlists', 'Search', 'Playback device', 'More']);
    await userEvent.click(screen.getByRole('button', { name: 'Playback device' }));
    await screen.findByRole('button', { name: 'Listen on SpotiDeck' });
    await userEvent.keyboard('{Escape}');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Playback device' }));
    await userEvent.click(screen.getByRole('button', { name: 'More' }));
    expect(screen.queryByRole('slider', { name: 'Track position' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Playback device' })).toBeNull();
  });

  it('adjusts other audio without an active Spotify device or a Spotify command', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const state = await client.snapshot();
    state.playback!.device = null;
    client.snapshot = vi.fn().mockResolvedValue(state);
    const audio = vi.spyOn(client, 'audio');
    const command = vi.spyOn(client, 'command');
    render(<App client={client}/>);
    await act(async () => {});
    expect((screen.getByRole('slider', { name: 'Music volume' }) as HTMLInputElement).disabled).toBe(true);
    const other = screen.getByRole('slider', { name: 'Other audio' }) as HTMLInputElement;
    expect(other.disabled).toBe(false);
    fireEvent.change(other, { target: { value: '40' } });
    fireEvent.change(other, { target: { value: '30' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(audio).toHaveBeenCalledExactlyOnceWith('other', 30);
    expect(command).not.toHaveBeenCalled();
  });

  it('switches to a persisted balance slider with both sources at full volume in the center', async () => {
    const client = previewClient();
    const audio = vi.spyOn(client, 'audio');
    const view = render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'More' }));
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }));
    expect(screen.getByRole('button', { name: 'Separate volume sliders' }).getAttribute('aria-pressed')).toBe('true');
    await userEvent.click(screen.getByRole('button', { name: 'Game / Spotify balance mode' }));
    await waitFor(() => expect(audio).toHaveBeenCalledWith('mode', 'balance'));
    await userEvent.keyboard('{Escape}{Escape}');
    const balance = screen.getByRole('slider', { name: 'Game / Spotify balance' }) as HTMLInputElement;
    expect(balance.value).toBe('50');
    expect(screen.getByText('Game 100%')).toBeTruthy();
    expect(screen.getByText('Spotify 100%')).toBeTruthy();
    expect(screen.queryByRole('slider', { name: 'Music volume' })).toBeNull();
    expect(screen.queryByRole('slider', { name: 'Other audio' })).toBeNull();
    fireEvent.change(balance, { target: { value: '100' } });
    expect(screen.getByText('Game 0%')).toBeTruthy();
    expect(screen.getByText('Spotify 100%')).toBeTruthy();
    await waitFor(() => expect(audio).toHaveBeenCalledWith('balance', 100));
    fireEvent.change(balance, { target: { value: '0' } });
    expect(screen.getByText('Game 100%')).toBeTruthy();
    expect(screen.getByText('Spotify 0%')).toBeTruthy();
    await waitFor(() => expect(audio).toHaveBeenCalledWith('balance', 0));
    view.unmount();
    render(<App client={client}/>);
    expect((await screen.findByRole('slider', { name: 'Game / Spotify balance' }) as HTMLInputElement).value).toBe('0');
  });

  it.each([
    [0, 0, 100],
    [25, 50, 100],
    [50, 100, 100],
    [75, 100, 50],
    [100, 100, 0],
  ])('balance position %i shows Spotify %i%% and other audio %i%% before a slow refresh', async (position, spotify, other) => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockReturnValue(status.promise);
    client.audio = vi.fn().mockResolvedValue(undefined);
    render(<App client={client}/>);
    await act(async () => {});
    fireEvent.click(screen.getByRole('button', { name: 'More' }));
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Game / Spotify balance mode' }));
    await act(async () => {});
    expect(screen.getByText('At the center, both are at 100%. Move left to lower Spotify, or right to lower all other audio.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Go back' }));
    fireEvent.click(screen.getByRole('button', { name: 'Go back' }));
    expect(screen.getByText('Game 100%')).toBeTruthy();
    expect(screen.getByText('Spotify 100%')).toBeTruthy();
    // The acknowledgement must update both volumes even if no status read completes.
    fireEvent.change(screen.getByRole('slider', { name: 'Game / Spotify balance' }), { target: { value: String(position) } });
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(screen.getByText(`Game ${other}%`)).toBeTruthy();
    expect(screen.getByText(`Spotify ${spotify}%`)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'More' }));
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Separate volume sliders' }));
    await act(async () => {});
    fireEvent.click(screen.getByRole('button', { name: 'Go back' }));
    fireEvent.click(screen.getByRole('button', { name: 'Go back' }));
    expect((screen.getByRole('slider', { name: 'Music volume' }) as HTMLInputElement).value).toBe(String(spotify));
    expect((screen.getByRole('slider', { name: 'Other audio' }) as HTMLInputElement).value).toBe(String(other));

    // The interactive preview applies the same curve and preserves it on mode changes.
    const preview = previewClient();
    await preview.audio('mode', 'balance');
    expect((await preview.snapshot()).playback!.device!.volume).toBe(100);
    await preview.audio('balance', position);
    await preview.audio('mode', 'separate');
    const state = await preview.snapshot();
    expect(state.audio.otherVolume).toBe(other);
    expect(state.playback!.device!.volume).toBe(spotify);
  });

  it('leaves music controllable when the local mixer is unavailable', async () => {
    const client = previewClient();
    const state = await client.snapshot();
    state.audio = { ...state.audio, supported: false, error: 'Local audio is unavailable.' };
    client.snapshot = vi.fn().mockResolvedValue(state);
    render(<App client={client}/>);
    await screen.findByRole('button', { name: 'Pause' });
    expect((screen.getByRole('slider', { name: 'Other audio' }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('slider', { name: 'Music volume' }) as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText('Local audio is unavailable.')).toBeTruthy();
  });

  it('keeps newer volume movements while an earlier change is awaiting acknowledgement', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    const first = deferred<void>();
    const second = deferred<void>();
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockReturnValue(status.promise);
    client.command = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<App client={client}/>);
    await act(async () => {});
    const volume = screen.getByRole('slider', { name: 'Music volume' }) as HTMLInputElement;
    fireEvent.change(volume, { target: { value: '60' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(client.command).toHaveBeenCalledExactlyOnceWith('volume', 60);
    expect(volume.disabled).toBe(false);
    fireEvent.change(volume, { target: { value: '75' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(volume.value).toBe('75');
    expect(client.command).toHaveBeenCalledTimes(1);
    await act(async () => { first.resolve(); });
    expect(client.command).toHaveBeenNthCalledWith(2, 'volume', 75);
    expect(volume.value).toBe('75');
    await act(async () => { second.resolve(); });
    expect(volume.value).toBe('75');
    expect(volume.disabled).toBe(false);
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('acknowledges a seek without waiting for a playback refresh', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    initial.playback!.playing = false;
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockReturnValue(status.promise);
    client.command = vi.fn().mockResolvedValue(undefined);
    render(<App client={client}/>);
    await act(async () => {});
    const seek = screen.getByRole('slider', { name: 'Track position' }) as HTMLInputElement;
    fireEvent.change(seek, { target: { value: '120000' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(seek.value).toBe('120000');
    expect(screen.getByText('2:00')).toBeTruthy();
    expect(client.command).toHaveBeenCalledExactlyOnceWith('seek', 120000);
  });

  it('cancels a pending seek when another device takes over the same track', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    initial.refreshing = true;
    const next = structuredClone(initial);
    next.playback!.device!.id = 'external-device';
    next.playback!.device!.name = 'Phone';
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(next);
    client.command = vi.fn();
    render(<App client={client}/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(900); });
    fireEvent.change(screen.getByRole('slider', { name: 'Track position' }), { target: { value: '120000' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(screen.getByText('Phone')).toBeTruthy();
    expect(client.command).not.toHaveBeenCalled();
  });

  it('cancels a pending seek before a next-track operation', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    client.command = vi.fn().mockReturnValue(new Promise(() => {}));
    render(<App client={client}/>);
    await act(async () => {});
    fireEvent.change(screen.getByRole('slider', { name: 'Track position' }), { target: { value: '120000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Next track' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(client.command).toHaveBeenCalledExactlyOnceWith('next', undefined);
  });

  it('requires a controllable Spotify volume device for balance without disabling playback controls', async () => {
    const client = previewClient();
    const state = await client.snapshot();
    state.audio.mode = 'balance';
    state.playback!.device!.supportsVolume = false;
    client.snapshot = vi.fn().mockResolvedValue(state);
    render(<App client={client}/>);
    const balance = await screen.findByRole('slider', { name: 'Game / Spotify balance' }) as HTMLInputElement;
    expect(balance.disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText('Choose a Spotify playback device with volume control to adjust the balance.')).toBeTruthy();
  });

  it('restores the last confirmed volume after a rejected adjustment and permits retry', async () => {
    vi.useFakeTimers();
    const client = previewClient();
    const initial = await client.snapshot();
    const status = deferred<Snapshot>();
    client.snapshot = vi.fn().mockResolvedValueOnce(initial).mockReturnValue(status.promise);
    client.audio = vi.fn().mockRejectedValueOnce(new Error('Mixer is busy.')).mockResolvedValue(undefined);
    render(<App client={client}/>);
    await act(async () => {});
    const other = screen.getByRole('slider', { name: 'Other audio' }) as HTMLInputElement;
    fireEvent.change(other, { target: { value: '30' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(screen.getByRole('alert').textContent).toContain('Mixer is busy.');
    expect(other.value).toBe('100');
    expect(other.disabled).toBe(false);
    fireEvent.change(other, { target: { value: '40' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(other.value).toBe('40');
    expect(client.audio).toHaveBeenNthCalledWith(2, 'other', 40);
  });
});
