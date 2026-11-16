import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { describe, expect, it } from 'vitest';
import { Badge, Button, Dialog, Drawer, Tabs } from './index';
import type { ModalPresentationProps } from './contracts';
import { presentationFixtures as fixtures } from './fixtures';
import tokens from '../../../styles/product-tokens.json';
import { foundationMetadata } from '../../../../../tools/browser-test-runner';

describe('production presentation contracts', () => {
  it.each(fixtures.variants)('renders native labelled %s button with safe default type', variant => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} variant={variant} />);
    expect(html).toContain('<button type="button"');
    expect(html).toContain(fixtures.buttonLabel);
    expect(html).not.toContain('role="button"');
  });
  it('preserves a busy button name and identifies the reason without fake completion', () => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} busy disabledReason={fixtures.busyReason} />);
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain('aria-describedby=');
    expect(html).toContain(fixtures.buttonLabel);
    expect(html).toContain(fixtures.busyReason);
    expect(html).toContain('width="12" height="12" alt="" aria-hidden="true"');
    expect(html).not.toContain('disabled=""'); // Busy retains focus; click is guarded in browser tests.
  });
  it('uses native disabled semantics and a persistent reason', () => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} disabled disabledReason={fixtures.disabledReason} />);
    expect(html).toContain('disabled=""');
    expect(html).toContain(fixtures.disabledReason);
  });
  it.each(fixtures.badges)('preserves the explicit $tone label', ({ tone, label }) => {
    const html = renderToStaticMarkup(<Badge tone={tone} label={label} />);
    expect(html).toContain(label);
    expect(html).not.toContain('>Active<');
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Badge tone="unknown" label={'<script>generated</script>'} />);
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(html).not.toContain('<script>');
  });
  it.each(['', '   ', '\n'])('rejects a missing visible label %j', label => {
    expect(() => renderToStaticMarkup(<Button label={label} />)).toThrow('visible');
    expect(() => renderToStaticMarkup(<Badge tone="unknown" label={label} />)).toThrow('visible');
  });
  it('connects tab and panel IDs, with one tab stop and no inactive panel content', () => {
    const html = renderToStaticMarkup(<Tabs label="Generated panels" selectedKey="overview"
      onSelectionChange={() => { throw new Error('Rendering cannot activate a tab'); }}
      items={fixtures.tabs.map(item => ({ ...item, panel: item.content }))} />);
    expect(html.match(/role="tab"/g)).toHaveLength(4);
    expect(html.match(/aria-selected="true"/g)).toHaveLength(1);
    expect(html.match(/role="tabpanel"/g)).toHaveLength(4);
    const controls = [...html.matchAll(/aria-controls="([^"]+)"/g)].map(match => match[1]);
    for (const id of controls) expect(html).toContain(`id="${id}"`);
    expect(html).toContain(fixtures.tabs[0].content);
    expect(html).not.toContain(fixtures.tabs[2].content);
  });
  it('rejects duplicate keys and selection that is absent or disabled', () => {
    for (const items of [
      [{ key: 'a', label: 'A', panel: null }, { key: 'a', label: 'B', panel: null }],
      [{ key: 'a', label: 'A', panel: null, disabled: true }],
      [{ key: 'b', label: 'B', panel: null }],
    ]) expect(() => renderToStaticMarkup(<Tabs label="Generated" items={items} selectedKey="a"
      onSelectionChange={() => {}} />)).toThrow('unique keys');
  });
});

describe('finite presentation evidence admission', () => {
  function fixture(change: (root: string) => void, check: (root: string, review: { reason: string; designRevision: string }) => void) {
    const root = mkdtempSync(join(tmpdir(), 'opentour-253-metadata-'));
    const files = ['apps/web/styles/fonts/source.json', 'apps/web/styles/fonts/Geist.woff2', 'apps/web/styles/fonts/GeistMono.woff2',
      'apps/web/styles/product-tokens.json', 'apps/web/components/foundation/tokens/testing/baseline-review.json'];
    try {
      for (const path of files) {
        const destination = join(root, path); mkdirSync(join(destination, '..'), { recursive: true });
        writeFileSync(destination, readFileSync(new URL(`../../../../../${path}`, import.meta.url)));
      }
      const manifest = JSON.parse(readFileSync(join(root, files[4]), 'utf8'));
      const review = { reason: 'Generated local baseline authoring fixture', designRevision: manifest.design_revision };
      change(root); check(root, review);
    } finally { rmSync(root, { recursive: true, force: true }); }
  }
  it('binds qualified Geist sources and the reviewed focus revision without depending on the 127 font', () => {
    fixture(() => {}, (root, review) => {
      const metadata = foundationMetadata(root, review);
      expect(metadata.issue_key).toBe('OPE-253'); expect(metadata.fixture_id).toBe('presentation-foundation-v1');
      expect(metadata.fonts).toHaveLength(2); expect(metadata).not.toHaveProperty('font_sha256');
    });
  });
  it('rejects an unreviewed or missing browser baseline in ordinary execution', () => {
    fixture(() => {}, root => expect(() => foundationMetadata(root)).toThrow('INVALID_CONFIGURATION'));
  });
  it('rejects baseline authoring bound to a different design revision', () => {
    fixture(() => {}, (root, review) => expect(() => foundationMetadata(root, { ...review, designRevision: 'foreign' })).toThrow('INVALID_CONFIGURATION'));
  });
  it('rejects modified font bytes even when the font path is present', () => {
    fixture(root => writeFileSync(join(root, 'apps/web/styles/fonts/Geist.woff2'), 'generated-invalid-font'),
      (root, review) => expect(() => foundationMetadata(root, review)).toThrow('INVALID_CONFIGURATION'));
  });
  it('rejects an altered focus policy rather than inheriting the 127 exception', () => {
    fixture(root => {
      const path = join(root, 'apps/web/components/foundation/tokens/testing/baseline-review.json');
      const value = JSON.parse(readFileSync(path, 'utf8')); value.max_diff_pixels = 14;
      writeFileSync(path, JSON.stringify(value));
    }, (root, review) => expect(() => foundationMetadata(root, review)).toThrow('INVALID_CONFIGURATION'));
  });
});

