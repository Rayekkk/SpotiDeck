import { useContext, useEffect, useRef } from 'react';
import type { CSSProperties, KeyboardEvent } from 'react';
import { FocusTarget } from '../src/control-types';
import type { ActionProps, GroupProps, InputProps, RangeProps } from '../src/control-types';
export function Action({ children, label, className = '', onClick, disabled, selected, preferredFocus, onSecondary, secondaryLabel }: ActionProps) {
  const target = useContext(FocusTarget);
  const preferred = target ? target === label : !!preferredFocus;
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (preferred && !disabled) ref.current?.focus(); }, [preferred]);
  return <button ref={ref} type="button" aria-label={label} aria-pressed={selected} disabled={disabled}
    data-sp-preferred={preferred || undefined} title={onSecondary ? `X · ${secondaryLabel}` : undefined}
    className={`sp-action ${className}${disabled ? ' sp-disabled' : ''}`} onClick={onClick}
    onKeyDown={event => { if (event.key.toLowerCase() === 'x' && onSecondary && !disabled) { event.preventDefault(); event.stopPropagation(); onSecondary(); } }}>{children}</button>;
}
// Keyboard stand-in for Steam navigation; the plugin itself uses Decky's focus tree.
function navigate(event: KeyboardEvent<HTMLDivElement>) {
  if (!event.key.startsWith('Arrow')) return;
  const root = event.currentTarget;
  const active = document.activeElement as HTMLElement | null;
  if (!active || !root.contains(active)) return;
  const horizontal = event.key === 'ArrowLeft' || event.key === 'ArrowRight';
  if (active.matches('input:not([type=range])') || (horizontal && active.matches('input[type=range]'))) return;
  const controls = [...root.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)')];
  const lanes: HTMLElement[][] = [];
  const rowGroups = new Map<Element, HTMLElement[]>();
  for (const control of controls) {
    const row = control.closest('[data-sp-flow="row"]');
    if (!row) { lanes.push([control]); continue; }
    if (!rowGroups.has(row)) { const lane: HTMLElement[] = []; rowGroups.set(row, lane); lanes.push(lane); }
    rowGroups.get(row)!.push(control);
  }
  const laneIndex = lanes.findIndex(lane => lane.includes(active));
  if (laneIndex < 0) return;
  const lane = lanes[laneIndex];
  const direction = event.key === 'ArrowUp' || event.key === 'ArrowLeft' ? -1 : 1;
  let next: HTMLElement | undefined;
  if (horizontal) next = lane[lane.indexOf(active) + direction];
  else {
    const nextLane = lanes[laneIndex + direction];
    next = nextLane?.find(item => item.dataset.spPreferred === 'true') ?? nextLane?.[Math.min(lane.indexOf(active), nextLane.length - 1)];
  }
  event.preventDefault();
  next?.focus();
  next?.scrollIntoView?.({ block: 'nearest' });
}
export function Group({ children, className, onBack, flow = 'column', focusTarget }: GroupProps) {
  const content = <div className={className} data-sp-flow={flow} onKeyDown={event => {
    if (event.key === 'Escape' && onBack) { event.preventDefault(); event.stopPropagation(); onBack(); }
    else if (focusTarget !== undefined) navigate(event);
  }}>{children}</div>;
  return focusTarget === undefined ? content : <FocusTarget.Provider value={focusTarget}>{content}</FocusTarget.Provider>;
}
export function Input({ value, label, placeholder, secret, onChange, onSubmit }: InputProps) {
  return <div className="sp-input"><input aria-label={label} value={value} placeholder={placeholder} type={secret ? 'password' : 'text'} onChange={event => onChange(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') onSubmit?.(); }}/></div>;
}
export function Range({ value, max, step = 1, label, valueLabel, hideLabel, edgeLabels, disabled, onChange }: RangeProps) {
  return <label className={`sp-range${hideLabel ? ' sp-range-unlabelled' : ''}${disabled ? ' sp-range-disabled' : ''}`}><span className={`sp-range-caption${hideLabel ? ' sp-visually-hidden' : ''}`}><span>{label}</span>{valueLabel && <span aria-hidden="true">{valueLabel}</span>}</span><input type="range" aria-label={label} min={0} max={max} step={step} value={value} disabled={disabled} style={{ '--range-progress': `${value / Math.max(1, max) * 100}%` } as CSSProperties}
    onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); event.stopPropagation(); onChange(Math.max(0, Math.min(max, value + (event.key === 'ArrowRight' ? step : -step)))); } }}
    onChange={event => onChange(Number(event.target.value))}/>{edgeLabels && <span className="sp-time-labels"><span>{edgeLabels[0]}</span><span>{edgeLabels[1]}</span></span>}</label>;
}
export function openExternal(url: string) { window.open(url, '_blank', 'noopener,noreferrer'); }
