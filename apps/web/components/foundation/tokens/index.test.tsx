import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync, symlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { Badge, Button, Dialog, Drawer, Tabs } from './index';
import type { ModalPresentationProps } from './contracts';
import { presentationFixtures as fixtures } from './fixtures';
import tokens from '../../../styles/product-tokens.json';
import { foundationMetadata, overlayMetadata } from '../../../../../tools/browser-test-runner';

describe('production presentation contracts', () => {
  testCases(fixtures.variants)('renders native labelled %s button with safe default type', variant => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} variant={variant} />);
    assert.ok((html)?.includes('<button type="button"'));
    assert.ok((html)?.includes(fixtures.buttonLabel));
    assert.ok((html)?.includes('role="button"') === false);
  });
  it('preserves a busy button name and identifies the reason without fake completion', () => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} busy disabledReason={fixtures.busyReason} />);
    assert.ok((html)?.includes('aria-busy="true"'));
    assert.ok((html)?.includes('aria-disabled="true"'));
    assert.ok((html)?.includes('aria-describedby='));
    assert.ok((html)?.includes(fixtures.buttonLabel));
    assert.ok((html)?.includes(fixtures.busyReason));
    assert.ok((html)?.includes('width="12" height="12" alt="" aria-hidden="true"'));
    assert.ok((html)?.includes('disabled=""') === false); // Busy retains focus; click is guarded in browser tests.
  });
  it('uses native disabled semantics and a persistent reason', () => {
    const html = renderToStaticMarkup(<Button label={fixtures.buttonLabel} disabled disabledReason={fixtures.disabledReason} />);
    assert.ok((html)?.includes('disabled=""'));
    assert.ok((html)?.includes(fixtures.disabledReason));
  });
  testCases(fixtures.badges)('preserves the explicit $tone label', ({ tone, label }) => {
    const html = renderToStaticMarkup(<Badge tone={tone} label={label} />);
    assert.ok((html)?.includes(label));
    assert.ok((html)?.includes('>Active<') === false);
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Badge tone="unknown" label={'<script>generated</script>'} />);
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.ok((html)?.includes('<script>') === false);
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
    for (const id of controls) assert.ok((html)?.includes(`id="${id}"`));
    assert.ok((html)?.includes(fixtures.tabs[0].content));
    assert.ok((html)?.includes(fixtures.tabs[2].content) === false);
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
    for (const [key, value] of Object.entries(tokens.colors)) assert.ok((css)?.includes(`--ot-${key}: ${value};`));
    for (const value of tokens.spacing_px) assert.ok((css)?.includes(`--ot-space-${value}: ${value}px;`));
    for (const value of tokens.radius_px) assert.ok((css)?.includes(`--ot-radius-${value}: ${value}px;`));
    for (const [name, value] of Object.entries(tokens.typography)) {
      assert.ok((css)?.includes(`--ot-type-${name}-line: ${value.line_height_px / 16}rem;`));
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

// Keep the existing order because the transport IDs use the collected index.
for (const [name, Component] of [['Dialog', Dialog], ['Drawer', Drawer]] as const) {
  describe(`${name} modal presentation contract`, () => {
    const props: ModalPresentationProps = { open: true, title: 'Generated details', children: 'Generated body',
      initialFocus: { kind: 'heading' }, returnFocus: () => null, fallbackFocus: () => null,
      onDismiss: () => { throw new Error('Rendering must never request dismissal'); } };
    it('U01 requires a visible title, associates its heading and removes closed content', () => {
      assert.equal(renderToStaticMarkup(<Component {...props} open={false} />), '');
      assert.throws(() => renderToStaticMarkup(<Component {...props} title=" " />), /visible/);
      const html = renderToStaticMarkup(<Component {...props} />);
      const labelledBy = /aria-labelledby="([^"]+)"/.exec(html)?.[1];
      assert.ok(labelledBy); assert.ok((html)?.includes(`id="${labelledBy}" tabindex="-1"`));
      assert.ok((html)?.includes('Generated details'));
      assert.ok((html)?.includes('aria-label=') === false);
    });
    it('U02 renders only an explicit non-submit close control, without implicit confirmation', () => {
      const html = renderToStaticMarkup(<Component {...props} />);
      assert.ok((html)?.includes('<button type="button"'));
      assert.ok((html)?.includes(name === 'Dialog' ? 'Close dialog' : 'Close details'));
      assert.ok((html)?.includes('<form') === false); assert.ok((html)?.includes('method="dialog"') === false);
      assert.ok((html)?.includes('Confirm') === false);
      const withAction = renderToStaticMarkup(<Component {...props} actions={<Button label="Explicit action" />} />);
      assert.ok(withAction.indexOf(name === 'Dialog' ? 'Close dialog' : 'Close details') < withAction.indexOf('Explicit action'));
      assert.equal((withAction.match(/<button /g))?.length, 2);
    });
    it('U03 requires an explicit initial focus policy and permits an initially empty element ref', () => {
      assert.throws(() => renderToStaticMarkup(<Component {...props} initialFocus={undefined as never} />), /explicit initial focus/);
      assert.throws(() => renderToStaticMarkup(<Component {...props} initialFocus={{ kind: 'unknown' } as never} />), /explicit initial focus/);
      assert.ok((renderToStaticMarkup(<Component {...props} initialFocus={{ kind: 'element', ref: { current: null } }} />))?.includes('<dialog'));
      assert.ok((renderToStaticMarkup(<Component {...props} initialFocus={{ kind: 'close-button' }} />))?.includes('<dialog'));
    });
    it('U04 preserves React content escaping without invoking caller actions', () => {
      const html = renderToStaticMarkup(<Component {...props} title={'<script>generated</script>'}>
        {'<img src=x onerror=generated>'}<Button label="Generated action" onPress={() => { throw new Error('No implicit action'); }} />
      </Component>);
      assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
      assert.ok((html)?.includes('&lt;img src=x onerror=generated&gt;'));
      assert.ok((html)?.includes('<script>') === false);
    });
  });
}

