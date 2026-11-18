import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { FormErrorSummary, FormErrorSummaryRows } from './form-error-summary';
import type { FormErrorSummaryItem, FormErrorSummaryProps } from './form-error-summary';

// Generated copy only. Focus movement needs a document and is qualified in form-error-summary.visual.spec.ts.
const attempts: FormErrorSummaryItem = { fieldId: 'maximum-attempts', fieldLabel: 'Maximum attempts', message: 'enter a whole number' };
const spend: FormErrorSummaryItem = { fieldId: 'maximum-spend', fieldLabel: 'Maximum spend', message: 'enter a nonnegative amount' };
const general: FormErrorSummaryItem = { message: 'These limits could not be saved together.' };
const mappedBody = 'Your entries are still here. Correct the fields below.';
const unmappedBody = 'Review the form and try again. Contact support if the problem continues.';

function render(errors: readonly FormErrorSummaryItem[], submissionId: FormErrorSummaryProps['submissionId'] = null, formId?: string) {
  return renderToStaticMarkup(<FormErrorSummary errors={errors} submissionId={submissionId} formId={formId} />);
}
const notice = (title: string, body: string) => '<div role="status" class="ot-notice ot-notice--failure">' +
  `<p class="ot-notice-title">${title}</p><p class="ot-notice-body">${body}</p></div>`;
const link = (id: string, text: string) => `<li class="ot-form-error-item"><a class="ot-form-error-link" href="#${id}">${text}</a></li>`;
const plain = (text: string) => `<li class="ot-form-error-item">${text}</li>`;

