import { useId, useState } from 'react';
import type { ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { FormErrorSummary } from '../../../components/foundation/forms/form-error-summary';
import type { FormErrorSummaryItem } from '../../../components/foundation/forms/form-error-summary';
import { Button } from '../../../components/foundation/tokens';
import './form-error-summary.css';

/*
 * Generated gallery for the OPE-321 form error summary. Every value and message here is generated.
 *
 * The limits are plain native inputs. They are temporary consumers that stand in for the shared field
 * until it is integrated, and carry only what the summary needs: a stable native id, a label and the
 * raw text as typed. The checks below are fixture logic. Nothing here is a provider or a shared field.
 *
 * The page's <main data-view> selects one view:
 *   mapped     the standalone mapped summary, outside the form that owns its two inputs
 *   unmapped   the standalone summary for a problem with no field and no public detail
 *   duplicate  the composed form after an earlier form that repeats both field ids (adverse)
 *   anything else: the composed form
 */
type Outcome = 'accept' | 'general' | 'undetailed';
interface Draft { attempts: string; spend: string }

const fieldIds = { attempts: 'maximum-attempts', spend: 'maximum-spend' } as const;

function fieldErrors(draft: Draft): FormErrorSummaryItem[] {
  const errors: FormErrorSummaryItem[] = [];
  if (!/^\d+$/.test(draft.attempts)) {
    errors.push({ fieldId: fieldIds.attempts, fieldLabel: 'Maximum attempts', message: 'enter a whole number' });
  }
  if (!/^\d+(?:\.\d+)?$/.test(draft.spend)) {
    errors.push({ fieldId: fieldIds.spend, fieldLabel: 'Maximum spend', message: 'enter a nonnegative amount' });
  }
  return errors;
}

/** A generated save result. `undetailed` is a problem whose detail is withheld, so it carries no text. */
function generalErrors(outcome: Outcome): FormErrorSummaryItem[] {
  if (outcome === 'general') return [{ message: 'These limits could not be saved together.' }];
  return outcome === 'undetailed' ? [{}] : [];
}

/**
 * The label wraps its control and the control is named by the label text's own generated id, so the
 * name is exactly the label and never depends on a field id that the adverse view repeats.
 */
function NativeField({ label, children }: { label: string; children: (labelId: string) => ReactNode }) {
  const labelId = useId();
  return <label className="form-errors-field"><span id={labelId}>{label}</span>{children(labelId)}</label>;
}

function Gallery({ title, children }: { title: string; children: ReactNode }) {
  return <div className="ot-foundation form-errors-gallery"><h1>{title}</h1>{children}</div>;
}

function MappedSpecimen() {
  return <Gallery title="Form error summary · mapped">
    <div className="form-errors-specimen">
      <FormErrorSummary errors={fieldErrors({ attempts: '2.5', spend: '-4' })} submissionId={null} formId="specimen-limits" />
    </div>
    <form id="specimen-limits" className="form-errors-form" aria-label="Temporary native inputs" noValidate
      onSubmit={event => event.preventDefault()}>
      <p className="form-errors-note">Temporary native inputs · pending the shared field</p>
      <NativeField label="Maximum attempts">{labelId =>
        <input id={fieldIds.attempts} aria-labelledby={labelId} type="text" inputMode="numeric" autoComplete="off"
          defaultValue="2.5" aria-invalid="true" />}
      </NativeField>
      <NativeField label="Maximum spend">{labelId =>
        <input id={fieldIds.spend} aria-labelledby={labelId} type="text" inputMode="decimal" autoComplete="off"
          defaultValue="-4" aria-invalid="true" />}
      </NativeField>
    </form>
  </Gallery>;
}

function UnmappedSpecimen() {
  return <Gallery title="Form error summary · unmapped">
    <div className="form-errors-specimen">
      <FormErrorSummary errors={generalErrors('undetailed')} submissionId={null} />
    </div>
  </Gallery>;
}

function ComposedForm() {
  const [draft, setDraft] = useState<Draft>({ attempts: '', spend: '' });
  const [outcome, setOutcome] = useState<Outcome>('accept');
  const [fields, setFields] = useState<FormErrorSummaryItem[]>([]);
  const [general, setGeneral] = useState<FormErrorSummaryItem[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [failedSubmission, setFailedSubmission] = useState<number | null>(null);
  const [activity, setActivity] = useState('Not submitted');
  const [renders, setRenders] = useState(0);
  const invalid = (id: string) => fields.some(error => error.fieldId === id) || undefined;
  // Before the first submission a blur reports nothing. After it, a blur re-checks the fields only.
  const revalidate = () => { if (submitted) setFields(fieldErrors(draft)); };

  return <form className="form-errors-form" aria-label="Generated run limits" noValidate onSubmit={event => {
    event.preventDefault();
    const nextFields = fieldErrors(draft);
    const nextGeneral = generalErrors(outcome);
    setSubmitted(true);
    setFields(nextFields);
    setGeneral(nextGeneral);
    if (nextFields.length + nextGeneral.length > 0) {
      // The identifier changes with the errors of the submission that failed, and only then.
      setFailedSubmission(current => (current ?? 0) + 1);
      setActivity('Not saved');
    } else setActivity('Saved');
  }}>
    <FormErrorSummary errors={[...fields, ...general]} submissionId={failedSubmission} />
    <p className="form-errors-note">Temporary native inputs · pending the shared field</p>
    <NativeField label="Maximum attempts">{labelId =>
      <input id={fieldIds.attempts} aria-labelledby={labelId} name="maximumAttempts" type="text" inputMode="numeric"
        autoComplete="off" value={draft.attempts} aria-invalid={invalid(fieldIds.attempts)}
        onChange={event => { const attempts = event.target.value; setDraft(current => ({ ...current, attempts })); }}
        onBlur={revalidate} />}
    </NativeField>
    <NativeField label="Maximum spend">{labelId =>
      <input id={fieldIds.spend} aria-labelledby={labelId} name="maximumSpend" type="text" inputMode="decimal"
        autoComplete="off" value={draft.spend} aria-invalid={invalid(fieldIds.spend)}
        onChange={event => { const spend = event.target.value; setDraft(current => ({ ...current, spend })); }}
        onBlur={revalidate} />}
    </NativeField>
    <NativeField label="Generated save outcome">{labelId =>
      <select id="generated-save-outcome" aria-labelledby={labelId} value={outcome}
        onChange={event => setOutcome(event.target.value as Outcome)}>
        <option value="accept">Accept</option>
        <option value="general">General problem</option>
        <option value="undetailed">No public detail</option>
      </select>}
    </NativeField>
    <div className="form-errors-actions">
      <Button type="submit" label="Save limits" />
      <Button variant="secondary" label="Render again" onPress={() => setRenders(count => count + 1)} />
    </div>
    <output aria-label="Generated form activity">{activity}</output>
    <output aria-label="Render count">{renders}</output>
  </form>;
}

/** Adverse: an earlier form that repeats both ids, which native fragment navigation would pick first. */
function DecoyForm() {
  return <form className="form-errors-form" aria-label="Earlier generated form" noValidate
    onSubmit={event => event.preventDefault()}>
    <p className="form-errors-note">Adverse fixture: this earlier form repeats both field ids on purpose.</p>
    <NativeField label="Maximum attempts">{labelId =>
      <input id={fieldIds.attempts} aria-labelledby={labelId} type="text" autoComplete="off" defaultValue="earlier 1" />}
    </NativeField>
    <NativeField label="Maximum spend">{labelId =>
      <input id={fieldIds.spend} aria-labelledby={labelId} type="text" autoComplete="off" defaultValue="earlier 2" />}
    </NativeField>
  </form>;
}

const mount = document.querySelector<HTMLElement>('main')!;
const view = mount.dataset.view;
createRoot(mount).render(
  view === 'mapped' ? <MappedSpecimen />
    : view === 'unmapped' ? <UnmappedSpecimen />
      : view === 'duplicate' ? <Gallery title="Form error summary · repeated ids"><DecoyForm /><ComposedForm /></Gallery>
        : <Gallery title="Form error summary · composed form"><ComposedForm /></Gallery>,
);
