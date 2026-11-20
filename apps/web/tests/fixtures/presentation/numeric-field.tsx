import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { NumericField } from '../../../components/foundation/forms/numeric-field';

const specimens = {
  integer: { label: 'Maximum attempts', value: '3', invalid: '2.5',
    helper: 'Enter a whole number from the allowed range.', error: 'Enter a whole number. 2.5 is not valid.' },
  decimal: { label: 'Threshold', value: '0.25', invalid: '0..25',
    helper: 'Enter a decimal using the precision shown in this policy.', error: 'Enter a valid decimal.' },
};
type Kind = keyof typeof specimens;
const states = ['default', 'focus', 'error', 'disabled', 'read-only'] as const;

function Specimen({ kind, state }: { kind: Kind; state: typeof states[number] }) {
  const { label, value, invalid, helper, error } = specimens[kind];
  const [draft, setDraft] = useState(state === 'error' ? invalid : value);
  const [message, setMessage] = useState(state === 'error' ? error : undefined);
  const id = kind + '-' + state;
  return <NumericField id={id} kind={kind} label={label} required value={draft}
    onChange={setDraft} onBlur={() => setMessage(draft === invalid ? error : undefined)}
    helper={message ? undefined : helper} error={message} disabled={state === 'disabled'}
    disabledReason="Unavailable: your current role cannot edit this field." readOnly={state === 'read-only'} />;
}

function Gallery({ kind }: { kind: Kind }) {
  return <section aria-label={kind}>{states.map(state => <Specimen key={state} kind={kind} state={state} />)}</section>;
}
createRoot(document.querySelector('main')!).render(<><Gallery kind="integer" /><Gallery kind="decimal" /></>);
