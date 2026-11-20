import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { FormErrorSummary } from '../../../components/foundation/forms/form-error-summary';
import { describeProjectDraft, ProjectDescription } from '../../../features/workspace-lifecycle/project-description';

const sample = 'Refund outcome learning.';
const multiline = 'Receipt-status workflow\nChecks settled payments against the source of truth.';
const reason = 'Unavailable: your current role cannot edit this field.';

function Fixture() {
  const [draft, setDraft] = useState(sample);
  const [attempt, setAttempt] = useState(0);
  const [serverError, setServerError] = useState('');
  const [acceptChanges, setAcceptChanges] = useState(true);
  const problem = describeProjectDraft(draft).error ?? serverError;
  const errors = problem ? [{ fieldId: 'project-description', fieldLabel: 'Description', message: problem }] : [];
  return <main>
    <form onSubmit={event => { event.preventDefault(); setAttempt(value => value + 1); }}>
      <FormErrorSummary errors={attempt ? errors : []} submissionId={attempt || null} />
      <ProjectDescription id="project-description" value={draft} error={serverError}
        onChange={value => { if (acceptChanges) { setDraft(value); setServerError(''); } }} />
      <button type="submit">Save changes</button>
    </form>
    <div className="typed">
      <section aria-label="Typed default"><ProjectDescription id="typed" helper="" value={multiline} onChange={() => {}} /></section>
      <section aria-label="Unavailable"><ProjectDescription id="unavailable" helper="" disabledReason={reason}
        value={multiline} onChange={() => {}} /></section>
      <section aria-label="Read only"><ProjectDescription id="readonly" helper="" readOnly
        value={multiline} onChange={() => {}} /></section>
    </div>
    <button type="button" onClick={() => setServerError('Review this description before saving.')}>Supply error</button>
    <button type="button" onClick={() => setAcceptChanges(value => !value)}>Toggle caller updates</button>
    <output aria-label="Current draft">{JSON.stringify(draft)}</output>
  </main>;
}

createRoot(document.getElementById('root')!).render(<Fixture />);
