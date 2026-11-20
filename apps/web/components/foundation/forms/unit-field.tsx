import { requireLabel } from '../tokens/require-label';
import { TextField, type TextFieldProps } from './text-field';
import './unit-field.css';

export interface UnitFieldProps extends Omit<TextFieldProps, 'affix' | 'inputMode' | 'describedBy'> {
  kind: 'currency' | 'duration';
  /** Approved currency code or duration unit from the caller. */
  unit: string;
  disabledReason?: string;
}

/** The unit describes the caller's raw value and does not participate in its edits. */
export function UnitField({ kind, unit, disabledReason, ...field }: UnitFieldProps) {
  requireLabel(unit, 'field unit');
  const reasonId = field.disabled && disabledReason ? field.id + '-reason' : undefined;
  return <div className="ot-unit-field">
    <TextField {...field} inputMode="decimal" affix={{ text: unit, position: kind === 'currency' ? 'prefix' : 'suffix' }}
      describedBy={reasonId} />
    {reasonId && <p id={reasonId}>{disabledReason}</p>}
  </div>;
}
