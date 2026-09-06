import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import { artists, episodes, playlists, previewClient, shows, tracks } from '../preview/fixtures';

afterEach(() => { cleanup(); vi.useRealTimers(); });

async function library(label: string) {
  await userEvent.click(await screen.findByRole('button', { name: 'More' }));
  await userEvent.click(screen.getByRole('button', { name: 'Your library' }));
  await userEvent.click(screen.getByRole('button', { name: label }));
}
async function playlist() {
  await userEvent.click(await screen.findByRole('button', { name: 'Playlists' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Open Night Drive' }));
  await screen.findByRole('button', { name: 'Play Silver Horizon' });
}

describe('Expanded native music library', () => {
  it('refreshes the owned local queue while visible and stops when QAM is hidden', async () => {
    vi.useFakeTimers(); const client = previewClient(); const state = await client.snapshot();
    state.player.wsConnected = true; state.player.active = true;
    client.snapshot = vi.fn().mockResolvedValue(state);
    client.queue = vi.fn().mockResolvedValueOnce([tracks[1]]).mockResolvedValue([tracks[2]]);
    const view = render(<App client={client}/>); await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'More' })); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Queue' })); });
    expect(screen.getByRole('button', { name: 'Play Silver Horizon' })).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole('button', { name: 'Play Stay a Little Longer' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Play Silver Horizon' })).toBeNull();
    expect(client.queue).toHaveBeenCalledTimes(2);
    view.rerender(<App client={client} visible={false}/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(client.queue).toHaveBeenCalledTimes(2);
  });

  it('does not repeatedly request a remote queue when local event sync is unavailable', async () => {
    vi.useFakeTimers(); const client = previewClient(); const queue = vi.spyOn(client, 'queue');
    render(<App client={client}/>); await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'More' })); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Queue' })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(queue).toHaveBeenCalledTimes(1);
  });

  it('plays a selected playlist track with its original context and position', async () => {
    const client = previewClient(); const command = vi.spyOn(client, 'command');
    render(<App client={client}/>); await playlist();
    await userEvent.click(screen.getByRole('button', { name: 'Play Silver Horizon' }));
    expect(command).toHaveBeenCalledWith('play', { uri: tracks[1].uri, contextUri: playlists[0].uri, position: 1 });
    expect((await client.queue())[0].uri).toBe(tracks[2].uri);
  });

  it('uses the liked-song source instead of inventing a playlist URI', async () => {
    const client = previewClient(); const command = vi.spyOn(client, 'command');
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Playlists' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Open Liked Songs' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Play Silver Horizon' }));
    expect(command).toHaveBeenCalledWith('play', { uri: tracks[1].uri, source: 'liked', position: 1 });
  });

  it('opens an artist discography and restores that artist when returning from an album', async () => {
    const client = previewClient(); const load = vi.spyOn(client, 'tracks');
    render(<App client={client}/>); await library('Followed artists');
    await userEvent.click(await screen.findByRole('button', { name: `Open ${artists[0].title}` }));
    await userEvent.click(await screen.findByRole('button', { name: 'Open Night Drive' }));
    await screen.findByRole('button', { name: 'Play Silver Horizon' });
    await userEvent.click(screen.getByRole('button', { name: 'Go back' }));
    await screen.findByRole('heading', { name: artists[0].title });
    expect(load.mock.calls.some(([item]) => item.kind === 'artist')).toBe(true);
    expect(await screen.findByRole('button', { name: 'Open Night Drive' })).toBeTruthy();
  });

  it('resumes a podcast episode and offers 15-second controls on the compact player', async () => {
    const client = previewClient(); const command = vi.spyOn(client, 'command');
    render(<App client={client}/>); await library('Podcasts');
    await userEvent.click(await screen.findByRole('button', { name: `Open ${shows[0].title}` }));
    await userEvent.click(await screen.findByRole('button', { name: `Play ${episodes[0].title}` }));
    expect(command).toHaveBeenCalledWith('play', { uri: episodes[0].uri, positionMs: 120000 });
    for (let i = 0; i < 4; i++) await userEvent.click(screen.getByRole('button', { name: 'Go back' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Rewind episode 15 seconds' }));
    expect(command).toHaveBeenLastCalledWith('seek', expect.any(Number));
    expect(command.mock.calls[command.mock.calls.length - 1][1]).toBeGreaterThanOrEqual(105000);
    expect(command.mock.calls[command.mock.calls.length - 1][1]).toBeLessThan(107000);
  });

  it('reads saved status from Spotify and permits removing an existing saved track', async () => {
    const client = previewClient(); await client.command('save', tracks[0].uri);
    const command = vi.spyOn(client, 'command');
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'More' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Remove from Liked Songs' }));
    expect(command).toHaveBeenCalledWith('unsave', tracks[0].uri);
    expect(await screen.findByRole('button', { name: 'Save to Liked Songs' })).toBeTruthy();
  });

  it('does not claim a failed save succeeded', async () => {
    const client = previewClient(); client.command = vi.fn().mockRejectedValue(new Error('Permission required.'));
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'More' }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save to Liked Songs' }) as HTMLButtonElement).disabled).toBe(false));
    await userEvent.click(screen.getByRole('button', { name: 'Save to Liked Songs' }));
    await screen.findByText('Permission required.');
    expect(screen.queryByRole('button', { name: 'Remove from Liked Songs' })).toBeNull();
  });

  it('pins a collection and exposes it in the library without adding a home-screen row', async () => {
    const client = previewClient(); render(<App client={client}/>); await playlist();
    await userEvent.click(screen.getByRole('button', { name: 'Options for Night Drive' }));
    await userEvent.click(screen.getByRole('button', { name: 'Pin collection' }));
    await screen.findByRole('button', { name: 'Unpin collection' });
    expect((await client.library('pinned')).items[0].uri).toBe(playlists[0].uri);
    for (let i = 0; i < 3; i++) await userEvent.click(screen.getByRole('button', { name: 'Go back' }));
    expect(screen.getAllByRole('button')).toHaveLength(7);
    await library('Pinned collections');
    expect(await screen.findByRole('button', { name: 'Open Night Drive' })).toBeTruthy();
  });

  it('reorders with a snapshot ID, reloads the collection, then confirms removal', async () => {
    const client = previewClient(); const mutate = vi.spyOn(client, 'playlist');
    render(<App client={client}/>); await playlist();
    await userEvent.click(screen.getByRole('button', { name: 'Options for Silver Horizon' }));
    await userEvent.click(screen.getByRole('button', { name: 'Move up in playlist' }));
    expect(mutate).toHaveBeenCalledWith('reorder', expect.objectContaining({ id: playlists[0].id, position: 1, insertBefore: 0, snapshotId: '1' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Options for Silver Horizon' }));
    await userEvent.click(screen.getByRole('button', { name: 'Remove from playlist' }));
    expect(mutate).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm remove from playlist' }));
    expect(mutate).toHaveBeenCalledWith('remove', expect.objectContaining({ uri: tracks[1].uri, position: 0, snapshotId: '2' }));
    await screen.findByRole('button', { name: 'Play Midnight City Lights' });
    expect(screen.queryByRole('button', { name: 'Play Silver Horizon' })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Play Night Drive' }));
  });

  it('does not expose editing controls for a read-only playlist', async () => {
    const client = previewClient(); const original = client.tracks;
    client.tracks = async (item, offset) => ({ ...await original(item, offset), editable: false });
    render(<App client={client}/>); await playlist();
    await userEvent.click(screen.getByRole('button', { name: 'Options for Silver Horizon' }));
    expect(screen.queryByRole('button', { name: 'Remove from playlist' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Move up in playlist' })).toBeNull();
  });

  it('creates a private playlist, returns to the picker and adds the chosen song', async () => {
    const client = previewClient(); const mutate = vi.spyOn(client, 'playlist');
    render(<App client={client}/>); await playlist();
    await userEvent.click(screen.getByRole('button', { name: 'Options for Silver Horizon' }));
    await userEvent.click(screen.getByRole('button', { name: 'Add to playlist' }));
    await userEvent.click(screen.getByRole('button', { name: 'Create playlist' }));
    await userEvent.type(screen.getByLabelText('Playlist name'), 'Handheld evenings');
    await userEvent.click(screen.getByRole('button', { name: 'Create new playlist' }));
    expect(mutate).toHaveBeenCalledWith('create', { name: 'Handheld evenings', description: '', public: false });
    await userEvent.click(await screen.findByRole('button', { name: 'Add to Handheld evenings' }));
    expect(mutate).toHaveBeenLastCalledWith('add', expect.objectContaining({ uris: [tracks[1].uri] }));
    expect(await screen.findByText('Added to playlist')).toBeTruthy();
  });

  it('sets and cancels the backend sleep timer and rejects excessive custom durations', async () => {
    const client = previewClient(); const timer = vi.spyOn(client, 'timer');
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'More' }));
    await userEvent.click(screen.getByRole('button', { name: 'Sleep timer' }));
    await userEvent.click(screen.getByRole('button', { name: 'Pause after 30 minutes' }));
    expect(timer).toHaveBeenCalledWith(30);
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel sleep timer' }));
    expect(timer).toHaveBeenLastCalledWith(0);
    fireEvent.change(screen.getByLabelText('Sleep timer minutes'), { target: { value: '999' } });
    expect((screen.getByRole('button', { name: 'Set custom sleep timer' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('continues polling local playback events during a cloud rate limit', async () => {
    vi.useFakeTimers(); const client = previewClient(); const state = await client.snapshot();
    state.player.wsConnected = true; state.player.active = true;
    client.snapshot = vi.fn().mockResolvedValueOnce(state).mockResolvedValue({ ...state, retryAfter: 10 });
    render(<App client={client}/>); await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(client.snapshot).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(client.snapshot).toHaveBeenCalledTimes(11);
  });
});
