'use client';

import { useId } from 'react';
import type { CheckboxProps, CheckboxState, SwitchProps, SwitchState } from './selection-controls.contracts';
import { IconCheckCircle } from './selection-controls-glyphs/IconCheckCircle';
import { requireLabel } from './require-label';
import switchOffArtwork from './selection-controls-assets/switch-off.svg';
import switchOnArtwork from './selection-controls-assets/switch-on.svg';
import '../../../styles/product-tokens.css';
import './selection-controls.css';

export type { CheckboxProps, CheckboxState, SwitchProps, SwitchState } from './selection-controls.contracts';
export const checkboxStates: readonly CheckboxState[] = ['enabled', 'disabled', 'error'];
export const switchStates: readonly SwitchState[] = ['enabled', 'disabled', 'pending'];

function admit<State extends string>(props: CheckboxProps | SwitchProps, state: State, states: readonly State[]): void {
  requireLabel(props.label);
  requireLabel(props.helper, 'helper');
  if (typeof props.checked !== 'boolean') throw new Error('A caller-supplied boolean checked value is required.');
  if (typeof props.onCheckedChange !== 'function') throw new Error('A change request handler is required.');
  if (!states.includes(state)) throw new Error('A declared selection control state is required.');
}

/** One activation is one request for the opposite of the supplied value; blocked treatments request nothing. */
export function requestChange(checked: boolean, state: CheckboxState | SwitchState, onCheckedChange: (next: boolean) => void): void {
  if (state === 'disabled' || state === 'pending') return;
  onCheckedChange(!checked);
}

/** Native checkbox whose hit target and focus ring are the whole labelled card. */
export function Checkbox(props: CheckboxProps) {
  const { checked, label, helper, onCheckedChange, state = 'enabled' } = props;
  admit(props, state, checkboxStates);
  const id = useId();
  return <div className={`ot-selection ot-selection--checkbox ot-selection--${state}`}>
    <input className="ot-selection-control" type="checkbox" id={`${id}control`} checked={checked}
      disabled={state === 'disabled'} aria-invalid={state === 'error' || undefined} aria-describedby={`${id}helper`}
      onChange={() => requestChange(checked, state, onCheckedChange)} />
    <span className="ot-selection-row">
      <span className={`ot-selection-art ot-checkbox-indicator${checked ? ' ot-checkbox-indicator--checked' : ''}`} aria-hidden="true">
        {checked && <IconCheckCircle />}
      </span>
      <label className="ot-selection-label" htmlFor={`${id}control`}>{label}</label>
    </span>
    <span className="ot-selection-helper" id={`${id}helper`}>{helper}</span>
  </div>;
}

/** Native button exposed as a switch. Pending stays focusable; only native disabled leaves the tab order. */
export function Switch(props: SwitchProps) {
  const { checked, label, helper, onCheckedChange, state = 'enabled' } = props;
  admit(props, state, switchStates);
  const id = useId();
  const pending = state === 'pending';
  return <div className={`ot-selection ot-selection--switch ot-selection--${state}`}>
    <button className="ot-selection-control" type="button" role="switch" aria-checked={checked}
      aria-labelledby={`${id}label`} aria-describedby={`${id}helper`} disabled={state === 'disabled'}
      aria-disabled={pending || undefined} aria-busy={pending || undefined}
      onClick={() => requestChange(checked, state, onCheckedChange)} />
    <span className="ot-selection-row">
      {/* Both native exports stay mounted so a value change never waits on an image request. */}
      <span className="ot-selection-art ot-switch-art" aria-hidden="true">
        <img src={switchOffArtwork} width="38" height="20" alt="" hidden={checked} />
        <img src={switchOnArtwork} width="38" height="20" alt="" hidden={!checked} />
      </span>
      <span className="ot-selection-label" id={`${id}label`}>{label}</span>
    </span>
    <span className="ot-selection-helper" id={`${id}helper`}>{helper}</span>
  </div>;
}
