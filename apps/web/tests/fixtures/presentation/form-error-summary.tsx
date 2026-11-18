import { useId, useState } from 'react';
import type { ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { FormErrorSummary, FormErrorSummaryRows } from '../../../components/foundation/forms/form-error-summary';
import type { FormErrorSummaryItem } from '../../../components/foundation/forms/form-error-summary';
import { TextField } from '../../../components/foundation/forms/text-field';
import { Button } from '../../../components/foundation/tokens';
import './form-error-summary.css';

/*
 * Generated views for the OPE-321 form error summary. Every value and message here is generated.
 *
 * Each field that a summary link points to is the shared production TextField, imported as it is:
 * nothing here mocks it, wraps its markup or restates its styles. The checks are fixture logic that
 * runs in the page. There is no request, no provider and no persistence anywhere in this file.
 *
 * The page's <div data-view> selects one view:
 *   form       the workflow settings screen (Figma 159:121, 159:151, 159:181, 159:213): the genuine
 *              consumer, with the notice above its fields and the rows after them. This is the
 *              acceptance render. Below 1280px its header has a third row, the navigation button.
 *   mapped     the standalone mapped summary (Figma 153:386), outside the form that owns its two fields
 *   unmapped   the standalone summary for a problem with no field and no public detail
 *   stress     an interactive form for the general, mixed, repeated and re-rendered cases
 *   duplicate  the stress form after an earlier form that repeats both field ids (adverse)
 * Only the adverse decoy repeats an id. Every other view has ids that are unique in its document.
 */

/** One failed check: the summary row for it and the text shown under the field itself. */
interface Problem { fieldId: string; text: string; row: FormErrorSummaryItem }

function problem(fieldId: string, fieldLabel: string, message: string, text: string): Problem {
  return { fieldId, text, row: { fieldId, fieldLabel, message } };
}
const rowsOf = (problems: readonly Problem[]) => problems.map(item => item.row);
const textFor = (problems: readonly Problem[], fieldId: string) => problems.find(item => item.fieldId === fieldId)?.text;

/* ---- Workflow settings: the consumer in Figma 159:121, 159:151, 159:181 and 159:213 ---- */

interface SettingsDraft { name: string; attempts: string; spend: string }

const settingIds = { name: 'workflow-name', attempts: 'maximum-attempts', spend: 'maximum-spend' } as const;
/** The generated settings the screen opens with. A discarded draft returns to them. */
const currentSettings: SettingsDraft = { name: 'Receipt status', attempts: '3', spend: 'USD 10.00' };

/** The draft is only read. Each entry stays exactly as typed, whether or not it passes. */
function settingsProblems(draft: SettingsDraft): Problem[] {
  const problems: Problem[] = [];
  if (draft.name.trim() === '') {
    problems.push(problem(settingIds.name, 'Workflow name', 'enter a name', 'Enter a name.'));
  }
  if (!/^\d+$/.test(draft.attempts)) {
    problems.push(problem(settingIds.attempts, 'Maximum attempts', 'enter a whole number',
      'Enter a whole number. Your entry has not been changed.'));
  }
  if (!/^USD \d+(?:\.\d{2})?$/.test(draft.spend)) {
    problems.push(problem(settingIds.spend, 'Maximum spend', 'enter a nonnegative amount', 'Enter a nonnegative amount.'));
  }
  return problems;
}

function WorkflowSettings() {
  const headingId = useId();
  const [draft, setDraft] = useState<SettingsDraft>(currentSettings);
  const [problems, setProblems] = useState<Problem[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [failedSubmission, setFailedSubmission] = useState<number | null>(null);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const errors = rowsOf(problems);
  const edit = (entry: Partial<SettingsDraft>) => setDraft(current => ({ ...current, ...entry }));
  // Before the first submission a blur reports nothing. After it, a blur re-checks the fields.
  const revalidate = () => { if (submitted) setProblems(settingsProblems(draft)); };

  return <div className="ot-foundation workflow-settings-screen">
    <header className="workflow-settings-header">
      <p className="workflow-settings-product">{'OpenTour  /  Customer workspace'}</p>
      <p className="workflow-settings-workspace">Acme Labs / Production</p>
      {/* Third header row of the 768 and 320 frames; the stylesheet leaves it out from 1280px up. The button
          is the shared secondary Button and it works: it opens and closes a local list, and its name says
          which. That list is generated fixture content with no routing, request or provider behind it. */}
      <div className="workflow-settings-navigation">
        <Button variant="secondary" label={navigationOpen ? 'Close navigation' : 'Open navigation'}
          onPress={() => setNavigationOpen(open => !open)} />
        {navigationOpen && <nav className="workflow-settings-navigation-list" aria-label="Workspace navigation">
          <ul role="list"><li aria-current="page">Workflow settings</li></ul>
        </nav>}
      </div>
    </header>
    <main className="workflow-settings-main">
      <h1 id={headingId}>Workflow settings</h1>
      <p className="workflow-settings-revision">Receipt-status workflow · Current revision 8</p>
      <form className="workflow-settings-form" aria-labelledby={headingId} noValidate onSubmit={event => {
        event.preventDefault();
        const next = settingsProblems(draft);
        setSubmitted(true);
        setProblems(next);
        // The identifier changes with the errors of the submission that failed, and only then.
        if (next.length > 0) setFailedSubmission(current => (current ?? 0) + 1);
      }}>
        {/* One notice above the fields and its rows after them. Both read the same errors, and every
            sibling keeps its position, so a change of errors never remounts an input. */}
        <FormErrorSummary errors={errors} submissionId={failedSubmission} rows="detached" />
        <TextField id={settingIds.name} label="Workflow name" required value={draft.name}
          onChange={name => edit({ name })} onBlur={revalidate} helper="Name used in your workspace."
          error={textFor(problems, settingIds.name)} />
        <TextField id={settingIds.attempts} label="Maximum attempts" required value={draft.attempts}
          onChange={attempts => edit({ attempts })} onBlur={revalidate} error={textFor(problems, settingIds.attempts)} />
        <TextField id={settingIds.spend} label="Maximum spend" required value={draft.spend}
          onChange={spend => edit({ spend })} onBlur={revalidate} error={textFor(problems, settingIds.spend)} />
        <FormErrorSummaryRows errors={errors} />
        <div className="workflow-settings-actions">
          <Button type="submit" label="Save changes" />
          {/* The shared quiet variant: no border and no outline at rest, as the native frames draw it. */}
          <Button variant="quiet" label="Discard draft" onPress={() => {
            setDraft(currentSettings);
            setProblems([]);
            setSubmitted(false);
          }} />
        </div>
      </form>
    </main>
  </div>;
}

/* ---- Standalone specimens and the interactive stress form ---- */

type Outcome = 'accept' | 'general' | 'undetailed';
interface LimitDraft { attempts: string; spend: string }

const limitIds = { attempts: 'maximum-attempts', spend: 'maximum-spend' } as const;

function limitProblems(draft: LimitDraft): Problem[] {
  const problems: Problem[] = [];
  if (!/^\d+$/.test(draft.attempts)) {
    problems.push(problem(limitIds.attempts, 'Maximum attempts', 'enter a whole number', 'Enter a whole number.'));
  }
  if (!/^\d+(?:\.\d+)?$/.test(draft.spend)) {
    problems.push(problem(limitIds.spend, 'Maximum spend', 'enter a nonnegative amount', 'Enter a nonnegative amount.'));
  }
  return problems;
}

/** A generated save result. `undetailed` is a problem whose detail is withheld, so it carries no text. */
function generalErrors(outcome: Outcome): FormErrorSummaryItem[] {
  if (outcome === 'general') return [{ message: 'These limits could not be saved together.' }];
  return outcome === 'undetailed' ? [{}] : [];
}

/** The two limits as shared TextFields. `spendPresent` lets the stress form take one field away. */
function LimitFields({ draft, problems, onEdit, onBlur, spendPresent = true }: {
  draft: LimitDraft; problems: readonly Problem[]; onEdit: (entry: Partial<LimitDraft>) => void;
  onBlur?: () => void; spendPresent?: boolean;
}) {
  return <>
    <TextField id={limitIds.attempts} label="Maximum attempts" required value={draft.attempts}
      onChange={attempts => onEdit({ attempts })} onBlur={onBlur} error={textFor(problems, limitIds.attempts)} />
    {spendPresent && <TextField id={limitIds.spend} label="Maximum spend" required value={draft.spend}
      onChange={spend => onEdit({ spend })} onBlur={onBlur} error={textFor(problems, limitIds.spend)} />}
  </>;
}

/**
 * A native control of the fixture's own, used for the save outcome and for the adverse decoy. It is
 * named by its label text's generated id, so the name never depends on an id that the decoy repeats.
 */
function NativeField({ label, children }: { label: string; children: (labelId: string) => ReactNode }) {
  const labelId = useId();
  return <label className="form-errors-field"><span id={labelId}>{label}</span>{children(labelId)}</label>;
}

function Gallery({ title, children }: { title: string; children: ReactNode }) {
  return <main className="ot-foundation form-errors-gallery"><h1>{title}</h1>{children}</main>;
}

function MappedSpecimen() {
  const [draft, setDraft] = useState<LimitDraft>({ attempts: '2.5', spend: '-4' });
  // One fixed failed state: the problems are those of the entries the specimen opened with.
  const [problems] = useState(() => limitProblems(draft));
  return <Gallery title="Form error summary · mapped">
    <div className="form-errors-specimen">
      <FormErrorSummary errors={rowsOf(problems)} submissionId={null} formId="specimen-limits" />
    </div>
    <form id="specimen-limits" className="form-errors-form" aria-label="Generated run limits" noValidate
      onSubmit={event => event.preventDefault()}>
      <LimitFields draft={draft} problems={problems} onEdit={entry => setDraft(current => ({ ...current, ...entry }))} />
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

function StressForm() {
  const [draft, setDraft] = useState<LimitDraft>({ attempts: '', spend: '' });
  const [outcome, setOutcome] = useState<Outcome>('accept');
  const [problems, setProblems] = useState<Problem[]>([]);
  const [general, setGeneral] = useState<FormErrorSummaryItem[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [failedSubmission, setFailedSubmission] = useState<number | null>(null);
  const [activity, setActivity] = useState('Not submitted');
  const [renders, setRenders] = useState(0);
  const [spendPresent, setSpendPresent] = useState(true);
  // Before the first submission a blur reports nothing. After it, a blur re-checks the fields only.
  const revalidate = () => { if (submitted) setProblems(limitProblems(draft)); };

  return <form className="form-errors-form" aria-label="Generated run limits" noValidate onSubmit={event => {
    event.preventDefault();
    const nextProblems = limitProblems(draft);
    const nextGeneral = generalErrors(outcome);
    setSubmitted(true);
    setProblems(nextProblems);
    setGeneral(nextGeneral);
    if (nextProblems.length + nextGeneral.length > 0) {
      // The identifier changes with the errors of the submission that failed, and only then.
      setFailedSubmission(current => (current ?? 0) + 1);
      setActivity('Not saved');
    } else setActivity('Saved');
  }}>
    <FormErrorSummary errors={[...rowsOf(problems), ...general]} submissionId={failedSubmission} />
    <LimitFields draft={draft} problems={problems} onEdit={entry => setDraft(current => ({ ...current, ...entry }))}
      onBlur={revalidate} spendPresent={spendPresent} />
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
      {/* Adverse: takes the field out of this form and leaves its listed error as it is. */}
      <Button variant="secondary" label={spendPresent ? 'Remove maximum spend field' : 'Restore maximum spend field'}
        onPress={() => setSpendPresent(shown => !shown)} />
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
      <input id={limitIds.attempts} aria-labelledby={labelId} type="text" autoComplete="off" defaultValue="earlier 1" />}
    </NativeField>
    <NativeField label="Maximum spend">{labelId =>
      <input id={limitIds.spend} aria-labelledby={labelId} type="text" autoComplete="off" defaultValue="earlier 2" />}
    </NativeField>
  </form>;
}

const mount = document.querySelector<HTMLElement>('[data-view]')!;
const view = mount.dataset.view;
createRoot(mount).render(
  view === 'mapped' ? <MappedSpecimen />
    : view === 'unmapped' ? <UnmappedSpecimen />
      : view === 'stress' ? <Gallery title="Form error summary · stress form"><StressForm /></Gallery>
        : view === 'duplicate' ? <Gallery title="Form error summary · repeated ids"><DecoyForm /><StressForm /></Gallery>
          : <WorkflowSettings />,
);
