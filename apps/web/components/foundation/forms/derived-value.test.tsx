import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { DerivedValue, type DerivedValueProps } from './derived-value';

const props: DerivedValueProps = { label: 'Generated reference', value: '  0007α\n  ', helpText: 'Current generated value', copyLabel: 'Copy generated reference',
  copyFeedback: { success: 'Copied', failure: 'Could not copy. Select the value and copy it manually.' } };
describe('derived value presentation', () => {
  testCases(['', '  0007  ', '\nα<&>\n', '007.00'])('preserves exact string %j and never submits a value', value => {
    const html = renderToStaticMarkup(<DerivedValue {...props} value={value} />);
    assert.ok((html)?.includes(`class="ot-derived-text">${value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}</span>`));
    assert.doesNotMatch(html, /<(input|textarea|select)|contenteditable|\sname=/i);
    assert.ok((html)?.includes('type="button"'));
    assert.ok((html)?.includes('role="status" class="ot-derived-feedback"></p>'));
    assert.ok((html)?.includes('Copied') === false);
  });
  testCases(['label', 'helpText', 'copyLabel'] as const)('rejects missing meaningful %s', key => {
    for (const invalid of ['', ' \n ', null, undefined, 7]) {
      assert.throws(() => renderToStaticMarkup(<DerivedValue {...{ ...props, [key]: invalid } as DerivedValueProps} />), /visible/);
    }
  });
  testCases(['success', 'failure'] as const)('requires safe %s feedback', key => {
    assert.throws(() => renderToStaticMarkup(<DerivedValue {...props} copyFeedback={{ ...props.copyFeedback, [key]: '' }} />), /feedback/);
  });
  testCases([null, undefined, 0, false, {}])('rejects non-string display %j without coercion', value => {
    assert.throws(() => renderToStaticMarkup(<DerivedValue {...props} value={value as string} />), /must be a string/);
  });
  it('escapes caller labels, help and copy names', () => {
    const html = renderToStaticMarkup(<DerivedValue {...props} label="<b>Reference</b>" helpText="<script>help</script>" copyLabel="<i>Copy</i>" />);
    assert.ok((html)?.includes('&lt;script&gt;help&lt;/script&gt;'));
    assert.ok((html)?.includes('&lt;b&gt;Reference&lt;/b&gt;'));
    assert.ok((html)?.includes('&lt;i&gt;Copy&lt;/i&gt;'));
    assert.doesNotMatch(html, /<script>|<b>|<i>/);
  });
  it('provides unique associations for repeated values', () => {
    const html = renderToStaticMarkup(<><DerivedValue {...props} /><DerivedValue {...props} /></>);
    const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(match => match[1]);
    assert.equal((ids)?.length, 6); assert.equal(new Set(ids).size, 6);
    const names = [...html.matchAll(/aria-labelledby="([^"]+)"/g)].map(match => match[1]);
    const descriptions = [...html.matchAll(/aria-describedby="([^"]+)"/g)].map(match => match[1]);
    assert.deepEqual(names, [ids[0], ids[0], ids[3], ids[3]]);
    assert.deepEqual(descriptions, [`${ids[1]} ${ids[2]}`, `${ids[4]} ${ids[5]}`]);
    assert.equal((html.match(/role="status"/g))?.length, 2);
    assert.ok((html)?.includes('aria-live') === false);
  });
  it('does not forward arbitrary form, style or behavior overrides', () => {
    const extra = { ...props, name: 'revision', onChange: () => {}, role: 'textbox', className: 'override', style: { display: 'none' }, tabIndex: 0, children: 'secret' };
    assert.equal(renderToStaticMarkup(<DerivedValue {...extra} />), renderToStaticMarkup(<DerivedValue {...props} />));
  });
});
