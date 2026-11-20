'use client';

import { useState } from 'react';
import { Checkbox } from '../../components/foundation/tokens/selection-controls';
import { Button, Notice } from '../../components/foundation/tokens';
import './terms-acknowledgment.css';

/** The parent supplies only the current, authorized TermsView from its approved source. */
export interface PublishedTerms {
  readonly revision: string;
  readonly document_url: string;
  readonly effective_at: string;
}

const dateFormat = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
});

function displayableTerms(terms: PublishedTerms | null) {
  if (!terms?.revision.trim() || !/^https:\/\//.test(terms.document_url)) return null;
  if (!URL.canParse(terms.document_url)) return null;
  const url = new URL(terms.document_url);
  if (url.username || url.password || !Number.isFinite(Date.parse(terms.effective_at))) return null;
  return terms;
}

/** Local to this setup instance. Returning to an earlier revision never restores its acknowledgment. */
export function useTermsAcknowledgment(current: PublishedTerms | null) {
  const terms = displayableTerms(current);
  const revision = terms?.revision ?? null;
  const [choice, setChoice] = useState({ revision, checked: false, changed: false });
  if (choice.revision !== revision) {
    setChoice({ revision, checked: false, changed: choice.revision !== null && revision !== null });
  }
  const checked = revision !== null && choice.revision === revision && choice.checked;
  return {
    terms,
    acceptedRevision: checked ? revision : null,
    changed: terms !== null && choice.changed && !checked,
    onCheckedChange(next: boolean) {
      if (revision !== null) setChoice(state => state.revision === revision
        ? { ...state, checked: next, changed: state.changed && !next } : state);
    },
  };
}

type Acknowledgment = ReturnType<typeof useTermsAcknowledgment>;

export function TermsDocumentLink({ acknowledgment }: { acknowledgment: Acknowledgment }) {
  const { terms, changed } = acknowledgment;
  if (!terms) return null;
  return <a className="ot-terms-link" href={terms.document_url} target="_blank" rel="noopener noreferrer">
    {changed ? 'Read current terms' : 'Read published terms'} · Opens in a new tab
  </a>;
}

export function TermsAcknowledgment({ acknowledgment, onReload }: {
  acknowledgment: Acknowledgment;
  onReload: () => void;
}) {
  const { terms, changed, acceptedRevision, onCheckedChange } = acknowledgment;
  return <div className="ot-terms-acknowledgment">
    {!terms && <Notice tone="hold" title="Terms unavailable"
      body="The current published terms could not be loaded. Organization creation is unavailable until they are shown." />}
    {changed && <Notice tone="hold" title="Published terms changed"
      body="Review the new published revision and agree again before creating an organization." />}
    <Checkbox label="I agree to the published terms" checked={acceptedRevision !== null}
      state={terms ? 'enabled' : 'disabled'} onCheckedChange={onCheckedChange}
      helper={terms ? 'Required · Published revision ' + terms.revision : 'Required · Current terms unavailable'} />
    {terms && <dl className="ot-terms-revision">
      <dt>Terms revision</dt><dd>{terms.revision}</dd>
      <dd>Effective <time dateTime={terms.effective_at}>{dateFormat.format(new Date(terms.effective_at))}</time> · Approved HTTPS document</dd>
    </dl>}
    {!terms && <Button label="Reload terms" onPress={onReload} />}
  </div>;
}
