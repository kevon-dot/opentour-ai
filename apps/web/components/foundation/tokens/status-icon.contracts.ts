/** Caller-selected visual semantics, independent of domain state and operation outcome. */
export type StatusIconTone = 'success' | 'hold' | 'failed' | 'unknown' | 'info';
export interface StatusIconProps {
  tone: StatusIconTone;
  /** Exact nonempty visible status text. No label is inferred from the tone. */
  label: string;
}
