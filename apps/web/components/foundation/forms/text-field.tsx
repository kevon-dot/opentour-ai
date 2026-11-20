import { requireLabel } from '../tokens/require-label';
import './text-field.css';

interface ControlProps {
  /** Placed on the native input itself, never on a wrapper. */
  id: string;
  /** Raw text exactly as the caller holds it. It is never parsed, coerced or trimmed. */
  value: string;
  /** Receives the native input's raw text after every edit. */
  onChange: (value: string) => void;
  /** Called when the native input loses focus, so the caller can validate the raw value it already holds. */
  onBlur?: () => void;
  required?: boolean;
  disabled?: boolean;
  readOnly?: boolean;
  inputMode?: 'numeric' | 'decimal';
  /** Additional caller-rendered descriptions, appended to the built-in helper and error. */
  describedBy?: string;
}

export interface TextFieldProps extends ControlProps {
  /** Visible label without a suffix; the requirement suffix is appended here. */
  label: string;
  helper?: string;
  /** Fixed unit next to the editable raw value. It is part of the input description. */
  affix?: { text: string; position: 'prefix' | 'suffix' };
  /** Caller's explanation of an invalid value. Shown in addition to the helper, never in place of it. */
  error?: string;
}

export interface InputProps extends ControlProps {
  /** Accessible name. The standalone control draws no visible label of its own. */
  label: string;
  invalid?: boolean;
}

/** The id seeds the label, helper and error references, so it has to be a single IDREF token. */
function requireIdentity(id: string): void {
  if (typeof id !== 'string' || id.length === 0 || /\s/.test(id)) {
    throw new Error('A foundation form control identity without whitespace is required.');
  }
}

function NativeInput({ className, name, id, value, onChange, onBlur, required = false, disabled = false, invalid = false,
  readOnly, inputMode, describedBy, size }: ControlProps & { className: string; name?: string; invalid?: boolean; size?: number }) {
  return <input className={className} id={id} type="text" aria-label={name} required={required} disabled={disabled}
    size={size} readOnly={readOnly} inputMode={inputMode} aria-invalid={invalid || undefined} aria-describedby={describedBy || undefined} value={value}
    onChange={event => onChange(event.currentTarget.value)} onBlur={onBlur ? () => onBlur() : undefined} />;
}

/**
 * Labelled single-line text field. The caller owns the raw value and every validation decision;
 * the helper and the error are separate paragraphs with ids derived from the field id.
 */
export function TextField({ id, label, required = false, value, onChange, onBlur, helper, error,
  disabled = false, readOnly, inputMode, describedBy, affix }: TextFieldProps) {
  requireIdentity(id);
  requireLabel(label, 'field label');
  const helperId = helper ? id + '-helper' : undefined;
  const errorId = error ? id + '-error' : undefined;
  const affixId = affix ? id + '-unit' : undefined;
  const unit = affix && <span id={affixId} className="ot-text-field-unit">{affix.text}</span>;
  const input = <NativeInput className="ot-text-field-control" id={id} value={value} onChange={onChange} onBlur={onBlur}
    required={required} disabled={disabled} invalid={!!error} readOnly={readOnly} inputMode={inputMode}
    size={affix?.position === 'suffix' ? Math.max(1, value.length) : undefined}
    describedBy={[affixId, helperId, errorId, describedBy].filter(Boolean).join(' ')} />;

  return <div className="ot-text-field">
    <label className="ot-text-field-label" htmlFor={id}>{label + (required ? ' · Required' : ' · Optional')}</label>
    {affix ? <div className="ot-text-field-value" data-position={affix.position}>
      {affix.position === 'prefix' && unit}
      {input}
      {affix.position === 'suffix' && unit}
    </div> : input}
    {helper && <p className="ot-text-field-helper" id={helperId}>{helper}</p>}
    {error && <p className="ot-text-field-error" id={errorId}>{error}</p>}
  </div>;
}

/**
 * Standalone single-line input. An invalid value keeps the ordinary border, as designed: the state
 * is carried by `aria-invalid` and the caller's attached explanation.
 */
export function Input({ label, ...control }: InputProps) {
  requireIdentity(control.id);
  requireLabel(label, 'accessible name');
  return <NativeInput className="ot-input" name={label} {...control} />;
}
