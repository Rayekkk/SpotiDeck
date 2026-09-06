import { Focusable, TextField, SliderField, Navigation } from '@decky/ui';
import { useContext } from 'react';
import type { ComponentType } from 'react';
import type { TextFieldProps } from '@decky/ui';
import { FocusTarget } from './control-types';
import type { ActionProps, GroupProps, InputProps, RangeProps } from './control-types';
export function Action({ children, label, className = '', onClick, disabled, selected, preferredFocus, onSecondary, secondaryLabel }: ActionProps) {
  const target = useContext(FocusTarget);
  const activate = () => { if (!disabled) onClick(); };
  return <Focusable role="button" aria-label={label} aria-disabled={disabled} aria-pressed={selected} tabIndex={disabled ? -1 : 0}
    className={`sp-action ${className}${disabled ? ' sp-disabled' : ''}`} focusClassName="sp-focused" onActivate={activate} onClick={activate}
    preferredFocus={target ? target === label : preferredFocus} onOKActionDescription={label}
    onSecondaryActionDescription={!disabled && onSecondary ? secondaryLabel : undefined}
    onSecondaryButton={onSecondary ? event => { event.stopPropagation(); if (!disabled) onSecondary(); } : undefined}>{children}</Focusable>;
}
export function Group({ children, className = '', onBack, flow = 'column', focusTarget }: GroupProps) {
  const content = <Focusable className={className} flow-children={flow} navEntryPreferPosition={4}
    onCancelActionDescription={onBack ? 'Back' : undefined}
    onCancel={onBack ? (event) => { event.stopPropagation(); onBack(); } : undefined}>{children}</Focusable>;
  return focusTarget === undefined ? content : <FocusTarget.Provider value={focusTarget}>{content}</FocusTarget.Provider>;
}
export function Input({ value, label, placeholder, secret, onChange, onSubmit }: InputProps) {
  const Field = TextField as ComponentType<TextFieldProps & { placeholder?: string; type?: string; autoComplete?: string }>;
  return <div className="sp-input"><Field value={value} aria-label={label} placeholder={placeholder}
    type={secret ? 'password' : 'text'} autoComplete="off" spellCheck={false}
    onChange={event => onChange(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') onSubmit?.(); }}/></div>;
}
export function Range({ value, max, step = 1, label, valueLabel, hideLabel, edgeLabels, disabled, onChange }: RangeProps) {
  return <div className={`sp-range${hideLabel ? ' sp-range-unlabelled' : ''}${disabled ? ' sp-range-disabled' : ''}`}>
    <SliderField label={<span className={`sp-range-caption${hideLabel ? ' sp-visually-hidden' : ''}`}><span>{label}</span>{valueLabel && <span aria-hidden="true">{valueLabel}</span>}</span>}
      layout="below" className="sp-native-slider" value={value} min={0} max={max} step={step} minimumDpadGranularity={step} showValue={false} bottomSeparator="none" disabled={disabled} onChange={onChange}/>
    {edgeLabels && <div className="sp-time-labels"><span>{edgeLabels[0]}</span><span>{edgeLabels[1]}</span></div>}
  </div>;
}
export const openExternal = (url: string) => Navigation.NavigateToExternalWeb(url);
