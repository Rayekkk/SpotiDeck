import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Mockup } from '../preview/Mockup';
import { previewClient } from '../preview/fixtures';

afterEach(() => { cleanup(); history.replaceState(null, '', '/'); });

describe('interactive handheld mockup', () => {
  it('operates the focused playback control with the physical A button and D-pad', async () => {
    const client = previewClient();
    const command = vi.spyOn(client, 'command');
    render(<Mockup client={client}/>);
    await screen.findByRole('button', { name: 'Pause' });
    await userEvent.click(screen.getByRole('button', { name: 'A · Wybierz' }));
    await screen.findByRole('button', { name: 'Play' });
    expect(command).toHaveBeenCalledWith('pause', undefined);
    await userEvent.click(screen.getByRole('button', { name: 'Krzyżak · Prawo' }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Next track' }));
    await userEvent.click(screen.getByRole('button', { name: 'A · Wybierz' }));
    await screen.findByRole('heading', { name: 'Silver Horizon' });
    expect(command).toHaveBeenCalledTimes(2);
  });

  it('adjusts music volume with the D-pad without sending a request for every step', async () => {
    const client = previewClient();
    const command = vi.spyOn(client, 'command');
    render(<Mockup client={client}/>);
    await screen.findByRole('button', { name: 'Pause' });
    await userEvent.click(screen.getByRole('button', { name: 'Krzyżak · Dół' }));
    expect(document.activeElement).toBe(screen.getByRole('slider', { name: 'Music volume' }));
    await userEvent.click(screen.getByRole('button', { name: 'Krzyżak · Prawo' }));
    await userEvent.click(screen.getByRole('button', { name: 'Krzyżak · Prawo' }));
    expect((screen.getByRole('slider', { name: 'Music volume' }) as HTMLInputElement).value).toBe('65');
    await waitFor(() => expect(command).toHaveBeenCalledWith('volume', 65));
    expect(command).toHaveBeenCalledTimes(1);
  });

  it('backs out through the plugin, Decky and QAM while retaining playback', async () => {
    const client = previewClient();
    const player = vi.spyOn(client, 'player');
    const command = vi.spyOn(client, 'command');
    render(<Mockup client={client}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Playlists' }));
    await screen.findByRole('button', { name: 'Open Liked Songs' });
    await userEvent.click(screen.getByRole('button', { name: 'B · Wstecz' }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Playlists' }));
    await userEvent.click(screen.getByRole('button', { name: 'B · Wstecz' }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Otwórz plugin SpotiDeck' }));
    await userEvent.click(screen.getByRole('button', { name: 'B · Wstecz' }));
    expect(screen.queryByRole('complementary', { name: 'Quick Access Menu' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Otwórz QAM' }));
    await userEvent.click(screen.getByRole('button', { name: 'Otwórz plugin SpotiDeck' }));
    await screen.findByRole('button', { name: 'Pause' });
    expect(player).not.toHaveBeenCalled();
    expect(command).not.toHaveBeenCalled();
  });

  it('keeps the same plugin route when zooming the screen and after closing QAM', async () => {
    render(<Mockup client={previewClient()}/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Playlists' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Open Night Drive' }));
    const song = await screen.findByRole('button', { name: 'Play Silver Horizon' });
    song.focus();
    await userEvent.click(screen.getByRole('button', { name: 'X · Dodaj do kolejki' }));
    await screen.findByText('Added to queue');
    await userEvent.click(screen.getByRole('button', { name: 'Ekran 16:10' }));
    expect(location.search).toBe('?view=screen');
    expect(screen.getByRole('button', { name: 'Play Silver Horizon' })).toBe(song);
    await userEvent.click(screen.getByRole('button', { name: 'Zamknij QAM' }));
    await userEvent.click(screen.getByRole('button', { name: 'Otwórz QAM' }));
    expect(screen.getByRole('button', { name: 'Play Silver Horizon' })).toBe(song);
    expect(document.activeElement).toBe(song);
  });
});