describe('linked form error summary', () => {
  testCases([null, 0, 7, 'retry-2'])('renders nothing without errors for submission %j', submissionId => {
    assert.equal(render([], submissionId), '');
  });

  it('renders the mapped specimen as a named focusable region, the failure notice and one link per field', () => {
    const html = render([attempts, spend]);
    assert.equal(html.startsWith('<section class="ot-form-error-summary" aria-label="Check 2 fields before continuing" tabindex="-1">'), true);
    assert.ok((html)?.includes(notice('Check 2 fields before continuing', mappedBody)));
    assert.ok((html)?.includes('<ul class="ot-form-error-list" role="list">' +
      link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount') + '</ul></section>'));
    assert.deepEqual(html.match(/<[a-z]+/g), ['<section', '<div', '<p', '<p', '<ul', '<li', '<a', '<li', '<a']);
  });

  it('uses the singular heading for one field', () => {
    const html = render([spend]);
    assert.ok((html)?.includes('aria-label="Check 1 field before continuing"'));
    assert.ok((html)?.includes(notice('Check 1 field before continuing', mappedBody)));
    assert.equal((html.match(/<a /g))?.length, 1);
  });

  it('counts distinct fields, keeps each distinct problem and shows an identical problem once', () => {
    const second: FormErrorSummaryItem = { ...attempts, message: 'enter 10 or fewer' };
    const html = render([attempts, second, attempts, spend, spend]);
    assert.ok((html)?.includes(notice('Check 2 fields before continuing', mappedBody)));
    assert.ok((html)?.includes(link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      link('maximum-attempts', 'Maximum attempts: enter 10 or fewer') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount') + '</ul>'));
    assert.ok((render([attempts, second]))?.includes(notice('Check 1 field before continuing', mappedBody)));
  });

  it('keeps unmapped text in a mixed state as plain text in the given order, with no link', () => {
    const html = render([attempts, general, spend, general, {}]);
    assert.ok((html)?.includes(notice('Check 2 fields before continuing', mappedBody)));
    assert.ok((html)?.includes(link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      plain('These limits could not be saved together.') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount') + '</ul>'));
    assert.equal((html.match(/<a /g))?.length, 2);
  });

  it('uses the general heading and body when no problem maps to a field', () => {
    const withText = render([general]);
    assert.ok((withText)?.includes('aria-label="We could not save these changes"'));
    assert.ok((withText)?.includes(notice('We could not save these changes', unmappedBody) +
      '<ul class="ot-form-error-list" role="list">' + plain('These limits could not be saved together.') + '</ul>'));
    assert.ok((withText)?.includes('<a') === false);
    // A problem with no public text, such as a withheld provider message, leaves the notice alone.
    const undetailed = render([{}]);
    assert.equal(undetailed.endsWith(notice('We could not save these changes', unmappedBody) + '</section>'), true);
    assert.doesNotMatch(undetailed, /<ul|<li|<a/);
  });

  it('escapes caller text, keeps it verbatim and encodes the field identity in the fragment', () => {
    const html = render([
      { fieldId: 'limit:max/1', fieldLabel: '<b>Limit</b>', message: 'keep   0003? & 2.5 as typed' },
      { message: '<script>generated()</script>' },
    ]);
    assert.ok((html)?.includes('href="#limit%3Amax%2F1"'));
    assert.ok((html)?.includes('&lt;b&gt;Limit&lt;/b&gt;: keep   0003? &amp; 2.5 as typed'));
    assert.ok((html)?.includes(plain('&lt;script&gt;generated()&lt;/script&gt;')));
    assert.doesNotMatch(html, /<script|<b>/);
  });

  it('never changes the errors it is given and renders the same markup each time', () => {
    const errors = Object.freeze([attempts, general, spend, {}].map(error => Object.freeze({ ...error })));
    const before = JSON.stringify(errors);
    const first = render(errors, 1);
    assert.equal(render(errors, 1), first);
    assert.equal(JSON.stringify(errors), before);
  });

  it('takes its content from the errors alone: the submission and form identifiers add no markup', () => {
    const html = render([attempts, spend]);
    for (const submissionId of [1, 2, 'retry-2']) assert.equal(render([attempts, spend], submissionId, 'generated-limits'), html);
    assert.ok((html)?.includes('generated-limits') === false);
  });

  it('adds no control, value or inline style of its own', () => {
    const html = render([attempts, general, spend]);
    assert.doesNotMatch(html, /<(?:button|input|select|textarea|form)\b/);
    assert.doesNotMatch(html, /\s(?:style|value|on[a-z]+)=/);
  });

  testCases<[string, FormErrorSummaryItem, string]>([
    ['a blank field identity', { ...attempts, fieldId: ' ' } as FormErrorSummaryItem, 'field identity'],
    ['an empty field identity', { ...attempts, fieldId: '' } as FormErrorSummaryItem, 'field identity'],
    ['a field identity with a space', { ...attempts, fieldId: 'maximum attempts' } as FormErrorSummaryItem, 'native id without spaces'],
    ['a mapped problem without a field name', { fieldId: 'maximum-attempts', message: 'enter a whole number' } as FormErrorSummaryItem, 'field name'],
    ['a mapped problem without a message', { fieldId: 'maximum-attempts', fieldLabel: 'Maximum attempts' } as FormErrorSummaryItem, 'error message'],
    ['a blank unmapped message', { message: ' \n' }, 'error message'],
  ])('refuses %s instead of rendering an empty or broken row', (_name, error, reason) => {
    assert.throws(() => render([spend, error]), reason);
  });
});

describe('detached rows for a form that places its fields between the notice and the rows', () => {
  const detached = (errors: readonly FormErrorSummaryItem[]) =>
    renderToStaticMarkup(<FormErrorSummary errors={errors} submissionId={null} rows="detached" />);
  const rowsOnly = (errors: readonly FormErrorSummaryItem[], formId?: string) =>
    renderToStaticMarkup(<FormErrorSummaryRows errors={errors} formId={formId} />);
  const mixed = [attempts, general, spend, attempts];

  it('keeps the named focusable region and the one notice, and leaves the rows out of a detached summary', () => {
    assert.equal(detached(mixed), '<section class="ot-form-error-summary" aria-label="Check 2 fields before continuing" tabindex="-1">' +
      notice('Check 2 fields before continuing', mappedBody) + '</section>');
    assert.equal(detached([]), '');
  });

  it('draws the same rows once in a list named by the heading, with no second status, region or focus target', () => {
    const html = rowsOnly(mixed, 'generated-limits');
    const items = link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      plain('These limits could not be saved together.') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount');
    assert.equal(html, '<ul class="ot-form-error-list ot-form-error-list--detached" role="list" ' +
      'aria-label="Check 2 fields before continuing">' + items + '</ul>');
    // The attached arrangement lists exactly these rows: one source for both.
    assert.ok((render(mixed))?.includes('<ul class="ot-form-error-list" role="list">' + items + '</ul>'));
    assert.doesNotMatch(html, /role="status"|<section|tabindex|generated-limits/);
    assert.equal(((detached(mixed) + html).match(/role="status"/g))?.length, 1);
  });

  it('names the detached rows by the general heading when no problem maps to a field', () => {
    assert.equal(rowsOnly([general]), '<ul class="ot-form-error-list ot-form-error-list--detached" role="list" ' +
      'aria-label="We could not save these changes">' + plain('These limits could not be saved together.') + '</ul>');
  });

  it('renders no detached rows when there is no row to show', () => {
    assert.equal(rowsOnly([]), '');
    assert.equal(rowsOnly([{}]), '');
  });

  it('refuses a broken problem in the detached rows as the summary does', () => {
    assert.throws(() => rowsOnly([{ ...attempts, fieldId: 'maximum attempts' } as FormErrorSummaryItem]), /native id without spaces/);
    assert.throws(() => rowsOnly([{ message: ' ' }]), /error message/);
  });
});

describe('form error summary design binding', () => {
  const source = readFileSync(new URL('./form-error-summary.css', import.meta.url), 'utf8');
  const css = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const tokens = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');

  it('takes colour, spacing and type from recorded tokens with the focus outline as the only literal length', () => {
    assert.equal(css.match(/#[0-9a-f]{3,8}\b/gi), null);
    assert.deepEqual(css.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em)\b/g), ['2px', '2px']);
    for (const [, name] of css.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)) assert.ok((tokens)?.includes(`  ${name}: `));
  });

  it('binds the 12px rhythm, the 14/22 regular rows and the underlined link colour to their tokens', () => {
    assert.equal((css.match(/ gap: var\(--ot-space-12\); /g))?.length, 2);
    assert.ok((css)?.includes('.ot-form-error-item { font-size: var(--ot-type-body-size); font-weight: var(--ot-type-body-weight); ' +
      'line-height: var(--ot-type-body-line); }'));
    assert.ok((css)?.includes('.ot-form-error-link { color: var(--ot-text-link); text-decoration: underline; }'));
    for (const value of ['--ot-space-12: 12px;', '--ot-type-body-size: 0.875rem;', '--ot-type-body-line: 1.375rem;',
      '--ot-type-body-weight: 400;', '--ot-text-link: #245bee;']) assert.ok((tokens)?.includes(value));
  });

  it('shows keyboard focus on the summary and on each link, and keeps it in forced colours', () => {
    assert.ok((css)?.includes('.ot-form-error-summary:focus-visible, .ot-form-error-link:focus-visible ' +
      '{ outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }'));
    assert.match(css, /@media \(forced-colors: active\) \{\s+\.ot-form-error-summary:focus-visible, \.ot-form-error-link:focus-visible \{ outline-color: Highlight; \}\s+\}/);
    assert.doesNotMatch(css, /outline:\s*(?:none|0)\b/);
  });

  it('fills its container and declares nothing that could clip or stop text wrapping', () => {
    assert.ok((css)?.includes(' inline-size: 100%; min-inline-size: 0; '));
    assert.ok((css)?.includes(' overflow-wrap: anywhere; '));
    assert.doesNotMatch(css, /overflow(?:-[xy]|-inline|-block)?:|text-overflow|white-space|line-clamp|max-inline-size|(?<!-)block-size|(?<![\w-])(?:height|width)/);
  });

  it('gives detached rows the fill, colour, face and wrapping the summary would have given them, on the 16px rhythm of their form', () => {
    assert.ok((css)?.includes('.ot-form-error-list--detached { box-sizing: border-box; gap: var(--ot-space-16); inline-size: 100%; ' +
      'min-inline-size: 0; color: var(--ot-text-primary); font-family: Geist, sans-serif; overflow-wrap: anywhere; }'));
    assert.ok((tokens)?.includes('--ot-space-16: 16px;'));
    // Attached rows keep the 12px rhythm of the shared list rule. Only the detached list widens it, in a later
    // rule of the same weight, and nothing else sets a row gap.
    const shared = '.ot-form-error-list { display: flex; flex-direction: column; gap: var(--ot-space-12); margin: 0; padding: 0; list-style: none; }';
    assert.ok((css)?.includes(shared));
    assert.ok(css.indexOf('.ot-form-error-list--detached {') > css.indexOf(shared));
    assert.equal((css.match(/(?<![\w-])gap:/g))?.length, 3);
  });
});
