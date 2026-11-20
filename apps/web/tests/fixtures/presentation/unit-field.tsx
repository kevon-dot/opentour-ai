import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { UnitField } from '../../../components/foundation/forms/unit-field';

const specimens = {
  currency: { label: 'Maximum spend', value: '100.00', invalid: '−10.00', unit: 'USD', alternative: 'EUR',
    helper: 'US dollars. Exact cents are used when you submit.', alternativeHelper: 'Euros. Exact cents are used when you submit.',
    error: 'Enter a nonnegative amount with at most 2 decimal places.' },
  duration: { label: 'Timeout', value: '120', invalid: '1.5', unit: 'seconds', alternative: 'milliseconds',
    helper: 'Enter a whole number of seconds.', alternativeHelper: 'Enter a whole number of milliseconds.',
    error: 'Enter a whole number of seconds.' },
};
const states = ['default', 'focus', 'error', 'disabled', 'read-only'] as const;
type Kind = keyof typeof specimens;

function Specimen({ kind, state, alternate }: { kind: Kind; state: typeof states[number]; alternate: boolean }) {
  const spec = specimens[kind];
  const [raw, setRaw] = useState(state === 'error' ? spec.invalid : spec.value);
  const invalid = raw === spec.invalid;
  const helper = alternate ? spec.alternativeHelper : spec.helper;
  const error = alternate && kind === 'duration' ? spec.alternativeHelper : spec.error;
  return <UnitField id={kind + '-' + state} kind={kind} unit={alternate ? spec.alternative : spec.unit}
    value={raw} label={spec.label} required helper={invalid ? undefined : helper} error={invalid ? error : undefined}
    disabled={state === 'disabled'} disabledReason="Unavailable: your current role cannot edit this field."
    readOnly={state === 'read-only'} onChange={setRaw} />;
}

function Family({ kind }: { kind: Kind }) {
  const [alternate, setAlternate] = useState(false);
  return <><section aria-label={kind}>{states.map(state =>
    <Specimen key={state} kind={kind} state={state} alternate={alternate} />)}</section>
    <button type="button" onClick={() => setAlternate(!alternate)}>Change {kind} unit</button></>;
}
createRoot(document.querySelector('main')!).render(<><Family kind="currency" /><Family kind="duration" /></>);
