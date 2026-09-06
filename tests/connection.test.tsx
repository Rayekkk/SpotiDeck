import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import { previewClient } from '../preview/fixtures';

const { encode } = vi.hoisted(() => ({ encode: vi.fn().mockResolvedValue('data:image/png;base64,cHJldmlldw==') }));
vi.mock('qrcode', () => ({ default: { toDataURL: encode } }));
afterEach(() => { cleanup(); vi.useRealTimers(); encode.mockClear(); });

describe('Phone sign-in handoff', () => {
  it('renders a locally generated QR, exact redirect and certificate, and removes it on cancellation', async () => {
    const client = previewClient(false); const state = await client.snapshot();
    client.snapshot = async () => ({ ...state });
    client.connect = vi.fn().mockImplementation(async () => {
      state.connecting = true;
      return { url: 'https://192.0.2.1:43892/start?ticket=test-only', redirect: 'https://192.0.2.1:43892/callback', mode: 'phone', expiresAt: Date.now() + 600000, certificateFingerprint: 'TEST:FINGERPRINT' };
    });
    client.cancelConnect = vi.fn().mockImplementation(async () => { state.connecting = false; });
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Spotify' }));
    await userEvent.type(screen.getByLabelText('Spotify Client ID'), 'a'.repeat(32));
    await userEvent.click(screen.getByRole('button', { name: 'Sign in with phone QR' }));
    await userEvent.click(screen.getByRole('button', { name: 'Connect to Spotify' }));
    expect(client.connect).toHaveBeenCalledWith('a'.repeat(32), 'phone');
    expect(await screen.findByAltText('Scan with your phone to connect Spotify')).toBeTruthy();
    expect(screen.getByText('https://192.0.2.1:43892/callback')).toBeTruthy();
    expect(encode).toHaveBeenCalledWith('https://192.0.2.1:43892/start?ticket=test-only', expect.any(Object));
    await userEvent.click(screen.getByRole('button', { name: 'Show connection certificate' }));
    expect(screen.getByText('TEST:FINGERPRINT')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel Spotify sign-in' }));
    expect(client.cancelConnect).toHaveBeenCalledOnce();
    expect(screen.queryByAltText('Scan with your phone to connect Spotify')).toBeNull();
  });

  it('keeps the reauthorization QR while the old account is connected, then removes it on completion', async () => {
    const client = previewClient(); const state = await client.snapshot();
    state.clientId = 'a'.repeat(32); state.needsReauthorization = true;
    client.snapshot = async () => ({ ...state });
    client.connect = vi.fn().mockImplementation(async () => {
      state.connecting = true;
      return { url: 'https://192.0.2.1:43892/start?ticket=test-only', redirect: 'https://192.0.2.1:43892/callback', mode: 'phone', expiresAt: Date.now() + 600000 };
    });
    render(<App client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Update Spotify permissions' }));
    await userEvent.click(screen.getByRole('button', { name: 'Sign in with phone QR' }));
    await userEvent.click(screen.getByRole('button', { name: 'Connect to Spotify' }));
    expect(await screen.findByAltText('Scan with your phone to connect Spotify')).toBeTruthy();
    vi.useFakeTimers();
    // Restarting the action triggers a snapshot and establishes a fake-clock poll.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Connect to Spotify' })); });
    state.connecting = false; state.needsReauthorization = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.queryByAltText('Scan with your phone to connect Spotify')).toBeNull();
    expect(screen.getByText('Connected as Rayek')).toBeTruthy();
  });
});
