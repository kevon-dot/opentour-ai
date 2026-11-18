import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Input, TextField } from './text-field';

const base = { id: 'attempts', label: 'Maximum attempts', value: '3', onChange: () => {} };
const helper = 'Used for each generated run.';
const problem = 'Enter a whole number.';

/** Every attribute of the one native input, so an unexpected attribute fails as surely as a missing one. */
function control(html: string): Record<string, string> {
  const tags = html.match(/<input [^>]*>/g) ?? [];
  expect(tags).toHaveLength(1);
  if (!tags[0]) throw new Error('Missing native input');
  return Object.fromEntries([...tags[0].matchAll(/ ([a-z-]+)="([^"]*)"/g)].map(match => [match[1], match[2]]));
}

describe('TextField', () => {
  it('puts the id on one native text input and names it with the visible required label', () => {
    const html = renderToStaticMarkup(<TextField {...base} required />);
    expect(html).toContain('<label class="ot-text-field-label" for="attempts">Maximum attempts · Required</label>');
    expect(control(html)).toEqual({ class: 'ot-text-field-control', id: 'attempts', type: 'text', required: '', value: '3' });
    expect(html).not.toContain('<p');
  });

  it.each([{}, { required: false }])('marks a field that is not required as optional %j', requirement => {
    const html = renderToStaticMarkup(<TextField {...base} {...requirement} />);
    expect(html).toContain('for="attempts">Maximum attempts · Optional</label>');
    expect(control(html)).toEqual({ class: 'ot-text-field-control', id: 'attempts', type: 'text', value: '3' });
  });

  it.each([['2.5', '2.5'], ['', ''], ['  03?  ', '  03?  '], ['0003', '0003'], ['-1e3', '-1e3'], ['<raw>', '&lt;raw&gt;']])(
    'renders the caller value %j without parsing, coercing or trimming it', (value, rendered) => {
      expect(control(renderToStaticMarkup(<TextField {...base} value={value} />)).value).toBe(rendered);
      expect(control(renderToStaticMarkup(<TextField {...base} value={value} error={problem} />)).value).toBe(rendered);
      expect(control(renderToStaticMarkup(<TextField {...base} value={value} disabled />)).value).toBe(rendered);
    });

  const references: Array<{ text: { helper?: string; error?: string }; describedBy?: string; invalid?: string }> = [
    { text: {} },
    { text: { helper }, describedBy: 'attempts-helper' },
    { text: { error: problem }, describedBy: 'attempts-error', invalid: 'true' },
    { text: { helper, error: problem }, describedBy: 'attempts-helper attempts-error', invalid: 'true' },
  ];
  it.each(references)('references the helper and the error independently for $text', ({ text, describedBy, invalid }) => {
    const attributes = control(renderToStaticMarkup(<TextField {...base} {...text} />));
    expect(attributes['aria-describedby']).toBe(describedBy);
    expect(attributes['aria-invalid']).toBe(invalid);
  });

  it('keeps the helper and shows the error as a separate paragraph after it', () => {
    const html = renderToStaticMarkup(<TextField {...base} value="2.5" helper={helper} error={problem} />);
    const helperText = '<p class="ot-text-field-helper" id="attempts-helper">' + helper + '</p>';
    const errorText = '<p class="ot-text-field-error" id="attempts-error">' + problem + '</p>';
    expect(html).toContain(helperText + errorText);
    expect(html.match(/<p /g)).toHaveLength(2);
  });

  it('keeps the helper reference unchanged when an error is added and removed', () => {
    const withError = renderToStaticMarkup(<TextField {...base} helper={helper} error={problem} />);
    const withoutError = renderToStaticMarkup(<TextField {...base} helper={helper} />);
    expect(withError).toContain('id="attempts-helper"');
    expect(withoutError).toContain('id="attempts-helper"');
    expect(withoutError).not.toContain('attempts-error');
    expect(withoutError).not.toContain('aria-invalid');
  });

  it('does not treat an empty helper or error as text to reference', () => {
    const html = renderToStaticMarkup(<TextField {...base} helper="" error="" />);
    expect(control(html)).toEqual({ class: 'ot-text-field-control', id: 'attempts', type: 'text', value: '3' });
    expect(html).not.toContain('<p');
  });

  it('uses the native disabled and required states rather than ARIA substitutes', () => {
    const html = renderToStaticMarkup(<TextField {...base} required disabled helper={helper} />);
    expect(control(html)).toEqual({ class: 'ot-text-field-control', id: 'attempts', type: 'text', required: '', disabled: '',
      'aria-describedby': 'attempts-helper', value: '3' });
    expect(html).not.toContain('aria-disabled');
    expect(html).not.toContain('aria-required');
    expect(html).not.toContain('readonly');
  });

  it('escapes caller text instead of making markup', () => {
    const html = renderToStaticMarkup(<TextField {...base} label="<b>Attempts</b>" helper="<i>help</i>" error="<u>no</u>" />);
    expect(html).toContain('&lt;b&gt;Attempts&lt;/b&gt; · Optional');
    expect(html).toContain('&lt;i&gt;help&lt;/i&gt;');
    expect(html).toContain('&lt;u&gt;no&lt;/u&gt;');
    expect(html).not.toMatch(/<(b|i|u)>/);
  });

  it.each(['', '   '])('refuses the blank label %j', label => {
    expect(() => renderToStaticMarkup(<TextField {...base} label={label} />)).toThrow('field label');
  });

  it.each(['', ' ', 'maximum attempts', 'attempts\n'])('refuses the identity %j, which cannot be referenced', id => {
    expect(() => renderToStaticMarkup(<TextField {...base} id={id} />)).toThrow('identity');
  });
});

describe('Input', () => {
  const standalone = { id: 'reference', label: 'Generated reference', value: 'Generated value', onChange: () => {} };

  it('is one native text input with an accessible name and no label element or wrapper', () => {
    const html = renderToStaticMarkup(<Input {...standalone} />);
    expect(html).toMatch(/^<input [^>]*\/>$/);
    expect(control(html)).toEqual({ class: 'ot-input', id: 'reference', type: 'text', 'aria-label': 'Generated reference',
      value: 'Generated value' });
  });

  it('conveys an invalid value through its state and the attached explanation', () => {
    const html = renderToStaticMarkup(<Input {...standalone} value="2.5" invalid describedBy="reference-problem" />);
    expect(control(html)).toEqual({ class: 'ot-input', id: 'reference', type: 'text', 'aria-label': 'Generated reference',
      'aria-invalid': 'true', 'aria-describedby': 'reference-problem', value: '2.5' });
  });

  it('omits the invalid state and an empty description when neither applies', () => {
    const attributes = control(renderToStaticMarkup(<Input {...standalone} invalid={false} describedBy="" />));
    expect(attributes).not.toHaveProperty('aria-invalid');
    expect(attributes).not.toHaveProperty('aria-describedby');
  });

  it('uses the native disabled and required states', () => {
    const attributes = control(renderToStaticMarkup(<Input {...standalone} disabled required />));
    expect(attributes.disabled).toBe('');
    expect(attributes.required).toBe('');
  });

  it.each(['', '2.5', '  spaced  '])('renders the caller value %j unchanged', value => {
    expect(control(renderToStaticMarkup(<Input {...standalone} value={value} />)).value).toBe(value);
  });

  it.each(['', '  '])('refuses the blank accessible name %j', label => {
    expect(() => renderToStaticMarkup(<Input {...standalone} label={label} />)).toThrow('accessible name');
  });

  it.each(['', 'generated reference'])('refuses the identity %j', id => {
    expect(() => renderToStaticMarkup(<Input {...standalone} id={id} />)).toThrow('identity');
  });
});
