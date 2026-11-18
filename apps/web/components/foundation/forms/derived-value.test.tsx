import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { DerivedValue, type DerivedValueProps } from './derived-value';

const props: DerivedValueProps = { label: 'Generated reference', value: '  0007α\n  ', helpText: 'Current generated value', copyLabel: 'Copy generated reference',
  copyFeedback: { success: 'Copied', failure: 'Could not copy. Select the value and copy it manually.' } };
describe('derived value presentation', () => {
  it.each(['', '  0007  ', '\nα<&>\n', '007.00'])('preserves exact string %j and never submits a value', value => {
    const html = renderToStaticMarkup(<DerivedValue {...props} value={value} />);
    expect(html).toContain(`class="ot-derived-text">${value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}</span>`);
    expect(html).not.toMatch(/<(input|textarea|select)|contenteditable|\sname=/i);
    expect(html).toContain('type="button"');
    expect(html).toContain('role="status" class="ot-derived-feedback"></p>');
    expect(html).not.toContain('Copied');
  });
  it.each(['label', 'helpText', 'copyLabel'] as const)('rejects missing meaningful %s', key => {
    for (const invalid of ['', ' \n ', null, undefined, 7]) {
      expect(() => renderToStaticMarkup(<DerivedValue {...{ ...props, [key]: invalid } as DerivedValueProps} />)).toThrow('visible');
    }
  });
  it.each(['success', 'failure'] as const)('requires safe %s feedback', key => {
    expect(() => renderToStaticMarkup(<DerivedValue {...props} copyFeedback={{ ...props.copyFeedback, [key]: '' }} />)).toThrow('feedback');
  });
  it.each([null, undefined, 0, false, {}])('rejects non-string display %j without coercion', value => {
    expect(() => renderToStaticMarkup(<DerivedValue {...props} value={value as string} />)).toThrow('must be a string');
  });
  it('escapes caller labels, help and copy names', () => {
    const html = renderToStaticMarkup(<DerivedValue {...props} label="<b>Reference</b>" helpText="<script>help</script>" copyLabel="<i>Copy</i>" />);
    expect(html).toContain('&lt;script&gt;help&lt;/script&gt;');
    expect(html).toContain('&lt;b&gt;Reference&lt;/b&gt;');
    expect(html).toContain('&lt;i&gt;Copy&lt;/i&gt;');
    expect(html).not.toMatch(/<script>|<b>|<i>/);
  });
  it('provides unique associations for repeated values', () => {
    const html = renderToStaticMarkup(<><DerivedValue {...props} /><DerivedValue {...props} /></>);
    const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(match => match[1]);
    expect(ids).toHaveLength(6); expect(new Set(ids).size).toBe(6);
    const names = [...html.matchAll(/aria-labelledby="([^"]+)"/g)].map(match => match[1]);
    const descriptions = [...html.matchAll(/aria-describedby="([^"]+)"/g)].map(match => match[1]);
    expect(names).toEqual([ids[0], ids[0], ids[3], ids[3]]);
    expect(descriptions).toEqual([`${ids[1]} ${ids[2]}`, `${ids[4]} ${ids[5]}`]);
    expect(html.match(/role="status"/g)).toHaveLength(2);
    expect(html).not.toContain('aria-live');
  });
  it('does not forward arbitrary form, style or behavior overrides', () => {
    const extra = { ...props, name: 'revision', onChange: () => {}, role: 'textbox', className: 'override', style: { display: 'none' }, tabIndex: 0, children: 'secret' };
    expect(renderToStaticMarkup(<DerivedValue {...extra} />)).toBe(renderToStaticMarkup(<DerivedValue {...props} />));
  });
});
