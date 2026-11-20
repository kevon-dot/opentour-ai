import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { TermsAcknowledgment, TermsDocumentLink, useTermsAcknowledgment, type PublishedTerms } from './terms-acknowledgment';

const current = { revision: 'opaque-r1', effective_at: '2026-11-01T00:00:00Z', document_url: 'https://terms.example/r1' };
function Fixture({ terms }: { terms: PublishedTerms | null }) {
  const choice = useTermsAcknowledgment(terms);
  return <><TermsDocumentLink acknowledgment={choice} /><TermsAcknowledgment acknowledgment={choice} onReload={() => {}} /></>;
}
it('shows an initially unchecked control, current opaque revision, effective date and announced external link', () => {
  const html = renderToStaticMarkup(<Fixture terms={current} />);
  assert.match(html, /href="https:\/\/terms.example\/r1" target="_blank" rel="noopener noreferrer"/);
  assert.match(html, /Opens in a new tab/);
  assert.match(html, /type="checkbox"/);
  assert.doesNotMatch(html, / checked=| disabled=|<form/);
  assert.match(html, /opaque-r1/);
  assert.match(html, /1 Nov 2026/);
});
it('disables acknowledgment and exposes only recovery for unavailable or invalid metadata', () => {
  for (const terms of [null, { ...current, document_url: 'http://terms.example/r1' },
    { ...current, document_url: '/unapproved' }, { ...current, document_url: 'https://user@terms.example/r1' },
    { ...current, revision: '' }, { ...current, effective_at: 'invalid' }]) {
    const html = renderToStaticMarkup(<Fixture terms={terms} />);
    assert.match(html, /disabled=""/);
    assert.match(html, /Terms unavailable/);
    assert.match(html, /Reload terms/);
    assert.doesNotMatch(html, /<a\b|<time\b|opaque-r1|type="submit"/);
  }
});
