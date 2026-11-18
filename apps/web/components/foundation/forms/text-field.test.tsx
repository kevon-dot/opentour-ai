import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { Input, TextField } from './text-field';

const base = { id: 'attempts', label: 'Maximum attempts', value: '3', onChange: () => {} };
const helper = 'Used for each generated run.';
const problem = 'Enter a whole number.';

/** Every attribute of the one native input, so an unexpected attribute fails as surely as a missing one. */
function control(html: string): Record<string, string> {
  const tags = html.match(/<input [^>]*>/g) ?? [];
  assert.equal((tags)?.length, 1);
  if (!tags[0]) throw new Error('Missing native input');
  return Object.fromEntries([...tags[0].matchAll(/ ([a-z-]+)="([^"]*)"/g)].map(match => [match[1], match[2]]));
}

describe('TextField', () => {
  it('puts the id on one native text input and names it with the visible required label', () => {
    const html = renderToStaticMarkup(<TextField {...base} required />);
    assert.ok((html)?.includes('<label class="ot-text-field-label" for="attempts">Maximum attempts · Required</label>'));
    assert.deepEqual(control(html), { class: 'ot-text-field-control', id: 'attempts', type: 'text', required: '', value: '3' });
    assert.ok((html)?.includes('<p') === false);
  });

  testCases([{}, { required: false }])('marks a field that is not required as optional %j', requirement => {
    const html = renderToStaticMarkup(<TextField {...base} {...requirement} />);
    assert.ok((html)?.includes('for="attempts">Maximum attempts · Optional</label>'));
    assert.deepEqual(control(html), { class: 'ot-text-field-control', id: 'attempts', type: 'text', value: '3' });
  });

  testCases([['2.5', '2.5'], ['', ''], ['  03?  ', '  03?  '], ['0003', '0003'], ['-1e3', '-1e3'], ['<raw>', '&lt;raw&gt;']])(
    'renders the caller value %j without parsing, coercing or trimming it', (value, rendered) => {
      assert.equal(control(renderToStaticMarkup(<TextField {...base} value={value} />)).value, rendered);
      assert.equal(control(renderToStaticMarkup(<TextField {...base} value={value} error={problem} />)).value, rendered);
      assert.equal(control(renderToStaticMarkup(<TextField {...base} value={value} disabled />)).value, rendered);
    });

  const references: Array<{ text: { helper?: string; error?: string }; describedBy?: string; invalid?: string }> = [
    { text: {} },
    { text: { helper }, describedBy: 'attempts-helper' },
    { text: { error: problem }, describedBy: 'attempts-error', invalid: 'true' },
    { text: { helper, error: problem }, describedBy: 'attempts-helper attempts-error', invalid: 'true' },
  ];
  testCases(references)('references the helper and the error independently for $text', ({ text, describedBy, invalid }) => {
    const attributes = control(renderToStaticMarkup(<TextField {...base} {...text} />));
    assert.equal(attributes['aria-describedby'], describedBy);
    assert.equal(attributes['aria-invalid'], invalid);
  });

  it('keeps the helper and shows the error as a separate paragraph after it', () => {
    const html = renderToStaticMarkup(<TextField {...base} value="2.5" helper={helper} error={problem} />);
    const helperText = '<p class="ot-text-field-helper" id="attempts-helper">' + helper + '</p>';
    const errorText = '<p class="ot-text-field-error" id="attempts-error">' + problem + '</p>';
    assert.ok((html)?.includes(helperText + errorText));
    assert.equal((html.match(/<p /g))?.length, 2);
  });

  it('keeps the helper reference unchanged when an error is added and removed', () => {
    const withError = renderToStaticMarkup(<TextField {...base} helper={helper} error={problem} />);
    const withoutError = renderToStaticMarkup(<TextField {...base} helper={helper} />);
    assert.ok((withError)?.includes('id="attempts-helper"'));
    assert.ok((withoutError)?.includes('id="attempts-helper"'));
    assert.ok((withoutError)?.includes('attempts-error') === false);
    assert.ok((withoutError)?.includes('aria-invalid') === false);
  });

  it('does not treat an empty helper or error as text to reference', () => {
    const html = renderToStaticMarkup(<TextField {...base} helper="" error="" />);
    assert.deepEqual(control(html), { class: 'ot-text-field-control', id: 'attempts', type: 'text', value: '3' });
    assert.ok((html)?.includes('<p') === false);
  });

  it('uses the native disabled and required states rather than ARIA substitutes', () => {
    const html = renderToStaticMarkup(<TextField {...base} required disabled helper={helper} />);
    assert.deepEqual(control(html), { class: 'ot-text-field-control', id: 'attempts', type: 'text', required: '', disabled: '',
      'aria-describedby': 'attempts-helper', value: '3' });
    assert.ok((html)?.includes('aria-disabled') === false);
    assert.ok((html)?.includes('aria-required') === false);
    assert.ok((html)?.includes('readonly') === false);
  });

  it('escapes caller text instead of making markup', () => {
    const html = renderToStaticMarkup(<TextField {...base} label="<b>Attempts</b>" helper="<i>help</i>" error="<u>no</u>" />);
    assert.ok((html)?.includes('&lt;b&gt;Attempts&lt;/b&gt; · Optional'));
    assert.ok((html)?.includes('&lt;i&gt;help&lt;/i&gt;'));
    assert.ok((html)?.includes('&lt;u&gt;no&lt;/u&gt;'));
    assert.doesNotMatch(html, /<(b|i|u)>/);
  });

  testCases(['', '   '])('refuses the blank label %j', label => {
    assert.throws(() => renderToStaticMarkup(<TextField {...base} label={label} />), /field label/);
  });

  testCases(['', ' ', 'maximum attempts', 'attempts\n'])('refuses the identity %j, which cannot be referenced', id => {
    assert.throws(() => renderToStaticMarkup(<TextField {...base} id={id} />), /identity/);
  });
});

describe('Input', () => {
  const standalone = { id: 'reference', label: 'Generated reference', value: 'Generated value', onChange: () => {} };

  it('is one native text input with an accessible name and no label element or wrapper', () => {
    const html = renderToStaticMarkup(<Input {...standalone} />);
    assert.match(html, /^<input [^>]*\/>$/);
    assert.deepEqual(control(html), { class: 'ot-input', id: 'reference', type: 'text', 'aria-label': 'Generated reference',
      value: 'Generated value' });
  });

  it('conveys an invalid value through its state and the attached explanation', () => {
    const html = renderToStaticMarkup(<Input {...standalone} value="2.5" invalid describedBy="reference-problem" />);
    assert.deepEqual(control(html), { class: 'ot-input', id: 'reference', type: 'text', 'aria-label': 'Generated reference',
      'aria-invalid': 'true', 'aria-describedby': 'reference-problem', value: '2.5' });
  });

  it('omits the invalid state and an empty description when neither applies', () => {
    const attributes = control(renderToStaticMarkup(<Input {...standalone} invalid={false} describedBy="" />));
    assert.ok(!('aria-invalid' in attributes));
    assert.ok(!('aria-describedby' in attributes));
  });

  it('uses the native disabled and required states', () => {
    const attributes = control(renderToStaticMarkup(<Input {...standalone} disabled required />));
    assert.equal(attributes.disabled, '');
    assert.equal(attributes.required, '');
  });

  testCases(['', '2.5', '  spaced  '])('renders the caller value %j unchanged', value => {
    assert.equal(control(renderToStaticMarkup(<Input {...standalone} value={value} />)).value, value);
  });

  testCases(['', '  '])('refuses the blank accessible name %j', label => {
    assert.throws(() => renderToStaticMarkup(<Input {...standalone} label={label} />), /accessible name/);
  });

  testCases(['', 'generated reference'])('refuses the identity %j', id => {
    assert.throws(() => renderToStaticMarkup(<Input {...standalone} id={id} />), /identity/);
  });
});
