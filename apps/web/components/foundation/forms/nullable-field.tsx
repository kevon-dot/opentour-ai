'use client';

import { Button } from '../tokens';
import { requireLabel } from '../tokens/require-label';
import { TextField } from './text-field';
import './nullable-field.css';

export type NullableDraft =
  | { kind: 'value'; raw: string }
  | { kind: 'null' }
  | { kind: 'omitted' };

interface Choice {
  label: string;
  helper: string;
}

/** An absent choice is unavailable. All visible copy comes from the specific field. */
export interface NullableChoices {
  value?: Choice;
  null?: Choice & { display: string };
  omitted?: Choice & { display: string };
}

export interface NullableFieldProps {
  id: string;
  label: string;
  required?: boolean;
  mode: NullableDraft['kind'];
  /** Caller-owned draft, retained even when another mode is active. */
  raw: string;
  choices: NullableChoices;
  onChange: (next: NullableDraft) => void;
}

const orderedModes = ['value', 'null', 'omitted'] as const;

export function NullableField({ id, label, required = false, mode, raw, choices, onChange }: NullableFieldProps) {
  const permitted = orderedModes.filter(kind => choices[kind] !== undefined);
  if (!permitted.length) return null;
  requireLabel(label, 'nullable field label');
  const fullLabel = label + (required ? ' · Required' : ' · Optional');
  const state = mode === 'value' ? undefined : choices[mode];

  return <div className="ot-nullable" role="group" aria-label={fullLabel}>
    {mode === 'value' && choices.value
      ? <TextField id={id} label={label} required={required} value={raw} helper={choices.value.helper}
          onChange={text => onChange({ kind: 'value', raw: text })} />
      : <div className="ot-text-field">
          <span className="ot-text-field-label">{fullLabel}</span>
          {state && <>
            <span className="ot-text-field-label">{state.display}</span>
            <p className="ot-text-field-helper">{state.helper}</p>
          </>}
        </div>}
    {permitted.map(kind => <Button key={kind} label={choices[kind]!.label}
      variant={kind === mode ? 'primary' : 'secondary'} pressed={kind === mode}
      onPress={() => {
        if (kind !== mode) onChange(kind === 'value' ? { kind, raw } : { kind });
      }} />)}
  </div>;
}
