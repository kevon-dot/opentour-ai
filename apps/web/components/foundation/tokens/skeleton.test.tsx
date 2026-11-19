import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Skeleton } from './skeleton';
import type { SkeletonProps, SkeletonVariant } from './skeleton.contracts';
import { skeletonFixtures as fixtures } from './skeleton.fixtures';
import tokens from '../../../styles/product-tokens.json';

describe('static skeleton presentation contract', () => {
  it.each(fixtures.variants)('%s exposes the visible loading label once and hides every placeholder', variant => {
    const html = renderToStaticMarkup(<Skeleton variant={variant} />);
    expect(html.match(/role="status"/g)).toHaveLength(1);
    expect(html.match(/Loading…/g)).toHaveLength(1);
    expect(html).toContain('<p class="ot-skeleton-label" role="status">Loading…</p>');
    expect(html.match(/class="ot-skeleton-bar" aria-hidden="true"/g)).toHaveLength(3);
    expect(html).not.toMatch(/aria-live|aria-atomic|aria-busy|progressbar|aria-value|tabindex|button|alert/);
  });

  it.each([undefined, null, '', 'Rows', 'line', 0, {}])('rejects undeclared variant %j', variant => {
    expect(() => renderToStaticMarkup(<Skeleton variant={variant as SkeletonVariant} />))
      .toThrow('A declared skeleton variant is required.');
  });

  it('does not forward arbitrary props that could change geometry, semantics or content', () => {
    const props = { variant: 'rows', role: 'alert', className: 'override', style: { display: 'none' },
      children: '<script>generated</script>', 'aria-label': 'Complete', percent: 100 } as unknown as SkeletonProps;
    expect(renderToStaticMarkup(<Skeleton {...props} />)).toBe(renderToStaticMarkup(<Skeleton variant="rows" />));
  });

  it.each(fixtures.variants)('%s renders no per-poll counter, nonce or progress value', variant => {
    const initial = renderToStaticMarkup(<Skeleton variant={variant} />);
    for (let repeat = 0; repeat < 5; repeat++) expect(renderToStaticMarkup(<Skeleton variant={variant} />)).toBe(initial);
    expect(initial.replace(/<[^>]*>/g, '')).toBe('Loading…');
    expect(initial).not.toMatch(/0%|100%|complete|succeeded|data-time|data-count/);
  });

  it('keeps the loading text above normal-text contrast on the consumer surface', () => {
    function luminance(hex: string) {
      const channels = hex.slice(1).match(/../g)!.map(value => parseInt(value, 16) / 255)
        .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
    }
    for (const surface of ['surface-base', 'surface-canvas'] as const) {
      expect((luminance(tokens.colors[surface]) + 0.05) / (luminance(tokens.colors['text-secondary']) + 0.05)).toBeGreaterThanOrEqual(4.5);
    }
  });
});
