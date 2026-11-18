import { useEffect, useState } from 'react';
import { SingleChoiceSelect } from './single-choice-select';
import type { SingleChoiceOption } from './single-choice-select';
import './single-choice-select.fixtures.css';

/** Generated presentation copy only: no lookup, no authorization and no stored selection. */

/** Titles and supporting text of the native option rows (Figma 31:42, 31:45 and 31:48). */
export const sourceOptions: readonly SingleChoiceOption[] = [
  { value: 'refund-ledger', label: 'Refund ledger', detail: 'completion authority' },
  { value: 'policy-registry', label: 'Policy registry', detail: 'policy authority' },
  { value: 'operator-notes', label: 'Operator notes', disabled: true, disabledReason: 'not independent truth' },
];

/** Candidate rows named as in the product-context frame (Figma 166:9963). */
export const candidateOptions: readonly SingleChoiceOption[] = [
  { value: 'candidate-r2', label: 'Candidate r2', detail: 'eligible' },
  { value: 'candidate-r3', label: 'Candidate r3', detail: 'eligible' },
  { value: 'incumbent-r0', label: 'Incumbent r0', detail: 'incumbent' },
];

/** A disabled choice between enabled ones, so skipping it is observable in both directions. */
export const reviewOptions: readonly SingleChoiceOption[] = [
  { value: 'refund-ledger', label: 'Refund ledger', detail: 'completion authority' },
  { value: 'operator-notes', label: 'Operator notes', disabled: true, disabledReason: 'not independent truth' },
  { value: 'policy-registry', label: 'Policy registry', detail: 'policy authority' },
  { value: 'audit-export', label: 'Audit export', detail: 'generated evidence' },
];

/** A replacement list that no longer offers the first list's opening value. */
export const replacedOptions: readonly SingleChoiceOption[] = [
  { value: 'policy-registry', label: 'Policy registry', detail: 'policy authority' },
  { value: 'audit-export', label: 'Audit export', detail: 'generated evidence' },
];

/**
 * The first control keeps the generic placeholder, which the design types with its caret (Figma 12:41).
 * The error and disabled placeholders are the product-context values, typed without one (Figma 166:9963),
 * and the source control shows its chosen value as the label alone (Figma 31:38).
 */
export const selectCopy = {
  label: 'Select',
  helper: 'Supporting information',
  sourceHeading: 'Authoritative source',
  sourceLabel: 'Source',
  sourceHelper: 'Arrow keys move focus; Enter selects; Escape closes.',
  errorLabel: 'Candidate artifact · Required',
  errorPlaceholder: 'Choose an eligible artifact',
  error: 'Choose an eligible artifact before continuing.',
  disabledLabel: 'Candidate artifact',
  disabledPlaceholder: 'No eligible artifacts',
  disabledReason: 'Complete artifact qualification, then refresh this list.',
  heldLabel: 'Held source',
  heldHelper: 'The caller keeps this value until it accepts a request.',
  reviewLabel: 'Review source',
} as const;

/** Gallery-only document event that swaps the review list for its replacement. */
export const replaceOptionsEvent = 'single-choice-fixture:replace-options';

/**
 * One local gallery: the designed control states, the native option rows, and two instances that
 * show the control is driven only by its caller. Every request is listed as text for the tests.
 */
export function SingleChoiceSelectFixture() {
  const [requests, setRequests] = useState<string[]>([]);
  const [artifact, setArtifact] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>('refund-ledger');
  const [review, setReview] = useState<string | null>('refund-ledger');
  const [replaced, setReplaced] = useState(false);
  // Lets a test replace the list the way a caller's re-render would, without a press that would dismiss it.
  useEffect(() => {
    const replace = () => setReplaced(true);
    document.addEventListener(replaceOptionsEvent, replace);
    return () => document.removeEventListener(replaceOptionsEvent, replace);
  }, []);
  const record = (field: string, accept?: (value: string) => void) => (value: string) => {
    setRequests(previous => [...previous, `${field}:${value}`]);
    accept?.(value);
  };
  return <main className="ot-single-choice-fixture">
    <h1>Single-choice select</h1>
    <section aria-label="Control states" className="fixture-row">
      <div className="fixture-cell">
        <SingleChoiceSelect label={selectCopy.label} helper={selectCopy.helper} options={candidateOptions}
          value={artifact} onChange={record('select', setArtifact)} />
      </div>
      <div className="fixture-cell">
        <SingleChoiceSelect label={selectCopy.errorLabel} placeholder={selectCopy.errorPlaceholder} error={selectCopy.error}
          options={candidateOptions} value={null} onChange={record('required')} />
      </div>
      <div className="fixture-cell">
        <SingleChoiceSelect label={selectCopy.disabledLabel} placeholder={selectCopy.disabledPlaceholder} disabled
          disabledReason={selectCopy.disabledReason} options={[]} value={null} onChange={record('unavailable')} />
      </div>
    </section>
    <section aria-labelledby="authoritative-source" className="fixture-reference" data-testid="reference">
      <h2 id="authoritative-source">{selectCopy.sourceHeading}</h2>
      <SingleChoiceSelect label={selectCopy.sourceLabel} helper={selectCopy.sourceHelper} options={sourceOptions}
        value={source} onChange={record('source', setSource)} />
    </section>
    <section aria-label="Controlled behaviour" className="fixture-row">
      <div className="fixture-cell">
        {/* The caller records the request and keeps its value: the control must keep showing it. */}
        <SingleChoiceSelect label={selectCopy.heldLabel} helper={selectCopy.heldHelper} options={sourceOptions}
          value="refund-ledger" onChange={record('held')} />
      </div>
      <div className="fixture-cell">
        <SingleChoiceSelect label={selectCopy.reviewLabel} options={replaced ? replacedOptions : reviewOptions}
          value={review} onChange={record('review', setReview)} />
      </div>
      <button type="button" className="ot-button ot-button--secondary" onClick={() => setReplaced(true)}>Replace options</button>
      <button type="button" className="ot-button ot-button--quiet">Next control</button>
    </section>
    <output aria-label="Requested choices">{requests.join(', ')}</output>
  </main>;
}
