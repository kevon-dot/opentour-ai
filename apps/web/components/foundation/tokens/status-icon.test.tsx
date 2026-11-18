import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { StatusIcon, statusIconTones } from './status-icon';
import type { StatusIconProps, StatusIconTone } from './status-icon.contracts';
import { statusIconFixtures as fixtures } from './status-icon.fixtures';

describe('semantic status icon presentation contract', () => {
  testCases(fixtures.examples)('$tone retains the supplied label and hides the decorative glyph', props => {
    const html = renderToStaticMarkup(<StatusIcon {...props} />);
    assert.ok((html)?.includes(`<span class="ot-status-icon-label">${props.label}</span>`));
    assert.ok((html)?.includes('<span class="ot-status-icon-symbol" aria-hidden="true">'));
    assert.doesNotMatch(html, /role=|aria-live|aria-label=|tabindex|<button|<a\b|<title/);
    assert.equal(html.replace(/<[^>]+>/g, ''), props.label);
  });
  testCases(statusIconTones)('never changes nonstandard domain text for %s', tone => {
    const html = renderToStaticMarkup(<StatusIcon tone={tone} label={fixtures.nonstandard.label} />);
    assert.equal(html.replace(/<[^>]+>/g, ''), fixtures.nonstandard.label);
  });
  testCases(['', ' ', '\n', undefined, null, 0])('rejects missing visible label %j', label => {
    assert.throws(() => renderToStaticMarkup(<StatusIcon tone="success" label={label as string} />), /visible/);
  });
  testCases(['complete', 'danger', '', undefined, null, 0, {}])('rejects undeclared tone %j', tone => {
    assert.throws(() => renderToStaticMarkup(<StatusIcon tone={tone as StatusIconTone} label="Generated" />), /declared status icon tone/);
  });
  it('escapes exact caller text instead of making markup or introducing a replacement label', () => {
    const html = renderToStaticMarkup(<StatusIcon tone="info" label={'<script>Generated & unconfirmed</script>'} />);
    assert.ok((html)?.includes('&lt;script&gt;Generated &amp; unconfirmed&lt;/script&gt;'));
    assert.ok((html)?.includes('<script>') === false);
  });
  it('does not forward overrides that could remove text, introduce a live role or make a keyboard stop', () => {
    const props = { tone: 'success', label: 'Generated', role: 'alert', tabIndex: 0, 'aria-label': 'Completed',
      style: { display: 'none' }, children: 'Completed', className: 'override' } as unknown as StatusIconProps;
    assert.equal(renderToStaticMarkup(<StatusIcon {...props} />), renderToStaticMarkup(<StatusIcon tone="success" label="Generated" />));
  });
  it('uses unique clipping references when multiple mapped alert glyphs share a page', () => {
    const html = renderToStaticMarkup(<>{fixtures.examples.filter(example => ['hold', 'unknown', 'info'].includes(example.tone))
      .map(example => <StatusIcon key={example.tone} {...example} />)}</>);
    const ids = [...html.matchAll(/clipPath id="([^"]+)"/g)].map(match => match[1]);
    assert.equal((ids)?.length, 3); assert.equal(new Set(ids).size, 3);
    for (const id of ids) assert.ok((html)?.includes(`clip-path="url(#${id})"`));
  });
});
