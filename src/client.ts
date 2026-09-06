import { callable } from '@decky/api';
import type { Client } from './types';
type Result<T> = { ok: true; data: T } | { ok: false; error: string; code: string; retry_after: number };
export class ClientError extends Error {
  constructor(message: string, readonly code: string, readonly retryAfter: number) {
    super(message);
    this.name = 'ClientError';
  }
}
const call = callable<[string, Record<string, unknown>], Result<unknown>>('dispatch');
async function request<T>(method: string, args: Record<string, unknown> = {}): Promise<T> {
  const result = await call(method, args);
  if (!result.ok) throw new ClientError(result.error, result.code, result.retry_after);
  return result.data as T;
}
export const client: Client = {
  snapshot: () => request('snapshot'),
  library: (kind, offset = 0) => request('library', { kind, offset }),
  search: (query, kind, offset = 0) => request('search', { query, kind, offset }),
  tracks: (item, offset = 0) => request('tracks', { kind: item.kind, id: item.id, offset }),
  libraryState: uris => request('library_state', { uris }),
  pin: (action, value) => request('pin', { action, value }),
  playlist: (action, value) => request('playlist', { action, value }),
  timer: minutes => request('timer', { minutes }),
  preferences: action => request('preferences', { action, value: null }),
  devices: () => request('devices'), queue: () => request('queue'),
  command: (command, value) => request('command', { command, value }),
  connect: (clientId, mode = 'handheld') => request('connect', { client_id: clientId, mode }),
  cancelConnect: () => request('cancel_connect'),
  disconnect: () => request('disconnect'),
  player: (action, key) => request('player', { action, key }),
  audio: (action, value) => request('audio', { action, value }),
};