describe('design token integrity', () => {
  const css = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');
  it('keeps the CSS colors, spacing and radii equal to the recorded design values', () => {
    for (const [key, value] of Object.entries(tokens.colors)) expect(css).toContain(`--ot-${key}: ${value};`);
    for (const value of tokens.spacing_px) expect(css).toContain(`--ot-space-${value}: ${value}px;`);
    for (const value of tokens.radius_px) expect(css).toContain(`--ot-radius-${value}: ${value}px;`);
    for (const [name, value] of Object.entries(tokens.typography)) {
      expect(css).toContain(`--ot-type-${name}-line: ${value.line_height_px / 16}rem;`);
    }
  });
  function luminance(hex: string) {
    const channels = hex.slice(1).match(/../g)!.map(channel => parseInt(channel, 16) / 255)
      .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  it('measures normal-text contrast for declared semantic pairs and primary buttons', () => {
    const pairs = [
      ...(['success', 'learning', 'hold', 'failure', 'unknown'] as const).map(tone =>
        [tokens.colors[`${tone}-text`], tokens.colors[`${tone}-background`]]),
      [tokens.colors['text-link'], tokens.colors['action-subtle']],
      [tokens.colors['text-inverse'], tokens.colors['action-primary']],
      [tokens.colors['text-secondary'], tokens.colors['surface-canvas']],
      [tokens.colors['text-primary'], tokens.colors['surface-base']],
    ];
    for (const [foreground, background] of pairs) {
      const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
      expect((values[0] + 0.05) / (values[1] + 0.05)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

// Append-only: historical Vitest transport IDs use the collected index.
describe.each([{ name: 'Dialog', Component: Dialog }, { name: 'Drawer', Component: Drawer }])('$name modal presentation contract', ({ Component, name }) => {
  const props: ModalPresentationProps = { open: true, title: 'Generated details', children: 'Generated body',
    initialFocus: { kind: 'heading' }, returnFocus: () => null, fallbackFocus: () => null,
    onDismiss: () => { throw new Error('Rendering must never request dismissal'); } };
  it('U01 requires a visible title, associates its heading and removes closed content', () => {
    expect(renderToStaticMarkup(<Component {...props} open={false} />)).toBe('');
    expect(() => renderToStaticMarkup(<Component {...props} title=" " />)).toThrow('visible');
    const html = renderToStaticMarkup(<Component {...props} />);
    const labelledBy = /aria-labelledby="([^"]+)"/.exec(html)?.[1];
    expect(labelledBy).toBeTruthy(); expect(html).toContain(`id="${labelledBy}" tabindex="-1"`);
    expect(html).toContain('Generated details');
    expect(html).not.toContain('aria-label=');
  });
  it('U02 renders only an explicit non-submit close control, without implicit confirmation', () => {
    const html = renderToStaticMarkup(<Component {...props} />);
    expect(html).toContain('<button type="button"');
    expect(html).toContain(name === 'Dialog' ? 'Close dialog' : 'Close details');
    expect(html).not.toContain('<form'); expect(html).not.toContain('method="dialog"');
    expect(html).not.toContain('Confirm');
    const withAction = renderToStaticMarkup(<Component {...props} actions={<Button label="Explicit action" />} />);
    expect(withAction.indexOf(name === 'Dialog' ? 'Close dialog' : 'Close details')).toBeLessThan(withAction.indexOf('Explicit action'));
    expect(withAction.match(/<button /g)).toHaveLength(2);
  });
  it('U03 requires an explicit initial focus policy and permits an initially empty element ref', () => {
    expect(() => renderToStaticMarkup(<Component {...props} initialFocus={undefined as never} />)).toThrow('explicit initial focus');
    expect(() => renderToStaticMarkup(<Component {...props} initialFocus={{ kind: 'unknown' } as never} />)).toThrow('explicit initial focus');
    expect(renderToStaticMarkup(<Component {...props} initialFocus={{ kind: 'element', ref: { current: null } }} />)).toContain('<dialog');
    expect(renderToStaticMarkup(<Component {...props} initialFocus={{ kind: 'close-button' }} />)).toContain('<dialog');
  });
  it('U04 preserves React content escaping without invoking caller actions', () => {
    const html = renderToStaticMarkup(<Component {...props} title={'<script>generated</script>'}>
      {'<img src=x onerror=generated>'}<Button label="Generated action" onPress={() => { throw new Error('No implicit action'); }} />
    </Component>);
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(html).toContain('&lt;img src=x onerror=generated&gt;');
    expect(html).not.toContain('<script>');
  });
});
