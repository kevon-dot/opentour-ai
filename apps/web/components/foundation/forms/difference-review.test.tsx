import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { DifferenceReview } from './difference-review';

const base = { fieldId: 'attempts', fieldName: 'Maximum attempts', draftRevision: 'revision 7',
  currentRevision: 'revision 8', currentValue: '5', onChoice: () => {} };
testCases(['003', '  03?\n', '', '<raw>'])('preserves caller draft %j', draftValue => {
  const html = renderToStaticMarkup(<DifferenceReview {...base} draftValue={draftValue} />);
  assert.ok((html)?.includes('<span>' + draftValue.replace('<raw>', '&lt;raw&gt;') + '</span>'));
  assert.ok((html)?.includes('Your draft · revision 7'));
  assert.ok((html)?.includes('Current settings · revision 8'));
});
it('renders both choices as non-submitting native buttons', () => {
  const html = renderToStaticMarkup(<DifferenceReview {...base} draftValue="3" />);
  assert.equal((html.match(/type="button"/g))?.length, 2);
  assert.ok(html.indexOf('Keep current value') < html.indexOf('Use my draft'));
});
it('requires a distinct field identity from the caller', () => {
  assert.throws(() => renderToStaticMarkup(<DifferenceReview {...base} fieldId="" draftValue="3" />), /field identity/);
});
