import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react';
import { App } from '../src/App';
import { Icon } from '../src/icons';
import type { Client } from '../src/types';
import { previewClient } from './fixtures';
import './mockup.css';

type Control = 'Enter' | 'Escape' | 'x' | 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight';
type SteamIconName = 'bell' | 'friends' | 'settings' | 'performance' | 'help' | 'plug' | 'wifi' | 'battery' | 'search' | 'back' | 'store';
function SteamIcon({ name, size = 30 }: { name: SteamIconName; size?: number }) {
  const paths: Record<SteamIconName, string> = {
    bell: 'M6 10a6 6 0 0 1 12 0v5l3 3H3l3-3Zm3 11h6',
    friends: 'M8 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm9-1a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM1 21v-4a7 7 0 0 1 14 0v4Zm16-7a5 5 0 0 1 6 4v3h-6',
    settings: 'm10 2-1 3-3 1-3-1-2 4 2 2v3l-2 2 2 4 3-1 3 1 1 3h4l1-3 3-1 3 1 2-4-2-2v-3l2-2-2-4-3 1-3-1-1-3ZM12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z',
    performance: 'M2 6h18v12H2Zm18 4h3v4h-3M5 9h12v6H5Z',
    help: 'M12 2a9 9 0 0 0-4 17l-2 4 7-3a9 9 0 0 0-1-18ZM9 8a3 3 0 0 1 6 0c0 2-3 2-3 5m0 3h.01',
    plug: 'M7 2v6m10-6v6M4 8h16v4a8 8 0 0 1-16 0Zm8 12v4',
    wifi: 'M3 3a18 18 0 0 1 18 18M3 9a12 12 0 0 1 12 12M3 15a6 6 0 0 1 6 6M3 21h.01',
    battery: 'M2 6h18v12H2Zm18 4h3v4h-3M5 9h11v6H5Z',
    search: 'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Zm-2 5 7 7',
    back: 'm13 5-7 7 7 7M6 12h15',
    store: 'M3 3h18l2 6-3 3-4-2-4 2-4-2-4 2-3-3ZM4 13v8h16v-8',
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]}/></svg>;
}

const rail: { icon: SteamIconName; title: string }[] = [
  { icon: 'bell', title: 'Notifications' }, { icon: 'friends', title: 'Friends' },
  { icon: 'settings', title: 'Quick settings' }, { icon: 'performance', title: 'Performance' },
  { icon: 'help', title: 'Help' }, { icon: 'plug', title: 'Decky' },
];

function SteamHome() {
  return <div className="steam-home" aria-hidden="true">
    <h2>Recent games</h2>
    <div className="steam-games">
      {[['hades', 'Hades'], ['elden-ring', 'ELDEN RING'], ['hollow-knight', 'Hollow Knight']].map(([file, title], i) => <div className={`steam-game ${i === 0 ? 'selected' : ''}`} key={file}><img src={`/assets/${file}.jpg`} alt=""/><span>{title}</span></div>)}
    </div>
    <div className="steam-home-tabs"><span>WHAT’S NEW</span><span>FRIENDS</span><span>RECOMMENDED</span></div>
    <div className="steam-news"><div><small>RECENTLY PLAYED</small><h3>Back to your next adventure</h3></div><div><small>YOUR LIBRARY</small><h3>Ready when you are</h3></div></div>
  </div>;
}

function useScreenScale() {
  const ref = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  useLayoutEffect(() => {
    const screen = ref.current;
    if (!screen) return;
    const measure = () => setScale(screen.getBoundingClientRect().width / 1280);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(screen);
    return () => observer.disconnect();
  }, []);
  return { ref, scale };
}

const hardware: { key: Control; label: string; x: number; y: number; w: number; h: number; shape?: string }[] = [
  { key: 'Enter', label: 'A · Wybierz', x: 1543, y: 279, w: 62, h: 62 },
  { key: 'Escape', label: 'B · Wstecz', x: 1592, y: 234, w: 62, h: 62 },
  { key: 'x', label: 'X · Dodaj do kolejki', x: 1497, y: 233, w: 62, h: 62 },
  { key: 'ArrowUp', label: 'Krzyżak · Góra', x: 182, y: 348, w: 66, h: 60, shape: 'direction' },
  { key: 'ArrowDown', label: 'Krzyżak · Dół', x: 182, y: 431, w: 66, h: 60, shape: 'direction' },
  { key: 'ArrowLeft', label: 'Krzyżak · Lewo', x: 136, y: 390, w: 60, h: 64, shape: 'direction' },
  { key: 'ArrowRight', label: 'Krzyżak · Prawo', x: 223, y: 390, w: 60, h: 64, shape: 'direction' },
];

