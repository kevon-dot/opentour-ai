/**
 * Mutually exclusive caller-selected treatments. `checked` stays orthogonal, so every treatment
 * renders either supplied value. Focus and hover are browser states and are never props.
 */
export type CheckboxState = 'enabled' | 'disabled' | 'error';
export type SwitchState = 'enabled' | 'disabled' | 'pending';

interface SelectionControlProps {
  /** Caller-owned value. The control keeps no value of its own and never infers one. */
  checked: boolean;
  /** Exact nonempty visible text and the whole accessible name. */
  label: string;
  /** Exact nonempty visible helper, reason or error. It describes the control and never replaces its name. */
  helper: string;
  /** A request for the opposite value, not a committed preference. Only a parent rerender changes `checked`. */
  onCheckedChange: (next: boolean) => void;
}
export interface CheckboxProps extends SelectionControlProps {
  /** `disabled` blocks change natively; `error` marks the control invalid with `helper` as its error text. */
  state?: CheckboxState;
}
export interface SwitchProps extends SelectionControlProps {
  /** `disabled` blocks change natively; `pending` suppresses repeated requests but keeps focus and `checked`. */
  state?: SwitchState;
}