describe('finite overlay evidence attribution', () => {
  const reviewPath = 'apps/web/components/foundation/tokens/testing/overlay-baseline-review.json';
  const manifestPath = 'docs/specification/manifest.json';
  const target = 'apps/web/components/foundation/tokens/visual.spec.ts';
  // Overlay baselines are reviewed for the macOS browser job only. Name that platform explicitly so the
  // admission checks below verify the same committed bytes on every unit host (CI runs them on Linux).
  const baselinePlatform = 'darwin';
  function fixture(check: (root: string, observed: Array<{ id: string; file: string; mode: 'customer_controlled' | 'opentour_managed' }>) => void) {
    const root = realpathSync(mkdtempSync(join(tmpdir(), 'opentour-overlay-metadata-')));
    try {
      const review = JSON.parse(readFileSync(new URL(`../../../../../${reviewPath}`, import.meta.url), 'utf8'));
      const paths = [reviewPath, manifestPath, ...Object.keys(review.images).map(path => `apps/web/components/foundation/tokens/${path}`)];
      for (const path of paths) {
        mkdirSync(join(root, path, '..'), { recursive: true });
        writeFileSync(join(root, path), readFileSync(new URL(`../../../../../${path}`, import.meta.url)));
      }
      const manifest = JSON.parse(readFileSync(join(root, manifestPath), 'utf8'));
      const observed = manifest.test_bindings.filter((binding: { issue_key: string; test_id: string }) =>
        ['OPE-253', 'OPE-300'].includes(binding.issue_key) && binding.test_id.startsWith(`${target}::`))
        .map((binding: { test_id: string; mode: 'customer_controlled' | 'opentour_managed' }) => ({ id: binding.test_id, file: target, mode: binding.mode }));
      check(root, observed);
    } finally { rmSync(root, { recursive: true, force: true }); }
  }
  it('binds exactly122 child browser cases while preserving the disjoint36 parent observations', () => {
    fixture((root, observed) => {
      const value = overlayMetadata(root, observed, baselinePlatform);
      assert.equal(value.issue_key, 'OPE-300'); assert.equal((value.test_ids)?.length, 122);
      assert.equal((observed)?.length, 158); assert.match(value.baseline_manifest_sha256, /^[a-f0-9]{64}$/);
      assert.equal((observed.filter(item => !value.test_ids.includes(item.id)))?.length, 36);
    });
  });
  it('rejects missing, unapproved, stale-specification or foreign-design/source baseline metadata', () => {
    for (const mutation of [null, { browser_baseline_status: 'PENDING' }, { spec_sha256: '0'.repeat(64) },
      { design_image_sha256: '0'.repeat(64) }, { max_diff_pixels: 1 }, { extra: true },
      { independent_browser_baseline_review: { reviewer: 'foreign', source_commit: '0'.repeat(40) } }, 'foreign-review']) {
      fixture((root, observed) => {
        const path = join(root, reviewPath);
        if (mutation === null) rmSync(path);
        else {
          const original = JSON.parse(readFileSync(path, 'utf8'));
          const change = typeof mutation === 'string' ? { independent_browser_baseline_review: { ...original.independent_browser_baseline_review, review_sha256: '0'.repeat(64) } } : mutation;
          writeFileSync(path, JSON.stringify({ ...original, ...change }));
        }
        assert.throws(() => overlayMetadata(root, observed, baselinePlatform));
      });
    }
  });
  it('rejects changed, missing or symlinked candidate image bytes without normalizing them', () => {
    for (const mutation of ['changed', 'missing', 'symlink', 'coordinated-map-replacement']) fixture((root, observed) => {
      const review = JSON.parse(readFileSync(join(root, reviewPath), 'utf8'));
      const path = join(root, 'apps/web/components/foundation/tokens', Object.keys(review.images)[0]);
      if (mutation === 'coordinated-map-replacement') {
        const replacement = Buffer.concat([readFileSync(path), Buffer.from([0])]);
        writeFileSync(path, replacement);
        review.images[Object.keys(review.images)[0]] = createHash('sha256').update(replacement).digest('hex');
        writeFileSync(join(root, reviewPath), JSON.stringify(review, null, 2) + '\n');
      } else if (mutation === 'changed') writeFileSync(path, 'generated invalid image bytes');
      else { rmSync(path); if (mutation === 'symlink') symlinkSync(join(root, reviewPath), path); }
      assert.throws(() => overlayMetadata(root, observed, baselinePlatform));
    });
    // The reviewed bytes are never admitted as another platform's baselines.
    fixture((root, observed) => assert.throws(() => overlayMetadata(root, observed, 'linux')));
  });
  it('rejects renamed or relabelled manifest cases and an incorrect current native capture', () => {
    for (const mutation of ['relabel', 'rename', 'capture', 'identity', 'revision', 'publication']) fixture((root, observed) => {
      const path = join(root, manifestPath); const manifest = JSON.parse(readFileSync(path, 'utf8'));
      const binding = manifest.test_bindings.find((item: { issue_key: string; test_id: string }) => item.issue_key === 'OPE-300' && item.test_id.startsWith(`${target}::`));
      if (mutation === 'relabel') binding.issue_key = 'OPE-253';
      else if (mutation === 'rename') binding.test_id += '-foreign';
      else {
        const child = manifest.source_projection.issues.find((item: { issue_key: string }) => item.issue_key === 'OPE-300');
        if (mutation === 'identity') child.native_uuid = 'foreign';
        else if (mutation === 'revision') child.updated_at = '2000-01-01T00:00:00.000Z';
        else if (mutation === 'publication') child.publication_spec_sha256 = '0'.repeat(64);
        else child.technical_capture_sha256 = '0'.repeat(64);
      }
      writeFileSync(path, JSON.stringify(manifest)); assert.throws(() => overlayMetadata(root, observed, baselinePlatform));
    });
  });
  it('refuses zero, incomplete, duplicate, foreign-file or mismatched-mode observations', () => {
    fixture((root, observed) => {
      for (const values of [[], observed.slice(1), [...observed.slice(1), observed[1]],
        observed.map((value, index) => index ? value : { ...value, file: 'foreign.ts' }),
        observed.map((value, index) => index ? value : { ...value, mode: value.mode === 'customer_controlled' ? 'opentour_managed' as const : 'customer_controlled' as const })]) {
        assert.throws(() => overlayMetadata(root, values, baselinePlatform));
      }
    });
  });
});
