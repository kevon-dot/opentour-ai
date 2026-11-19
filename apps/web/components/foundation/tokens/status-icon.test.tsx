import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { StatusIcon, statusIconTones } from './status-icon';
import type { StatusIconProps, StatusIconTone } from './status-icon.contracts';
import { statusIconFixtures as fixtures } from './status-icon.fixtures';

describe('semantic status icon presentation contract', () => {
  it.each(fixtures.examples)('$tone retains the supplied label and hides the decorative glyph', props => {
    const html = renderToStaticMarkup(<StatusIcon {...props} />);
    expect(html).toContain(`<span class="ot-status-icon-label">${props.label}</span>`);
    expect(html).toContain('<span class="ot-status-icon-symbol" aria-hidden="true">');
    expect(html).not.toMatch(/role=|aria-live|aria-label=|tabindex|<button|<a\b|<title/);
    expect(html.replace(/<[^>]+>/g, '')).toBe(props.label);
  });
  it.each(statusIconTones)('never changes nonstandard domain text for %s', tone => {
    const html = renderToStaticMarkup(<StatusIcon tone={tone} label={fixtures.nonstandard.label} />);
    expect(html.replace(/<[^>]+>/g, '')).toBe(fixtures.nonstandard.label);
  });
  it.each(['', ' ', '\n', undefined, null, 0])('rejects missing visible label %j', label => {
    expect(() => renderToStaticMarkup(<StatusIcon tone="success" label={label as string} />)).toThrow('visible');
  });
  it.each(['complete', 'danger', '', undefined, null, 0, {}])('rejects undeclared tone %j', tone => {
    expect(() => renderToStaticMarkup(<StatusIcon tone={tone as StatusIconTone} label="Generated" />)).toThrow('declared status icon tone');
  });
  it('escapes exact caller text instead of making markup or introducing a replacement label', () => {
    const html = renderToStaticMarkup(<StatusIcon tone="info" label={'<script>Generated & unconfirmed</script>'} />);
    expect(html).toContain('&lt;script&gt;Generated &amp; unconfirmed&lt;/script&gt;');
    expect(html).not.toContain('<script>');
  });
  it('does not forward overrides that could remove text, introduce a live role or make a keyboard stop', () => {
    const props = { tone: 'success', label: 'Generated', role: 'alert', tabIndex: 0, 'aria-label': 'Completed',
      style: { display: 'none' }, children: 'Completed', className: 'override' } as unknown as StatusIconProps;
    expect(renderToStaticMarkup(<StatusIcon {...props} />)).toBe(renderToStaticMarkup(<StatusIcon tone="success" label="Generated" />));
  });
  it('uses unique clipping references when multiple mapped alert glyphs share a page', () => {
    const html = renderToStaticMarkup(<>{fixtures.examples.filter(example => ['hold', 'unknown', 'info'].includes(example.tone))
      .map(example => <StatusIcon key={example.tone} {...example} />)}</>);
    const ids = [...html.matchAll(/clipPath id="([^"]+)"/g)].map(match => match[1]);
    expect(ids).toHaveLength(3); expect(new Set(ids).size).toBe(3);
    for (const id of ids) expect(html).toContain(`clip-path="url(#${id})"`);
  });
});
