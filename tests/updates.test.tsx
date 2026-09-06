import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { UpdateSection, disposeUpdates } from '../src/updates';
import { previewClient } from '../preview/fixtures';
import type { UpdateCheck, UpdateDownload } from '../src/types';

afterEach(cleanup);
const available: UpdateCheck = { success: true, current_version: '1.0.0', latest_version: '1.0.1', update_available: true, download_available: true, size: 2097152 };
function deferred<T>() {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>(yes => { resolve = yes; }), resolve };
}
async function click(label: string) {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: label })); });
}

it('checks only on request and treats a repository without releases as a normal state', async () => {
  const client = previewClient();
  client.updatesCheck = vi.fn(client.updatesCheck);
  render(<UpdateSection client={client}/>);
  expect(client.updatesCheck).not.toHaveBeenCalled();
  await click('Check for plugin updates');
  expect(client.updatesCheck).toHaveBeenCalledOnce();
  expect(screen.getByText(/No public release yet/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /Download plugin/ })).toBeNull();
});

it('downloads the selected version once and retains progress and the result across page remounts', async () => {
  const client = previewClient();
  client.updatesCheck = vi.fn().mockResolvedValue(available);
  const download = deferred<UpdateDownload>();
  client.updatesDownload = vi.fn(() => download.promise);
  let page = render(<UpdateSection client={client}/>);
  await click('Check for plugin updates');
  await click('Download plugin 1.0.1');
  await click('Download plugin 1.0.1');
  expect(client.updatesDownload).toHaveBeenCalledExactlyOnceWith('1.0.1');
  page.unmount();
  page = render(<UpdateSection client={client}/>);
  expect(screen.getByText('Downloading ZIP…')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Check for plugin updates' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => download.resolve({ success: true, version: '1.0.1', path: '/home/deck/Downloads/SpotiDeck-1.0.1.zip' }));
  expect(screen.getByText('/home/deck/Downloads/SpotiDeck-1.0.1.zip')).toBeTruthy();
  expect(screen.getByText(/Install the ZIP through Decky/)).toBeTruthy();
  page.unmount();
  render(<UpdateSection client={client}/>);
  expect(screen.getByText('Version 1.0.1 downloaded.')).toBeTruthy();
});

it('does not offer a download for an equal, older or unverifiable release', async () => {
  const client = previewClient();
  client.updatesCheck = vi.fn()
    .mockResolvedValueOnce({ ...available, latest_version: '1.0.0', update_available: false, download_available: false })
    .mockResolvedValueOnce({ ...available, latest_version: '0.9.0', update_available: false, download_available: false })
    .mockResolvedValueOnce({ ...available, download_available: false, error: 'Missing trusted digest.' });
  render(<UpdateSection client={client}/>);
  await click('Check for plugin updates');
  expect(screen.getByText('Up to date. Version 1.0.0.')).toBeTruthy();
  await click('Check for plugin updates');
  expect(screen.getByText(/No newer release/)).toBeTruthy();
  await click('Check for plugin updates');
  expect(screen.getByRole('alert').textContent).toBe('Missing trusted digest.');
  expect(screen.queryByRole('button', { name: /Download plugin/ })).toBeNull();
});

it('shows failed checks and allows retry without duplicate concurrent requests', async () => {
  const client = previewClient();
  const pending = deferred<UpdateCheck>();
  client.updatesCheck = vi.fn(() => pending.promise);
  render(<UpdateSection client={client}/>);
  await click('Check for plugin updates');
  await click('Check for plugin updates');
  expect(client.updatesCheck).toHaveBeenCalledOnce();
  await act(async () => pending.resolve({ ...available, success: false, error: 'GitHub rate limit reached.' }));
  expect(screen.getByRole('alert').textContent).toBe('GitHub rate limit reached.');
  client.updatesCheck = vi.fn().mockResolvedValue(available);
  await click('Check for plugin updates');
  expect(screen.queryByRole('alert')).toBeNull();
  expect(screen.getByRole('button', { name: 'Download plugin 1.0.1' })).toBeTruthy();
});

it('does not report a failed or mismatched download as completed', async () => {
  const client = previewClient();
  client.updatesCheck = vi.fn().mockResolvedValue(available);
  client.updatesDownload = vi.fn()
    .mockResolvedValueOnce({ success: false, error: 'Release changed. Check again.' })
    .mockResolvedValueOnce({ success: true, version: '1.0.2', path: '/wrong.zip' });
  render(<UpdateSection client={client}/>);
  await click('Check for plugin updates');
  await click('Download plugin 1.0.1');
  expect(screen.getByRole('alert').textContent).toMatch(/Release changed/);
  await click('Download plugin 1.0.1');
  expect(screen.getByRole('alert').textContent).toMatch(/Could not confirm/);
  expect(screen.queryByText(/downloaded\./)).toBeNull();
});

it('ignores completion from an unloaded plugin session', async () => {
  const client = previewClient();
  const pending = deferred<UpdateCheck>();
  client.updatesCheck = vi.fn(() => pending.promise);
  const page = render(<UpdateSection client={client}/>);
  await click('Check for plugin updates');
  disposeUpdates(client);
  page.unmount();
  render(<UpdateSection client={client}/>);
  await act(async () => pending.resolve(available));
  expect(screen.queryByRole('button', { name: /Download plugin/ })).toBeNull();
  expect(screen.getByText(/Installed version: 1.0.0. Check GitHub/)).toBeTruthy();
});
