import { createContext } from 'react';
import type { ReactNode } from 'react';
export const FocusTarget = createContext('');
export interface ActionProps { children: ReactNode; label: string; className?: string; onClick(): void; disabled?: boolean; selected?: boolean; preferredFocus?: boolean; onSecondary?(): void; secondaryLabel?: string }
export interface GroupProps { children: ReactNode; className?: string; onBack?(): void; flow?: 'row' | 'column'; focusTarget?: string }
export interface InputProps { value: string; label: string; placeholder?: string; secret?: boolean; onChange(value: string): void; onSubmit?(): void }
export interface RangeProps { value: number; max: number; step?: number; label: string; valueLabel?: string; hideLabel?: boolean; edgeLabels?: [string, string]; disabled?: boolean; onChange(value: number): void }
