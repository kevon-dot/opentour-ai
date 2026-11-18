import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
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
  it.each([null, 0, 7, 'retry-2'])('renders nothing without errors for submission %j', submissionId => {
    expect(render([], submissionId)).toBe('');
  });

  it('renders the mapped specimen as a named focusable region, the failure notice and one link per field', () => {
    const html = render([attempts, spend]);
    expect(html.startsWith('<section class="ot-form-error-summary" aria-label="Check 2 fields before continuing" tabindex="-1">')).toBe(true);
    expect(html).toContain(notice('Check 2 fields before continuing', mappedBody));
    expect(html).toContain('<ul class="ot-form-error-list" role="list">' +
      link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount') + '</ul></section>');
    expect(html.match(/<[a-z]+/g)).toEqual(['<section', '<div', '<p', '<p', '<ul', '<li', '<a', '<li', '<a']);
  });

  it('uses the singular heading for one field', () => {
    const html = render([spend]);
    expect(html).toContain('aria-label="Check 1 field before continuing"');
    expect(html).toContain(notice('Check 1 field before continuing', mappedBody));
    expect(html.match(/<a /g)).toHaveLength(1);
  });

  it('counts distinct fields, keeps each distinct problem and shows an identical problem once', () => {
    const second: FormErrorSummaryItem = { ...attempts, message: 'enter 10 or fewer' };
    const html = render([attempts, second, attempts, spend, spend]);
    expect(html).toContain(notice('Check 2 fields before continuing', mappedBody));
    expect(html).toContain(link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      link('maximum-attempts', 'Maximum attempts: enter 10 or fewer') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount') + '</ul>');
    expect(render([attempts, second])).toContain(notice('Check 1 field before continuing', mappedBody));
  });

  it('keeps unmapped text in a mixed state as plain text in the given order, with no link', () => {
    const html = render([attempts, general, spend, general, {}]);
    expect(html).toContain(notice('Check 2 fields before continuing', mappedBody));
    expect(html).toContain(link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      plain('These limits could not be saved together.') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount') + '</ul>');
    expect(html.match(/<a /g)).toHaveLength(2);
  });

  it('uses the general heading and body when no problem maps to a field', () => {
    const withText = render([general]);
    expect(withText).toContain('aria-label="We could not save these changes"');
    expect(withText).toContain(notice('We could not save these changes', unmappedBody) +
      '<ul class="ot-form-error-list" role="list">' + plain('These limits could not be saved together.') + '</ul>');
    expect(withText).not.toContain('<a');
    // A problem with no public text, such as a withheld provider message, leaves the notice alone.
    const undetailed = render([{}]);
    expect(undetailed.endsWith(notice('We could not save these changes', unmappedBody) + '</section>')).toBe(true);
    expect(undetailed).not.toMatch(/<ul|<li|<a/);
  });

  it('escapes caller text, keeps it verbatim and encodes the field identity in the fragment', () => {
    const html = render([
      { fieldId: 'limit:max/1', fieldLabel: '<b>Limit</b>', message: 'keep   0003? & 2.5 as typed' },
      { message: '<script>generated()</script>' },
    ]);
    expect(html).toContain('href="#limit%3Amax%2F1"');
    expect(html).toContain('&lt;b&gt;Limit&lt;/b&gt;: keep   0003? &amp; 2.5 as typed');
    expect(html).toContain(plain('&lt;script&gt;generated()&lt;/script&gt;'));
    expect(html).not.toMatch(/<script|<b>/);
  });

  it('never changes the errors it is given and renders the same markup each time', () => {
    const errors = Object.freeze([attempts, general, spend, {}].map(error => Object.freeze({ ...error })));
    const before = JSON.stringify(errors);
    const first = render(errors, 1);
    expect(render(errors, 1)).toBe(first);
    expect(JSON.stringify(errors)).toBe(before);
  });

  it('takes its content from the errors alone: the submission and form identifiers add no markup', () => {
    const html = render([attempts, spend]);
    for (const submissionId of [1, 2, 'retry-2']) expect(render([attempts, spend], submissionId, 'generated-limits')).toBe(html);
    expect(html).not.toContain('generated-limits');
  });

  it('adds no control, value or inline style of its own', () => {
    const html = render([attempts, general, spend]);
    expect(html).not.toMatch(/<(?:button|input|select|textarea|form)\b/);
    expect(html).not.toMatch(/\s(?:style|value|on[a-z]+)=/);
  });

  it.each<[string, FormErrorSummaryItem, string]>([
    ['a blank field identity', { ...attempts, fieldId: ' ' } as FormErrorSummaryItem, 'field identity'],
    ['an empty field identity', { ...attempts, fieldId: '' } as FormErrorSummaryItem, 'field identity'],
    ['a field identity with a space', { ...attempts, fieldId: 'maximum attempts' } as FormErrorSummaryItem, 'native id without spaces'],
    ['a mapped problem without a field name', { fieldId: 'maximum-attempts', message: 'enter a whole number' } as FormErrorSummaryItem, 'field name'],
    ['a mapped problem without a message', { fieldId: 'maximum-attempts', fieldLabel: 'Maximum attempts' } as FormErrorSummaryItem, 'error message'],
    ['a blank unmapped message', { message: ' \n' }, 'error message'],
  ])('refuses %s instead of rendering an empty or broken row', (_name, error, reason) => {
    expect(() => render([spend, error])).toThrow(reason);
  });
});

describe('detached rows for a form that places its fields between the notice and the rows', () => {
  const detached = (errors: readonly FormErrorSummaryItem[]) =>
    renderToStaticMarkup(<FormErrorSummary errors={errors} submissionId={null} rows="detached" />);
  const rowsOnly = (errors: readonly FormErrorSummaryItem[], formId?: string) =>
    renderToStaticMarkup(<FormErrorSummaryRows errors={errors} formId={formId} />);
  const mixed = [attempts, general, spend, attempts];

  it('keeps the named focusable region and the one notice, and leaves the rows out of a detached summary', () => {
    expect(detached(mixed)).toBe('<section class="ot-form-error-summary" aria-label="Check 2 fields before continuing" tabindex="-1">' +
      notice('Check 2 fields before continuing', mappedBody) + '</section>');
    expect(detached([])).toBe('');
  });

  it('draws the same rows once in a list named by the heading, with no second status, region or focus target', () => {
    const html = rowsOnly(mixed, 'generated-limits');
    const items = link('maximum-attempts', 'Maximum attempts: enter a whole number') +
      plain('These limits could not be saved together.') +
      link('maximum-spend', 'Maximum spend: enter a nonnegative amount');
    expect(html).toBe('<ul class="ot-form-error-list ot-form-error-list--detached" role="list" ' +
      'aria-label="Check 2 fields before continuing">' + items + '</ul>');
    // The attached arrangement lists exactly these rows: one source for both.
    expect(render(mixed)).toContain('<ul class="ot-form-error-list" role="list">' + items + '</ul>');
    expect(html).not.toMatch(/role="status"|<section|tabindex|generated-limits/);
    expect((detached(mixed) + html).match(/role="status"/g)).toHaveLength(1);
  });

  it('names the detached rows by the general heading when no problem maps to a field', () => {
    expect(rowsOnly([general])).toBe('<ul class="ot-form-error-list ot-form-error-list--detached" role="list" ' +
      'aria-label="We could not save these changes">' + plain('These limits could not be saved together.') + '</ul>');
  });

  it('renders no detached rows when there is no row to show', () => {
    expect(rowsOnly([])).toBe('');
    expect(rowsOnly([{}])).toBe('');
  });

  it('refuses a broken problem in the detached rows as the summary does', () => {
    expect(() => rowsOnly([{ ...attempts, fieldId: 'maximum attempts' } as FormErrorSummaryItem])).toThrow('native id without spaces');
    expect(() => rowsOnly([{ message: ' ' }])).toThrow('error message');
  });
});

describe('form error summary design binding', () => {
  const source = readFileSync(new URL('./form-error-summary.css', import.meta.url), 'utf8');
  const css = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const tokens = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');

  it('takes colour, spacing and type from recorded tokens with the focus outline as the only literal length', () => {
    expect(css.match(/#[0-9a-f]{3,8}\b/gi)).toBeNull();
    expect(css.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em)\b/g)).toEqual(['2px', '2px']);
    for (const [, name] of css.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)) expect(tokens).toContain(`  ${name}: `);
  });

  it('binds the 12px rhythm, the 14/22 regular rows and the underlined link colour to their tokens', () => {
    expect(css.match(/ gap: var\(--ot-space-12\); /g)).toHaveLength(2);
    expect(css).toContain('.ot-form-error-item { font-size: var(--ot-type-body-size); font-weight: var(--ot-type-body-weight); ' +
      'line-height: var(--ot-type-body-line); }');
    expect(css).toContain('.ot-form-error-link { color: var(--ot-text-link); text-decoration: underline; }');
    for (const value of ['--ot-space-12: 12px;', '--ot-type-body-size: 0.875rem;', '--ot-type-body-line: 1.375rem;',
      '--ot-type-body-weight: 400;', '--ot-text-link: #245bee;']) expect(tokens).toContain(value);
  });

  it('shows keyboard focus on the summary and on each link, and keeps it in forced colours', () => {
    expect(css).toContain('.ot-form-error-summary:focus-visible, .ot-form-error-link:focus-visible ' +
      '{ outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }');
    expect(css).toMatch(/@media \(forced-colors: active\) \{\s+\.ot-form-error-summary:focus-visible, \.ot-form-error-link:focus-visible \{ outline-color: Highlight; \}\s+\}/);
    expect(css).not.toMatch(/outline:\s*(?:none|0)\b/);
  });

  it('fills its container and declares nothing that could clip or stop text wrapping', () => {
    expect(css).toContain(' inline-size: 100%; min-inline-size: 0; ');
    expect(css).toContain(' overflow-wrap: anywhere; ');
    expect(css).not.toMatch(/overflow(?:-[xy]|-inline|-block)?:|text-overflow|white-space|line-clamp|max-inline-size|(?<!-)block-size|(?<![\w-])(?:height|width)/);
  });

  it('gives detached rows the fill, colour, face and wrapping the summary would have given them', () => {
    expect(css).toContain('.ot-form-error-list--detached { box-sizing: border-box; inline-size: 100%; min-inline-size: 0; ' +
      'color: var(--ot-text-primary); font-family: Geist, sans-serif; overflow-wrap: anywhere; }');
    // The shared list rule is still the only one that sets the row rhythm.
    expect(css).toContain('.ot-form-error-list { display: flex; flex-direction: column; gap: var(--ot-space-12); margin: 0; padding: 0; list-style: none; }');
  });
});