export function Mockup({ client: suppliedClient }: { client?: Client }) {
  const [view, setView] = useState<'device' | 'screen'>(() => new URLSearchParams(location.search).get('view') === 'screen' ? 'screen' : 'device');
  const [mode, setMode] = useState('connected');
  const [qamOpen, setQamOpen] = useState(true);
  const [pluginOpen, setPluginOpen] = useState(true);
  const [legend, setLegend] = useState({ a: 'Pause', x: false });
  const client = useMemo(() => suppliedClient ?? previewClient(mode === 'connected'), [suppliedClient, mode]);
  const screen = useScreenScale();
  const canvas = useRef<HTMLDivElement>(null);
  const lastAction = useRef<HTMLElement | null>(null);
  const listButton = useRef<HTMLButtonElement>(null);
  const showApp = qamOpen && pluginOpen;

  function rememberFocus(target: HTMLElement) {
    if (!target.closest('.mockup-plugin .sp-root') || !target.matches('button, input')) return;
    lastAction.current = target;
    const name = target.getAttribute('aria-label') ?? '';
    const a = target.matches('input[type=range]') ? 'Adjust' : target.matches('input') ? 'Search'
      : ['Pause', 'Play'].includes(name) ? name : name.startsWith('Play ') ? 'Play'
      : name.startsWith('Open ') ? 'Open' : name.includes('track') ? 'Skip' : 'Select';
    setLegend({ a, x: !!target.title.startsWith('X ·') });
  }
  useEffect(() => {
    const root = canvas.current;
    if (!root) return;
    const observer = new MutationObserver(() => {
      if (lastAction.current?.isConnected) rememberFocus(lastAction.current);
    });
    observer.observe(root, { subtree: true, attributes: true, attributeFilter: ['aria-label', 'title'] });
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!qamOpen) { lastAction.current?.blur(); return; }
    if (!pluginOpen) { listButton.current?.focus({ preventScroll: true }); return; }
    const target = lastAction.current?.isConnected ? lastAction.current
      : canvas.current?.querySelector<HTMLElement>('.sp-root [data-sp-preferred=true]:not(:disabled), .sp-root button:not(:disabled)');
    target?.focus({ preventScroll: true });
  }, [qamOpen, pluginOpen]);

  function backToShell() {
    if (!qamOpen) return;
    if (pluginOpen) setPluginOpen(false);
    else setQamOpen(false);
  }
  function send(key: Control) {
    if (!qamOpen) { if (key === 'Enter') setQamOpen(true); return; }
    if (!pluginOpen) { if (key === 'Escape') backToShell(); else if (key === 'Enter') setPluginOpen(true); return; }
    const target = lastAction.current?.isConnected ? lastAction.current
      : canvas.current?.querySelector<HTMLElement>('.sp-root button:not(:disabled)');
    if (!target || (key !== 'Escape' && target.matches(':disabled'))) return;
    target.focus({ preventScroll: true });
    if (key === 'Enter' && target.matches('button')) target.click();
    else target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
  }
  function shellKeys(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.defaultPrevented) return;
    if (event.key === 'Escape') { event.preventDefault(); backToShell(); }
    else if (!(event.target as HTMLElement).matches('input, textarea') && ['a', 'b'].includes(event.key.toLowerCase())) {
      event.preventDefault(); send(event.key.toLowerCase() === 'a' ? 'Enter' : 'Escape');
    }
  }
  function changeView(next: 'device' | 'screen') {
    setView(next);
    const url = new URL(location.href);
    if (next === 'screen') url.searchParams.set('view', 'screen'); else url.searchParams.delete('view');
    history.replaceState(null, '', url);
  }

  return <div className="mockup-page">
    <header className="mockup-top"><div className="mockup-brand"><Icon name="spotify" size={24}/><h1>SpotiDeck</h1><span>Interaktywny mockup</span></div><p>Legion Go 2 <span>·</span> ekran 16:10</p></header>
    <div className={`mockup-stage view-${view}`}>
      <img className="mockup-hardware" src="/assets/legion-go-2-frame.png" alt="Obudowa Legiona Go 2 z klikalnymi przyciskami" draggable="false"/>
      <div className="mockup-screen" ref={screen.ref} aria-label="Ekran Legiona Go 2, proporcje 16:10">
        <div className={`gamescope ${qamOpen ? 'qam-open' : ''}`} ref={canvas} style={{ transform: `scale(${screen.scale})` }} onKeyDown={shellKeys} onFocusCapture={event => rememberFocus(event.target as HTMLElement)}>
          <SteamHome/>
          <div className="steam-dimmer"/>
          <div className="steam-status" aria-label="Przykładowy pasek statusu Steam"><SteamIcon name="search" size={29}/><SteamIcon name="wifi" size={27}/><span>85%</span><SteamIcon name="battery" size={32}/><span>10:30 AM</span><span className="steam-avatar">R</span></div>
          <aside className="steam-qam" aria-label="Quick Access Menu" hidden={!qamOpen}>
            <nav className="steam-rail" aria-label="Zakładki QAM">{rail.map(item => item.icon === 'plug'
              ? <button key={item.icon} className="steam-tab selected" aria-label="Decky - lista pluginów" onClick={() => setPluginOpen(false)}><SteamIcon name="plug" size={34}/></button>
              : <span key={item.icon} className="steam-tab" title={item.title}><SteamIcon name={item.icon} size={31}/></span>)}</nav>
            <div className="steam-qam-content">
              <div className="steam-plugin-view" hidden={!pluginOpen}>
                <header className="steam-qam-heading"><button aria-label="Wróć do listy Decky" onClick={() => setPluginOpen(false)}><SteamIcon name="back" size={26}/></button><h2>SpotiDeck</h2></header>
                <div className="mockup-plugin-scroll"><div className="mockup-plugin"><App key={mode} client={client} visible={showApp}/></div></div>
              </div>
              <div className="steam-plugin-list" hidden={pluginOpen}>
                <header className="steam-qam-heading"><h2>Decky</h2><span className="steam-list-tools" aria-hidden="true"><SteamIcon name="store" size={27}/><SteamIcon name="settings" size={25}/></span></header>
                <button className="steam-plugin-entry" ref={listButton} aria-label="Otwórz plugin SpotiDeck" onClick={() => setPluginOpen(true)}><Icon name="spotify" size={30}/><span>SpotiDeck</span></button>
              </div>
            </div>
          </aside>
          <footer className="steam-footer"><span className="steam-menu-hint"><b>STEAM</b> MENU</span><div>
            {showApp && legend.x && <button aria-label="X · Dodaj utwór do kolejki" onPointerDown={event => event.preventDefault()} onClick={() => send('x')}><b>X</b> ADD TO QUEUE</button>}
            <button aria-label="A · Aktywuj zaznaczenie" onPointerDown={event => event.preventDefault()} onClick={() => send('Enter')}><b>A</b> {!qamOpen ? 'QUICK ACCESS' : !pluginOpen ? 'SELECT' : legend.a.toUpperCase()}</button>
            {qamOpen && <button aria-label="B · Powrót" onPointerDown={event => event.preventDefault()} onClick={() => send('Escape')}><b>B</b> BACK</button>}
          </div></footer>
        </div>
      </div>
      <div className="mockup-hardware-controls" hidden={view !== 'device'}>{hardware.map(control => <button key={control.key} className={`hardware-button ${control.shape ?? ''}`} aria-label={control.label} title={control.label} style={{ left: `${control.x / 1672 * 100}%`, top: `${control.y / 941 * 100}%`, width: `${control.w / 1672 * 100}%`, height: `${control.h / 941 * 100}%` } as CSSProperties} onPointerDown={event => event.preventDefault()} onClick={() => send(control.key)}/>)}
        <button className="hardware-button hardware-qam" aria-label="Przycisk QAM na obudowie" title="Otwórz / zamknij QAM" onPointerDown={event => event.preventDefault()} onClick={() => setQamOpen(open => !open)}/>
      </div>
    </div>
    <div className="mockup-toolbar"><div className="mockup-view-switch" aria-label="Widok mockupu"><button aria-pressed={view === 'device'} onClick={() => changeView('device')}>Cały Legion</button><button aria-pressed={view === 'screen'} onClick={() => changeView('screen')}>Ekran 16:10</button></div><button className="mockup-toggle" aria-pressed={qamOpen} onClick={() => setQamOpen(open => !open)}>{qamOpen ? 'Zamknij QAM' : 'Otwórz QAM'}</button><label className="mockup-mode">Dane<select aria-label="Stan konta w podglądzie" value={mode} onChange={event => { setMode(event.target.value); setQamOpen(true); setPluginOpen(true); }}><option value="connected">Odtwarzanie</option><option value="setup">Pierwsze uruchomienie</option></select></label></div>
    <div className="mockup-notes"><p><kbd>↑ ↓ ← →</kbd> nawigacja <kbd>Enter / A</kbd> wybierz <kbd>Esc / B</kbd> wstecz <kbd>X</kbd> kolejka</p><p>Klikaj też przyciski na obudowie. Przykładowe dane · bez dźwięku.</p></div>
  </div>;
}
