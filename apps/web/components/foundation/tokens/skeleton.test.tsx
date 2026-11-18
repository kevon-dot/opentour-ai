import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { Skeleton } from './skeleton';
import type { SkeletonProps, SkeletonVariant } from './skeleton.contracts';
import { skeletonFixtures as fixtures } from './skeleton.fixtures';
import tokens from '../../../styles/product-tokens.json';

describe('static skeleton presentation contract', () => {
  testCases(fixtures.variants)('%s exposes the visible loading label once and hides every placeholder', variant => {
    const html = renderToStaticMarkup(<Skeleton variant={variant} />);
    assert.equal((html.match(/role="status"/g))?.length, 1);
    assert.equal((html.match(/Loading…/g))?.length, 1);
    assert.ok((html)?.includes('<p class="ot-skeleton-label" role="status">Loading…</p>'));
    assert.equal((html.match(/class="ot-skeleton-bar" aria-hidden="true"/g))?.length, 3);
    assert.doesNotMatch(html, /aria-live|aria-atomic|aria-busy|progressbar|aria-value|tabindex|button|alert/);
  });

  testCases([undefined, null, '', 'Rows', 'line', 0, {}])('rejects undeclared variant %j', variant => {
    assert.throws(() => renderToStaticMarkup(<Skeleton variant={variant as SkeletonVariant} />), /A declared skeleton variant is required\./);
  });

  it('does not forward arbitrary props that could change geometry, semantics or content', () => {
    const props = { variant: 'rows', role: 'alert', className: 'override', style: { display: 'none' },
      children: '<script>generated</script>', 'aria-label': 'Complete', percent: 100 } as unknown as SkeletonProps;
    assert.equal(renderToStaticMarkup(<Skeleton {...props} />), renderToStaticMarkup(<Skeleton variant="rows" />));
  });

  testCases(fixtures.variants)('%s renders no per-poll counter, nonce or progress value', variant => {
    const initial = renderToStaticMarkup(<Skeleton variant={variant} />);
    for (let repeat = 0; repeat < 5; repeat++) assert.equal(renderToStaticMarkup(<Skeleton variant={variant} />), initial);
    assert.equal(initial.replace(/<[^>]*>/g, ''), 'Loading…');
    assert.doesNotMatch(initial, /0%|100%|complete|succeeded|data-time|data-count/);
  });

  it('keeps the loading text above normal-text contrast on the consumer surface', () => {
    function luminance(hex: string) {
      const channels = hex.slice(1).match(/../g)!.map(value => parseInt(value, 16) / 255)
        .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
    }
    for (const surface of ['surface-base', 'surface-canvas'] as const) {
      assert.ok((luminance(tokens.colors[surface]) + 0.05) / (luminance(tokens.colors['text-secondary']) + 0.05) >= 4.5);
    }
  });
});
