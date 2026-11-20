import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { NullableField, type NullableChoices, type NullableDraft } from '../../../components/foundation/forms';

const choices: NullableChoices = {
  value: { label: 'Set a value', helper: 'An explicit date will be saved.' },
  null: { label: 'No end date', display: 'No end date', helper: 'No value will be saved.' },
  omitted: { label: 'Leave unchanged', display: 'Use existing value', helper: 'This field will not be changed.' },
};

function Fixture() {
  const [mode, setMode] = useState<NullableDraft['kind']>('value');
  const [raw, setRaw] = useState('2026-12-31');
  const [allowOmission, setAllowOmission] = useState(true);
  const [applyChanges, setApplyChanges] = useState(true);
  const [events, setEvents] = useState<NullableDraft[]>([]);
  return <main>
    <button onClick={() => setApplyChanges(previous => !previous)}>Toggle caller updates</button>
    <button onClick={() => setAllowOmission(previous => !previous)}>Toggle allowed omission</button>
    <NullableField id="end-date" label="End date" mode={mode} raw={raw}
      choices={{ ...choices, omitted: allowOmission ? choices.omitted : undefined }}
      onChange={next => {
        setEvents(previous => [...previous, next]);
        if (applyChanges) {
          setMode(next.kind);
          if (next.kind === 'value') setRaw(next.raw);
        }
      }} />
    <output aria-label="Callback history">{JSON.stringify(events)}</output>
  </main>;
}

createRoot(document.getElementById('root')!).render(<Fixture />);
