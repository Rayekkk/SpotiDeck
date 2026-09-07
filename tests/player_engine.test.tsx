import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { previewClient } from '../preview/fixtures';

afterEach(cleanup);

it('switches engine setup without exposing or replacing the saved Soloist key', async () => {
  const client = previewClient();
  client.player = vi.fn(client.player);
  render(<App client={client}/>);
  await userEvent.click(await screen.findByRole('button', {name: 'More'}));
  await userEvent.click(await screen.findByRole('button', {name: 'Settings'}));
  expect(screen.getByLabelText('Soloist API key')).toBeTruthy();
  await userEvent.click(screen.getByRole('button', {name: 'Use Spotify Flatpak player'}));
  expect(client.player).toHaveBeenCalledWith('engine', 'flatpak');
  expect(await screen.findByRole('button', {name: 'Open Spotify app'})).toBeTruthy();
  expect(screen.queryByLabelText('Soloist API key')).toBeNull();
  expect(screen.getByText(/Manage downloads and audio quality/)).toBeTruthy();
  await userEvent.click(screen.getByRole('button', {name: 'Open Spotify app'}));
  expect(client.player).toHaveBeenCalledWith('open');
  await userEvent.click(screen.getByRole('button', {name: 'Use Soloist player'}));
  expect(await screen.findByLabelText('Soloist API key')).toBeTruthy();
  expect(client.player).not.toHaveBeenCalledWith('key', expect.anything());
});
