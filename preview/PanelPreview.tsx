import { useMemo, useState } from 'react';
import { App } from '../src/App';
import { Icon } from '../src/icons';
import { previewClient } from './fixtures';
import './preview.css';
export function PanelPreview() {
  const [mode, setMode] = useState('connected');
  const [width, setWidth] = useState(340);
  const client = useMemo(() => previewClient(mode === 'connected'), [mode]);
  return <div className="preview-layout">
    <p className="preview-caption">Podgląd panelu QAM · przykładowe dane · bez dźwięku</p>
    <div className="preview-device" style={{ width }}>
      <div className="preview-decky"><span>QUICK ACCESS</span><span>DECKY</span></div>
      <div className="preview-plugin-title"><Icon name="spotify" size={21}/><span>SpotiDeck</span></div>
      <div className="preview-viewport"><App key={mode} client={client}/></div>
    </div>
    <p className="preview-help">↑ ↓ Wybór · ← → Sterowanie · Enter / A Wybierz<br/>Esc / B Wstecz · X Dodaj utwór do kolejki</p>
    <div className="preview-controls"><label>Widok<select aria-label="Preview state" value={mode} onChange={e => setMode(e.target.value)}><option value="connected">Odtwarzanie</option><option value="setup">Pierwsze uruchomienie</option></select></label><label>Szerokość QAM<select aria-label="Panel width" value={width} onChange={e => setWidth(Number(e.target.value))}><option value={340}>340 px</option><option value={320}>320 px</option><option value={380}>380 px</option></select></label></div>
  </div>;
}
