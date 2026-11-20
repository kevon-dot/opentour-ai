import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { NumericField } from './index';

for (const kind of ['integer', 'decimal'] as const) {
  const props = { kind, id: 'draft', label: 'Setting', required: true, onChange: () => {} };
  describe(kind + ' field', () => {
    it('renders the supplied raw draft unchanged with a keyboard hint', () => {
      for (const value of ['', '0', '-', '.', '2.5', '0..25', '000.250000000000000000', ' 01 ', '1e-']) {
        const html = renderToStaticMarkup(<NumericField {...props} value={value} />);
        assert.ok(html.includes('type="text"'));
        assert.match(html, new RegExp('inputMode="' + (kind === 'integer' ? 'numeric' : 'decimal') + '"', 'i'));
        assert.ok(html.includes('value="' + value + '"'));
        assert.ok(html.includes('for="draft">Setting · Required'));
        assert.doesNotMatch(html, /aria-invalid| step=| pattern=/);
      }
    });
    it('associates separate supplied helper, error, constraints and disabled reason', () => {
      const html = renderToStaticMarkup(<NumericField {...props} value="2.5" helper="Help" error="Error"
        constraintText="Constraint" disabled disabledReason="Reason" />);
      assert.ok(html.includes('aria-describedby="draft-helper draft-error draft-constraints draft-reason"'));
      for (const [id, text] of [['helper', 'Help'], ['error', 'Error'], ['constraints', 'Constraint'], ['reason', 'Reason']]) {
        assert.ok(html.includes('id="draft-' + id + '">' + text + '</p>'));
      }
      assert.match(html, /aria-invalid="true"/);
      assert.match(html, / disabled=""/);
    });
    it('keeps read-only text focusable and omits inactive descriptions', () => {
      const html = renderToStaticMarkup(<NumericField {...props} value="0" readOnly disabledReason="Reason" />);
      assert.match(html, / readonly=""/i);
      assert.doesNotMatch(html, / disabled=|aria-describedby|Reason/);
    });
  });
}
