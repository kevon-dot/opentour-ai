import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Input, TextField } from '../../../components/foundation/forms/text-field';

/** Generated presentation data only: no product record, customer value or provider response. */
const helper = 'Used for each generated run.';
const problem = 'Enter a whole number.';
/** Validation belongs to this caller. The components never parse, coerce or trim the raw text. */
const check = (value: string) => /^\d+$/.test(value) ? undefined : problem;

type FieldKey = 'default' | 'focus' | 'invalid' | 'disabled' | 'measure';
type Key = FieldKey | 'reference' | 'count';
type Example = { value: string; error?: string };

const initial: Record<Key, Example> = {
  default: { value: '3' },
  focus: { value: '3' },
  invalid: { value: '2.5', error: problem },
  disabled: { value: '3' },
  measure: { value: '3' },
  reference: { value: 'Generated value' },
  count: { value: '2.5', error: problem },
};

const galleryStyles = `
.field-gallery { box-sizing: border-box; display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--ot-space-24); padding: var(--ot-space-32); background: var(--ot-surface-canvas); color: var(--ot-text-primary); font-family: Geist, sans-serif; overflow-wrap: anywhere; }
.field-gallery section, .field-gallery-inputs, .field-gallery-controls { display: grid; grid-template-columns: minmax(0, 1fr); justify-items: start; gap: var(--ot-space-8); }
.field-gallery-form { --ot-text-field-inline-size: 688px; }
.field-gallery-inputs { box-sizing: border-box; inline-size: 100%; max-inline-size: 280px; }
.field-gallery h1, .field-gallery h2, .field-gallery-problem { margin: 0; }
.field-gallery h1 { font: var(--ot-type-section-weight) var(--ot-type-section-size)/var(--ot-type-section-line) Geist, sans-serif; }
.field-gallery h2 { color: var(--ot-text-secondary); font: var(--ot-type-small-strong-weight) var(--ot-type-small-strong-size)/var(--ot-type-small-strong-line) Geist, sans-serif; }
.field-gallery-problem, .field-gallery output { font: var(--ot-type-small-weight) var(--ot-type-small-size)/var(--ot-type-small-line) Geist, sans-serif; }
.field-gallery-problem { color: var(--ot-failure-text); }
.field-gallery output { color: var(--ot-text-secondary); }
.field-gallery button { box-sizing: border-box; max-inline-size: 100%; padding: var(--ot-space-8) var(--ot-space-16); border: 1px solid var(--ot-border-control); border-radius: var(--ot-radius-6); background: var(--ot-surface-base); color: inherit; font: var(--ot-type-label-weight) var(--ot-type-label-size)/var(--ot-type-label-line) Geist, sans-serif; white-space: normal; overflow-wrap: anywhere; text-align: start; cursor: pointer; }
.field-gallery button[aria-pressed='true'] { background: var(--ot-action-subtle); }
.field-gallery button:focus-visible { outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }
@media (max-width: 767px) { .field-gallery { padding: var(--ot-space-16); } }
@media (forced-colors: active) { .field-gallery button:focus-visible { outline-color: Highlight; } }
`;

function FieldGallery() {
  const [examples, setExamples] = useState(initial);
  const [unavailable, setUnavailable] = useState(true);
  const [blurs, setBlurs] = useState<Key[]>([]);
  const edit = (key: Key) => (value: string) =>
    setExamples(previous => ({ ...previous, [key]: { ...previous[key], value } }));
  const validate = (key: Key) => () => {
    setExamples(previous => ({ ...previous, [key]: { value: previous[key].value, error: check(previous[key].value) } }));
    setBlurs(previous => [...previous, key]);
  };
  const field = (key: FieldKey, title: string, state: { required?: boolean; disabled?: boolean }, className?: string) =>
    <section aria-labelledby={key + '-title'} className={className}>
      <h2 id={key + '-title'}>{title}</h2>
      <TextField id={'attempts-' + key} label="Maximum attempts" value={examples[key].value} helper={helper}
        error={examples[key].error} onChange={edit(key)} onBlur={validate(key)} {...state} />
    </section>;

  return <div className="field-gallery">
    <h1>Text field states</h1>
    {field('default', 'Default', { required: true })}
    {field('focus', 'Keyboard focus', { required: true })}
    {field('invalid', 'Error', { required: true })}
    {field('disabled', 'Disabled', { disabled: unavailable })}
    {field('measure', 'Form measure', { required: true }, 'field-gallery-form')}
    <section aria-labelledby="standalone-title">
      <h2 id="standalone-title">Standalone input</h2>
      <div className="field-gallery-inputs">
        <Input id="reference" label="Generated reference" value={examples.reference.value} onChange={edit('reference')} />
        <Input id="count" label="Generated count" value={examples.count.value} invalid={!!examples.count.error}
          describedBy={examples.count.error ? 'count-problem' : undefined} onChange={edit('count')} onBlur={validate('count')} />
        {examples.count.error && <p id="count-problem" className="field-gallery-problem">{examples.count.error}</p>}
        <Input id="reference-unavailable" label="Unavailable generated reference" value="Generated value" disabled
          onChange={() => {}} />
      </div>
    </section>
    <section aria-labelledby="controls-title">
      <h2 id="controls-title">Gallery controls</h2>
      <div className="field-gallery-controls">
        {['3', '2.5', ''].map(value => <button key={value} type="button"
          onClick={() => setExamples(previous => ({ ...previous, default: { value, error: check(value) } }))}>
          {'Supply ' + JSON.stringify(value) + ' to the default example'}
        </button>)}
        <button type="button" aria-pressed={unavailable} onClick={() => setUnavailable(previous => !previous)}>
          Disable the disabled example
        </button>
      </div>
      <output aria-label="Raw values">
        {JSON.stringify(Object.fromEntries(Object.entries(examples).map(([key, example]) => [key, example.value])))}
      </output>
      <output aria-label="Blur validations">{blurs.join(', ')}</output>
    </section>
  </div>;
}

const style = document.createElement('style');
style.textContent = galleryStyles;
document.head.append(style);
createRoot(document.querySelector('main')!).render(<FieldGallery />);
