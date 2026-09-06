// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// Release flow adapted from LegionGo2Companion; state belongs to the plugin session.
import { useEffect, useState } from 'react';
import { version } from '../package.json';
import { Action } from './platform';
import type { Client, UpdateCheck, UpdateDownload } from './types';

interface UpdateState {
  busy: 'check' | 'download' | null;
  check?: UpdateCheck; download?: UpdateDownload; error?: string;
}
class UpdateController {
  state: UpdateState = { busy: null };
  listeners = new Set<(state: UpdateState) => void>();
  closed = false;
  constructor(private client: Client) {}
  publish(state: UpdateState) {
    if (this.closed) return;
    this.state = state;
    this.listeners.forEach(listener => listener(state));
  }
  async run(kind: 'check' | 'download') {
    if (this.closed || this.state.busy) return;
    const expected = this.state.check?.latest_version;
    if (kind === 'download' && (!this.state.check?.update_available || !this.state.check.download_available || !expected)) return;
    this.publish({ ...this.state, busy: kind, error: undefined });
    try {
      if (kind === 'check') {
        const result = await this.client.updatesCheck();
        if (!result.success) throw new Error(result.error || 'Could not check GitHub releases. Try again.');
        this.publish({ ...this.state, check: result, error: result.error,
          download: this.state.download?.version === result.latest_version ? this.state.download : undefined });
      } else {
        const result = await this.client.updatesDownload(expected!);
        if (!result.success || !result.path || result.version !== expected) {
          throw new Error(result.error || 'Could not confirm the downloaded ZIP. Try again.');
        }
        this.publish({ ...this.state, download: result });
      }
    } catch (error) {
      this.publish({ ...this.state, error: error instanceof Error ? error.message : 'The update request failed. Try again.' });
    } finally {
      this.publish({ ...this.state, busy: null });
    }
  }
}
const sessions = new WeakMap<Client, UpdateController>();
function controller(client: Client) {
  let current = sessions.get(client);
  if (!current) { current = new UpdateController(client); sessions.set(client, current); }
  return current;
}
export function disposeUpdates(client: Client) {
  const current = sessions.get(client);
  if (current) { current.closed = true; current.listeners.clear(); sessions.delete(client); }
}
function statusText(check?: UpdateCheck) {
  const installed = check?.current_version || version;
  if (!check) return `Installed version: ${installed}. Check GitHub when you need an update.`;
  if (check.no_release) return `No public release yet. Installed version: ${installed}.`;
  if (check.update_available) return check.download_available
    ? `Update available: ${check.latest_version}${check.size ? ` (${(check.size / 1048576).toFixed(1)} MB)` : ''}. Installed: ${installed}.`
    : `Version ${check.latest_version} has no verified installation ZIP yet.`;
  return check.latest_version === installed ? `Up to date. Version ${installed}.`
    : `No newer release. Installed: ${installed}. Latest public release: ${check.latest_version}.`;
}
export function UpdateSection({ client }: { client: Client }) {
  const session = controller(client);
  const [view, setView] = useState(session.state);
  useEffect(() => {
    setView(session.state);
    session.listeners.add(setView);
    return () => { session.listeners.delete(setView); };
  }, [session]);
  return <section aria-label="Plugin updates"><h3>Plugin updates</h3>
    <p role="status">{statusText(view.check)}</p>
    <Action label="Check for plugin updates" className="sp-secondary" disabled={view.busy !== null} onClick={() => void session.run('check')}>
      {view.busy === 'check' ? 'Checking GitHub…' : 'Check for updates'}
    </Action>
    {view.check?.update_available && view.check.download_available && <Action label={`Download plugin ${view.check.latest_version}`} className="sp-primary" disabled={view.busy !== null} onClick={() => void session.run('download')}>
      {view.busy === 'download' ? 'Downloading ZIP…' : `Download ${view.check.latest_version}`}
    </Action>}
    {view.error && <p role="alert">{view.error}</p>}
    {view.download?.path && <><p role="status">Version {view.download.version} downloaded.</p><code>{view.download.path}</code>
      <p>Install the ZIP through Decky Settings → Developer → Install Plugin from ZIP.</p></>}
  </section>;
}
