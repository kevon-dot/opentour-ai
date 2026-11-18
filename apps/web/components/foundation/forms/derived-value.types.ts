/** Authorized display text only. The caller removes this component when its context is no longer valid. */
export interface DerivedValueProps {
  label: string;
  /** Exact display and clipboard contents, including an intentional empty string. Never parsed or trimmed. */
  value: string;
  helpText: string;
  /** Visible contextual name, for example "Copy workflow reference". */
  copyLabel: string;
  /** Safe caller text. Failure must also describe an unavailable clipboard without assuming permission denial. */
  copyFeedback: { success: string; failure: string };
}
