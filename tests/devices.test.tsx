import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { App } from '../src/App';
import { previewClient } from '../preview/fixtures';
import type { Device } from '../src/types';

afterEach(() => { cleanup(); vi.useRealTimers(); });

it('confirms one click after stale device reads without resending the transfer', async () => {
  vi.useFakeTimers();
  const client = previewClient();
  const baseline = await client.snapshot();
  const original = baseline.playback!.device!;
  const phone: Device = { ...original, id: 'phone', name: 'Phone', active: false };
  let requested = false, confirmed = false;
  client.snapshot = vi.fn(async () => requested ? {
    ...baseline, playback: confirmed ? { ...baseline.playback!, device: { ...phone, active: true } } : null,
    playbackPending: !confirmed, deviceSelection: { device: phone, pending: !confirmed, error: null },
  } : baseline);
  let reads = 0;
  client.devices = vi.fn(async () => {
    reads++;
    if (requested && reads >= 4) confirmed = true;
    return [{ ...original, active: !confirmed }, { ...phone, active: confirmed }];
  });
  client.command = vi.fn(async () => { requested = true; });
  render(<App client={client}/>);
  await act(async () => {});
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Playback device' })); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Listen on Phone' })); });
  expect(screen.getByText('Connecting…')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Listen on Phone' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { vi.advanceTimersByTime(1000); });
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(screen.getByRole('button', { name: 'Listen on Phone' }).getAttribute('aria-pressed')).toBe('true');
  expect(client.command).toHaveBeenCalledExactlyOnceWith('transfer', 'phone');
});

it('retains the active device and exposes a failed transfer', async () => {
  vi.useFakeTimers();
  const client = previewClient();
  const original = (await client.snapshot()).playback!.device!;
  client.devices = vi.fn(async () => [original, { ...original, id: 'phone', name: 'Phone', active: false }]);
  client.command = vi.fn().mockRejectedValue(new Error('Device refused transfer'));
  render(<App client={client}/>);
  await act(async () => {});
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Playback device' })); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Listen on Phone' })); });
  expect(screen.getByText('Device refused transfer')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Listen on SpotiDeck' }).getAttribute('aria-pressed')).toBe('true');
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(client.command).toHaveBeenCalledTimes(1);
});

it('does not restart device polling when a hidden panel receives an older response', async () => {
  vi.useFakeTimers();
  const client = previewClient();
  const original = (await client.snapshot()).playback!.device!;
  let resolve!: (value: Device[]) => void;
  client.devices = vi.fn().mockReturnValue(new Promise<Device[]>(yes => { resolve = yes; }));
  const view = render(<App client={client}/>);
  await act(async () => {});
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Playback device' })); });
  view.rerender(<App client={client} visible={false}/>);
  await act(async () => { resolve([original]); });
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(client.devices).toHaveBeenCalledTimes(1);
});
