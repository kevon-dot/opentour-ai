import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { DifferenceReview } from './difference-review';

const base = { fieldId: 'attempts', fieldName: 'Maximum attempts', draftRevision: 'revision 7',
  currentRevision: 'revision 8', currentValue: '5', onChoice: () => {} };
it.each(['003', '  03?\n', '', '<raw>'])('preserves caller draft %j', draftValue => {
  const html = renderToStaticMarkup(<DifferenceReview {...base} draftValue={draftValue} />);
  expect(html).toContain('<span>' + draftValue.replace('<raw>', '&lt;raw&gt;') + '</span>');
  expect(html).toContain('Your draft · revision 7');
  expect(html).toContain('Current settings · revision 8');
});
it('renders both choices as non-submitting native buttons', () => {
  const html = renderToStaticMarkup(<DifferenceReview {...base} draftValue="3" />);
  expect(html.match(/type="button"/g)).toHaveLength(2);
  expect(html.indexOf('Keep current value')).toBeLessThan(html.indexOf('Use my draft'));
});
it('requires a distinct field identity from the caller', () => {
  expect(() => renderToStaticMarkup(<DifferenceReview {...base} fieldId="" draftValue="3" />)).toThrow('field identity');
});
