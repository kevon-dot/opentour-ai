import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { UnitField } from './unit-field';

const base = { id: 'edit', label: 'Limit', required: true, onChange: () => {} };
for (const kind of ['currency', 'duration'] as const) {
  const unit = kind === 'currency' ? 'USD' : 'seconds';
  it(kind + ' keeps each supplied raw string independent of its unit', () => {
    for (const value of ['', '0', '1.5', '001.230000000000000000001', '−10.00', ' 1.. ']) {
      const html = renderToStaticMarkup(<UnitField {...base} kind={kind} value={value} unit={unit} />);
      assert.ok(html.includes('value="' + value + '"'));
      assert.ok(html.includes('type="text"'));
      assert.ok(html.includes('id="edit-unit" class="ot-text-field-unit">' + unit + '</span>'));
      assert.equal(html.indexOf('id="edit-unit"') < html.indexOf('<input'), kind === 'currency');
      assert.ok(html.includes('aria-describedby="edit-unit"'));
      assert.ok(html.includes('for="edit">Limit · Required'));
    }
  });

  it(kind + ' associates caller messages and uses native disabled and read-only semantics', () => {
    const html = renderToStaticMarkup(<UnitField {...base} kind={kind} unit={unit} value="1.5"
      helper="Help" error="Error" disabled disabledReason="Locked" />);
    assert.match(html, /aria-invalid="true"/);
    assert.match(html, /aria-describedby="edit-unit edit-helper edit-error edit-reason"/);
    assert.match(html, / disabled=""/);
    assert.match(html, /id="edit-reason">Locked/);
    const locked = renderToStaticMarkup(<UnitField {...base} kind={kind} unit={unit} value="1.5" readOnly />);
    assert.match(locked, /readonly=""/i);
    assert.doesNotMatch(locked, / disabled=|aria-invalid|edit-reason/);
  });

  it(kind + ' requires a nonblank unit', () => {
    assert.throws(() => renderToStaticMarkup(<UnitField {...base} kind={kind} unit=" " value="" />), /field unit/);
  });
}
