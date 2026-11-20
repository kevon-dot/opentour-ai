import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { NullableField, type NullableFieldProps } from './nullable-field';

const props: NullableFieldProps = {
  id: 'expiry', label: 'End date', raw: '', mode: 'value', onChange: () => assert.fail('A render cannot change the caller draft'),
  choices: {
    value: { label: 'Set a value', helper: 'An explicit date will be saved.' },
    null: { label: 'No end date', display: 'No end date', helper: 'No value will be saved.' },
    omitted: { label: 'Leave unchanged', display: 'Use existing value', helper: 'This field will not be changed.' },
  },
};
function render(overrides: Partial<NullableFieldProps> = {}) {
  return renderToStaticMarkup(<NullableField {...props} {...overrides} />);
}

describe('NullableField', () => {
  it('keeps empty text editable and visually selects only its value alternative', () => {
    const html = render();
    assert.match(html, /for="expiry">End date · Optional/);
    assert.match(html, /type="text"[^>]*value=""/);
    assert.equal(html.match(/aria-pressed="true"/g)?.length, 1);
    assert.equal(html.match(/aria-pressed="false"/g)?.length, 2);
    assert.match(html, /aria-pressed="true"[^>]*><span>Set a value/);
  });

  for (const mode of ['null', 'omitted'] as const) {
    it(`displays the supplied ${mode} alternative without exposing the raw draft`, () => {
      const choice = props.choices[mode]!;
      const html = render({ mode, raw: 'hidden draft', required: true });
      assert.match(html, /End date · Required/);
      assert.ok(html.includes(choice.display) && html.includes(choice.helper));
      assert.ok(html.includes('aria-pressed="true"><span>' + choice.label));
      assert.doesNotMatch(html, /<input|hidden draft/);
    });
  }

  it('removes disallowed alternatives, even when the caller still supplies their former mode', () => {
    const html = render({ choices: { null: props.choices.null }, raw: 'revoked raw' });
    assert.doesNotMatch(html, /Set a value|Leave unchanged|revoked raw|<input|aria-pressed="true"/);
    assert.match(html, /aria-pressed="false"><span>No end date/);
    assert.equal(render({ choices: {} }), '');
  });
});
