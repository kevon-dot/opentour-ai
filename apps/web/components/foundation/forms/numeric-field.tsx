import { TextField, type TextFieldProps } from './text-field';
import './numeric-field.css';

export interface NumericFieldProps extends Omit<TextFieldProps, 'inputMode' | 'describedBy'> {
  kind: 'integer' | 'decimal';
  /** Display copy from the caller's schema. The field does not validate or format the draft. */
  constraintText?: string;
  disabledReason?: string;
}

export function NumericField({ kind, constraintText, disabledReason, ...field }: NumericFieldProps) {
  const constraintId = constraintText ? field.id + '-constraints' : undefined;
  const reasonId = field.disabled && disabledReason ? field.id + '-reason' : undefined;

  return <div className="ot-numeric-field">
    <TextField {...field} inputMode={kind === 'integer' ? 'numeric' : 'decimal'}
      describedBy={[constraintId, reasonId].filter(Boolean).join(' ')} />
    {constraintId && <p id={constraintId}>{constraintText}</p>}
    {reasonId && <p id={reasonId}>{disabledReason}</p>}
  </div>;
}
