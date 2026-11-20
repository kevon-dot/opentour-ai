'use client';

import { requireLabel } from '../../components/foundation/tokens/require-label';
import '../../components/foundation/forms/text-field.css';
import './project-description.css';

const limit = 500;
const settingsHint = 'Optional; No value is distinct from empty';

export function describeProjectDraft(value: string) {
  // Count Unicode code points, retaining whitespace, line breaks and the entire raw draft.
  const count = Array.from(value).length;
  return { count, error: count > limit ? 'Use 500 characters or fewer.' : undefined };
}

export interface ProjectDescriptionProps {
  /** Stable native textarea ID shared with the owning form and its validation summary. */
  id: string;
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  /** Additional context after the live count. An empty string shows the count alone. */
  helper?: string;
  /** A safe, caller-supplied field error, e.g. following a save attempt. */
  error?: string;
  /** Supplying the reason disables the field and explains the unavailable edit. */
  disabledReason?: string;
  readOnly?: boolean;
}

export function ProjectDescription({ id, value, onChange, onBlur, error, disabledReason, readOnly, helper = settingsHint }: ProjectDescriptionProps) {
  requireLabel(id, 'project description identity');
  if (/\s/.test(id)) throw new Error('A field identity must be a single ID.');
  if (disabledReason !== undefined) requireLabel(disabledReason, 'disabled reason');
  const draft = describeProjectDraft(value);
  const problem = draft.error ?? error;
  const helperId = id + '-helper';
  const errorId = problem ? id + '-error' : undefined;
  const reasonId = disabledReason ? id + '-unavailable' : undefined;
  return <div className="ot-project-description">
    <div className="ot-text-field ot-project-description-frame">
      <label className="ot-text-field-label" htmlFor={id}>Description · Optional</label>
      <textarea className="ot-text-field-control" id={id} rows={1} value={value}
        disabled={!!disabledReason} readOnly={readOnly} onBlur={onBlur}
        aria-invalid={problem ? true : undefined} aria-describedby={[helperId, errorId, reasonId].filter(Boolean).join(' ')}
        onChange={event => onChange(event.currentTarget.value)} />
      <p className="ot-text-field-helper" id={helperId}>{draft.count} / {limit} characters{helper && ' · ' + helper}</p>
      {problem && <p className="ot-text-field-error" id={errorId}>{problem}</p>}
    </div>
    {disabledReason && <p className="ot-text-field-helper" id={reasonId}>{disabledReason}</p>}
  </div>;
}
