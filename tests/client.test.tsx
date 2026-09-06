import { expect, it, vi } from 'vitest';

const { dispatch } = vi.hoisted(() => ({ dispatch: vi.fn() }));
vi.mock('@decky/api', () => ({ callable: () => dispatch }));
import { client } from '../src/client';

it('preserves the backend retry contract for startup and provider rate limits', async () => {
  dispatch.mockResolvedValueOnce({ ok: false, error: 'Starting.', code: 'startup', retry_after: 0 })
    .mockResolvedValueOnce({ ok: false, error: 'Please wait.', code: 'rate_limit', retry_after: 37 });
  await expect(client.snapshot()).rejects.toMatchObject({ message: 'Starting.', code: 'startup', retryAfter: 0 });
  await expect(client.snapshot()).rejects.toMatchObject({ message: 'Please wait.', code: 'rate_limit', retryAfter: 37 });
});
