import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { Badge, Button, Tabs } from './index';
import { presentationFixtures as fixtures } from './fixtures';
import tokens from '../../../styles/product-tokens.json';
import { foundationMetadata } from '../../../../../tools/browser-test-runner';

describe('production presentation contracts', () => {
  testCases(fixtures.variants)('renders native labelled %s button with safe default type', variant => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} variant={variant} />);
    assert.ok(html.includes('<button type="button"'));
    assert.ok(html.includes(fixtures.buttonLabel));
    assert.ok(!html.includes('role="button"'));
  });
  it('preserves a busy button name and identifies the reason without fake completion', () => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} busy disabledReason={fixtures.busyReason} />);
    assert.ok(html.includes('aria-busy="true"'));
    assert.ok(html.includes('aria-disabled="true"'));
    assert.ok(html.includes('aria-describedby='));
    assert.ok(html.includes(fixtures.buttonLabel));
    assert.ok(html.includes(fixtures.busyReason));
    assert.ok(html.includes('width="12" height="12" alt="" aria-hidden="true"'));
    assert.ok(!html.includes('disabled=""')); // Busy retains focus; click is guarded in browser tests.
  });
  it('uses native disabled semantics and a persistent reason', () => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} disabled disabledReason={fixtures.disabledReason} />);
    assert.ok(html.includes('disabled=""'));
    assert.ok(html.includes(fixtures.disabledReason));
  });
  testCases(fixtures.badges)('preserves the explicit $tone label', ({ tone, label }) => {
    const html = renderToStaticMarkup(<Badge tone={tone} label={label} />);
    assert.ok(html.includes(label));
    assert.ok(!html.includes('>Active<'));
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Badge tone="unknown" label={'<script>generated</script>'} />);
    assert.ok(html.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.ok(!html.includes('<script>'));
  });
  testCases(['', '   ', '\n'])('rejects a missing visible label %j', label => {
    assert.throws(() => renderToStaticMarkup(<Button label={label} />), /visible/);
    assert.throws(() => renderToStaticMarkup(<Badge tone="unknown" label={label} />), /visible/);
  });
  it('connects tab and panel IDs, with one tab stop and no inactive panel content', () => {
    const html = renderToStaticMarkup(<Tabs label="Generated panels" selectedKey="overview"
      onSelectionChange={() => { throw new Error('Rendering cannot activate a tab'); }}
      items={fixtures.tabs.map(item => ({ ...item, panel: item.content }))} />);
    assert.equal((html.match(/role="tab"/g))?.length, 4);
    assert.equal((html.match(/aria-selected="true"/g))?.length, 1);
    assert.equal((html.match(/role="tabpanel"/g))?.length, 4);
    const controls = [...html.matchAll(/aria-controls="([^"]+)"/g)].map(match => match[1]);
    for (const id of controls) assert.ok(html.includes(`id="${id}"`));
    assert.ok(html.includes(fixtures.tabs[0].content));
    assert.ok(!html.includes(fixtures.tabs[2].content));
  });
  it('rejects duplicate keys and selection that is absent or disabled', () => {
    for (const items of [
      [{ key: 'a', label: 'A', panel: null }, { key: 'a', label: 'B', panel: null }],
      [{ key: 'a', label: 'A', panel: null, disabled: true }],
      [{ key: 'b', label: 'B', panel: null }],
    ]) assert.throws(() => renderToStaticMarkup(<Tabs label="Generated" items={items} selectedKey="a"
      onSelectionChange={() => {}} />), /unique keys/);
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
      assert.equal(metadata.issue_key, 'OPE-253'); assert.equal(metadata.fixture_id, 'presentation-foundation-v1');
      assert.ok(Array.isArray(metadata.fonts));
      assert.equal(metadata.fonts.length, 2);
      assert.ok(!('font_sha256' in metadata));
    });
  });
  it('rejects an unreviewed or missing browser baseline in ordinary execution', () => {
    fixture(() => {}, root => assert.throws(() => foundationMetadata(root), /INVALID_CONFIGURATION/));
  });
  it('rejects baseline authoring bound to a different design revision', () => {
    fixture(() => {}, (root, review) => assert.throws(() => foundationMetadata(root, { ...review, designRevision: 'foreign' }), /INVALID_CONFIGURATION/));
  });
  it('rejects modified font bytes even when the font path is present', () => {
    fixture(root => writeFileSync(join(root, 'apps/web/styles/fonts/Geist.woff2'), 'generated-invalid-font'),
      (root, review) => assert.throws(() => foundationMetadata(root, review), /INVALID_CONFIGURATION/));
  });
  it('rejects an altered focus policy rather than inheriting the 127 exception', () => {
    fixture(root => {
      const path = join(root, 'apps/web/components/foundation/tokens/testing/baseline-review.json');
      const value = JSON.parse(readFileSync(path, 'utf8')); value.max_diff_pixels = 14;
      writeFileSync(path, JSON.stringify(value));
    }, (root, review) => assert.throws(() => foundationMetadata(root, review), /INVALID_CONFIGURATION/));
  });
});

describe('design token integrity', () => {
  const css = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');
  it('keeps the CSS colors, spacing and radii equal to the recorded design values', () => {
    for (const [key, value] of Object.entries(tokens.colors)) assert.ok(css.includes(`--ot-${key}: ${value};`));
    for (const value of tokens.spacing_px) assert.ok(css.includes(`--ot-space-${value}: ${value}px;`));
    for (const value of tokens.radius_px) assert.ok(css.includes(`--ot-radius-${value}: ${value}px;`));
    for (const [name, value] of Object.entries(tokens.typography)) {
      assert.ok(css.includes(`--ot-type-${name}-line: ${value.line_height_px / 16}rem;`));
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
      assert.ok((values[0] + 0.05) / (values[1] + 0.05) >= 4.5);
    }
  });
});
