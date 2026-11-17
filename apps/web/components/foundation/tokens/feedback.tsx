import type { DataStateKind, DataStateProps, ProgressProps, ProgressState, ToastProps, ToastState } from './feedback.contracts';
import { requireLabel } from './require-label';
import '../../../styles/product-tokens.css';
import './feedback.css';

export type { DataStateKind, DataStateProps, PersistentAnnouncement, ProgressProps, ProgressState, ToastAnnouncement,
  ToastProps, ToastState } from './feedback.contracts';

/** Declared states in the Figma variant order of component set 12:109 (State=Unknown outcome is `unknown-outcome`). */
export const dataStates: readonly DataStateKind[] = ['loading', 'empty', 'partial', 'error', 'denied', 'stale', 'offline', 'revoked', 'unknown-outcome'];
/** Declared states in the Figma variant order of component set 12:125. */
export const progressStates: readonly ProgressState[] = ['queued', 'running', 'stopping', 'complete', 'failed'];
/** Declared states in the Figma variant order of component set 12:138. */
export const toastStates: readonly ToastState[] = ['accepted', 'confirmed', 'failed', 'unknown'];

/** `status` is a polite live region read as a whole; `off` leaves plain text with no live region at all. */
function persistentRole(announce: string, name: string): 'status' | undefined {
  if (announce !== 'polite' && announce !== 'off') throw new Error(`A ${name} is announced politely or not at all.`);
  return announce === 'polite' ? 'status' : undefined;
}

/**
 * Persistent in-flow state of a data region. `role="status"` is silent when the page is first read and
 * announces a later change of title or body politely and as a whole. It is never an alert, including for
 * `error`: the card stays on screen, so nothing is lost by waiting for the user's current speech to end.
 */
export function DataState({ state, title, body, announce = 'polite' }: DataStateProps) {
  if (!dataStates.includes(state)) throw new Error('A declared data state is required.');
  const role = persistentRole(announce, 'data state');
  requireLabel(title, 'data state title');
  requireLabel(body, 'data state body');
  return <div role={role} className={`ot-data-state ot-data-state--${state}`}>
    <p className="ot-data-state-title">{title}</p>
    <p className="ot-data-state-body">{body}</p>
  </div>;
}

/**
 * Persistent state of one operation. It is a status and not a `progressbar`: the design has no bar or
 * value, and a progressbar would hide the title and body from assistive technology.
 */
export function Progress({ state, title, body, announce = 'polite' }: ProgressProps) {
  if (!progressStates.includes(state)) throw new Error('A declared progress state is required.');
  const role = persistentRole(announce, 'progress state');
  requireLabel(title, 'progress title');
  requireLabel(body, 'progress body');
  return <div role={role} className={`ot-progress ot-progress--${state}`}>
    <p className="ot-progress-title">{title}</p>
    <p className="ot-progress-body">{body}</p>
  </div>;
}

/**
 * A transient message that the caller mounts and unmounts. Polite (`role="status"`) for every state by
 * default; only a failed toast the user must act on may interrupt (`role="alert"`), and the caller says so.
 */
export function Toast({ state, title, body, announce = 'polite' }: ToastProps) {
  if (!toastStates.includes(state)) throw new Error('A declared toast state is required.');
  if (announce !== 'polite' && announce !== 'assertive') throw new Error('A toast is announced politely or assertively.');
  if (announce === 'assertive' && state !== 'failed') throw new Error('Only a failed toast may be announced assertively.');
  requireLabel(title, 'toast title');
  requireLabel(body, 'toast body');
  return <div role={announce === 'assertive' ? 'alert' : 'status'} className={`ot-toast ot-toast--${state}`}>
    <p className="ot-toast-title">{title}</p>
    <p className="ot-toast-body">{body}</p>
  </div>;
}
