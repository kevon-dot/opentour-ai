/**
 * Presentation inputs only. No prop represents server authority or an operation receipt.
 * Each component renders the state its caller names; none derives, advances, times or dismisses one,
 * and none takes a number, so an unknown count can never be shown as zero.
 */

/** Figma component set 12:109 `Data state`, in variant order (`State=Unknown outcome` is `unknown-outcome`). */
export type DataStateKind = 'loading' | 'empty' | 'partial' | 'error' | 'denied' | 'stale' | 'offline' | 'revoked' | 'unknown-outcome';

/** Figma component set 12:125 `Progress`, in variant order. */
export type ProgressState = 'queued' | 'running' | 'stopping' | 'complete' | 'failed';

/** Figma component set 12:138 `Toast`, in variant order. `accepted` (a request was taken) is not `confirmed`. */
export type ToastState = 'accepted' | 'confirmed' | 'failed' | 'unknown';

/**
 * `polite` exposes the card as one polite status that is read as a whole when its text changes.
 * `off` exposes plain text with no live region: for a card repeated in a list, or one whose text
 * changes on every poll, where a live region would flood a screen reader.
 */
export type PersistentAnnouncement = 'polite' | 'off';

/** `assertive` interrupts, and is accepted only for a failed toast the user must act on. */
export type ToastAnnouncement = 'polite' | 'assertive';

/** Persistent state of a data region. Never an alert. Both strings are visible and required; neither is markup. */
export interface DataStateProps {
  state: DataStateKind;
  /** States the condition in words. `state` selects colour only and never stands in for this text. */
  title: string;
  body: string;
  announce?: PersistentAnnouncement;
}

/** Persistent state of one operation, as told by the caller. It has no bar, percentage or indicator. */
export interface ProgressProps {
  state: ProgressState;
  /** States the condition in words. `state` selects colour only and never stands in for this text. */
  title: string;
  body: string;
  announce?: PersistentAnnouncement;
}

/**
 * A transient message, and never proof of completion. The caller mounts and unmounts it:
 * the component has no timer, no dismiss control and no position of its own.
 */
export interface ToastProps {
  state: ToastState;
  /** States the condition in words. `state` selects colour only and never stands in for this text. */
  title: string;
  body: string;
  announce?: ToastAnnouncement;
}
