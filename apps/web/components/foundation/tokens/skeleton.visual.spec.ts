import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { build } from 'vite';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { skeletonFixtures as fixtures } from './skeleton.fixtures';
import type { SkeletonVariant } from './skeleton.contracts';

let script: string;
let styles: string;
const unexpected = new WeakMap<Page, string[]>();

test.beforeAll(async () => {
  const result = await build({
    root: join(process.cwd(), 'apps/web'), configFile: false, envFile: false, publicDir: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
    build: { write: false, lib: { entry: join(process.cwd(),
      'apps/web/components/foundation/tokens/testing/skeleton-gallery.tsx'), name: 'SkeletonFixture', formats: ['iife'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing skeleton fixture bundle.');
  const js = bundle.output.find(item => item.type === 'chunk');
  const css = bundle.output.find(item => item.type === 'asset' && item.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing skeleton fixture assets.');
  script = js.code;
  styles = String(css.source);
});

test.afterEach(async ({ page }) => { expect(unexpected.get(page)).toEqual([]); });

async function openGallery(page: Page, baseURL: string | undefined, mode: string, variant: SkeletonVariant,
  viewport = { width: 1280, height: 900 }, scale = 100) {
  if (!baseURL) throw new Error('Use the supervised browser runner.');
  const rejected: string[] = [];
  unexpected.set(page, rejected);
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.origin === baseURL && url.pathname === '/skeleton-fixture' && request.method() === 'GET') {
      await route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Static skeleton fixture</title></head><body><div id="root"></div></body></html>' });
    } else { rejected.push(`${request.method()}:${url.origin}${url.pathname}`); await route.abort(); }
  });
  await page.setViewportSize(viewport);
  await page.goto(`${baseURL}/skeleton-fixture?mode=${mode}&variant=${variant}&tenant=${mode === 'opentour-managed' ? 'b' : 'a'}`);
  await page.addStyleTag({ content: styles + `html { font-size: ${scale}% }` });
  await page.addScriptTag({ content: script });
  await expect(page.getByRole('heading', { name: fixtures.title })).toBeVisible();
  await page.evaluate(async () => { await document.fonts.load('400 12px Geist'); await document.fonts.ready; });
  expect(await page.evaluate(() => document.fonts.check('400 12px Geist'))).toBe(true);
  await expect(page.getByRole('status')).toHaveCount(0);
}

async function startLoading(page: Page) {
  await page.getByRole('button', { name: fixtures.startLabel, exact: true }).click();
  await expect(page.getByRole('status')).toHaveCount(1);
  await expect(page.getByRole('status')).toHaveText('Loading…');
}

async function geometry(page: Page) {
  return page.evaluate(() => {
    const box = (element: Element) => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    const skeleton = document.querySelector('.ot-skeleton');
    const label = document.querySelector('.ot-skeleton-label');
    return {
      viewport: { width: innerWidth, height: innerHeight },
      horizontalOverflow: document.documentElement.scrollWidth - innerWidth,
      slot: box(document.querySelector('.skeleton-slot')!), following: box(document.querySelector('.skeleton-following')!),
      skeleton: skeleton && box(skeleton), label: label && box(label),
      bars: [...document.querySelectorAll('.ot-skeleton-bar')].map(element => ({ ...box(element),
        background: getComputedStyle(element).backgroundColor, radius: getComputedStyle(element).borderRadius,
        borderWidth: getComputedStyle(element).borderTopWidth, borderStyle: getComputedStyle(element).borderTopStyle,
        borderColor: getComputedStyle(element).borderTopColor })),
      labelStyle: label && { fontFamily: getComputedStyle(label).fontFamily, fontSize: getComputedStyle(label).fontSize,
        lineHeight: getComputedStyle(label).lineHeight, color: getComputedStyle(label).color, weight: getComputedStyle(label).fontWeight },
    };
  });
}

async function capture(page: Page, info: TestInfo, name: string, specimen = false) {
  const bytes = specimen ? await page.locator('.ot-skeleton').screenshot({ animations: 'disabled', caret: 'hide' })
    : await page.screenshot({ fullPage: true, animations: 'disabled', caret: 'hide' });
  await writeFile(info.outputPath(name), bytes);
  return { path: name, sha256: createHash('sha256').update(bytes).digest('hex') };
}

for (const mode of fixtures.modes) for (const variant of fixtures.variants) {
  for (const viewport of fixtures.viewports) for (const scale of [100, 200]) {
    test(`SK01 SK03 SK04 ${mode} ${variant} ${viewport.width}px ${scale}% text reserves loading and ready space`, async ({ page, baseURL }, info) => {
      await openGallery(page, baseURL, mode, variant, viewport, scale);
      await startLoading(page);
      const loading = await geometry(page);
      const expectedWidth = Math.min(360, viewport.width - (viewport.width >= 1280 ? 64 : viewport.width >= 768 ? 48 : 32));
      expect(loading.horizontalOverflow).toBe(0);
      expect(loading.skeleton).toEqual({ x: loading.slot.x, y: loading.slot.y, width: expectedWidth, height: scale === 100 ? 102 : 120 });
      expect(loading.label).toEqual({ x: loading.slot.x, y: loading.slot.y, width: expectedWidth, height: 18 * scale / 100 });
      expect(loading.labelStyle).toEqual({ fontFamily: 'Geist, sans-serif', fontSize: `${12 * scale / 100}px`,
        lineHeight: `${18 * scale / 100}px`, color: 'rgb(81, 96, 120)', weight: '400' });
      for (const [index, bar] of loading.bars.entries()) {
        expect(bar).toEqual({ x: loading.slot.x, y: loading.slot.y + 18 * scale / 100 + 12 + index * 28,
          width: Math.min(expectedWidth, fixtures.widths[variant][index]), height: 16,
          background: 'rgb(237, 240, 245)', radius: '4px', borderWidth: '0px', borderStyle: 'none', borderColor: 'rgb(24, 34, 55)' });
      }
      expect(loading.bars).toHaveLength(3);
      const label = page.getByRole('status');
      expect(await label.evaluate(node => node.scrollWidth <= node.clientWidth && node.scrollHeight <= node.clientHeight)).toBe(true);
      const prefix = `skeleton-${mode}-${variant}-${viewport.width}-${scale}`;
      const captures = [await capture(page, info, `${prefix}-loading.png`), await capture(page, info, `${prefix}-specimen.png`, true)];
      await page.getByRole('button', { name: fixtures.readyLabel, exact: true }).click();
      await expect(page.getByRole('status')).toHaveCount(0);
      await expect(page.getByText(fixtures.loadedTitle, { exact: true })).toBeVisible();
      const ready = await geometry(page);
      expect(ready.slot).toEqual(loading.slot);
      expect(ready.following).toEqual(loading.following);
      expect(ready.horizontalOverflow).toBe(0);
      for (const row of await page.locator('.skeleton-ready p').all()) {
        expect(await row.evaluate(node => node.scrollWidth <= node.clientWidth && node.scrollHeight <= node.clientHeight)).toBe(true);
      }
      captures.push(await capture(page, info, `${prefix}-ready.png`));
      await writeFile(info.outputPath('skeleton-geometry.json'), JSON.stringify({ mode, variant, scale,
        figma_node: fixtures.figmaNodes[variant], loading, ready, captures, fixture_kind: 'generated-presentation' }, null, 2));
    });
  }

  test(`SK02 ${mode} ${variant} exposes one status and keeps its text and focus stable on rerender`, async ({ page, baseURL }, info) => {
    await openGallery(page, baseURL, mode, variant);
    await page.evaluate(() => {
      const state = { exposures: [] as string[], changes: 0, status: null as Element | null, text: null as ChildNode | null };
      (window as typeof window & { skeletonProbe: typeof state }).skeletonProbe = state;
      new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
          if (node instanceof Element) for (const status of node.querySelectorAll('[role="status"]')) state.exposures.push(status.textContent ?? '');
        }
      }).observe(document.querySelector('.skeleton-slot')!, { childList: true });
    });
    await startLoading(page);
    const start = page.getByRole('button', { name: fixtures.startLabel, exact: true });
    await expect(start).toBeFocused();
    await expect(page.getByRole('status')).toMatchAriaSnapshot('- status: Loading…');
    await expect(page.locator('.ot-skeleton [aria-hidden="true"]')).toHaveCount(3);
    expect(await page.locator('.ot-skeleton').evaluate(node => node.querySelectorAll('button, a, input, [tabindex], [role="progressbar"], [aria-valuenow]').length)).toBe(0);
    await page.evaluate(() => {
      const state = (window as typeof window & { skeletonProbe: { status: Element | null; text: ChildNode | null; changes: number } }).skeletonProbe;
      state.status = document.querySelector('[role="status"]'); state.text = state.status!.firstChild;
      new MutationObserver(records => { state.changes += records.length; }).observe(state.status!, { characterData: true, childList: true, subtree: true, attributes: true });
    });
    for (let count = 1; count <= 5; count++) {
      // Dispatch without moving focus, as a caller's poll-triggered render would do.
      await page.getByRole('button', { name: fixtures.rerenderLabel, exact: true }).dispatchEvent('click');
      await expect(page.locator('main')).toHaveAttribute('data-renders', String(count));
      await expect(start).toBeFocused();
    }
    const probe = await page.evaluate(() => {
      const state = (window as typeof window & { skeletonProbe: { status: Element | null; text: ChildNode | null; exposures: string[]; changes: number } }).skeletonProbe;
      const status = document.querySelector('[role="status"]');
      return { exposures: state.exposures, changes: state.changes, sameStatus: status === state.status,
        sameTextNode: status!.firstChild === state.text, statusCount: document.querySelectorAll('[role="status"], [role="alert"], [aria-live]').length,
        loadingText: status!.textContent, storageKeys: { local: Object.keys(localStorage), session: Object.keys(sessionStorage) } };
    });
    expect(probe).toEqual({ exposures: ['Loading…'], changes: 0, sameStatus: true, sameTextNode: true,
      statusCount: 1, loadingText: 'Loading…', storageKeys: { local: [], session: [] } });
    expect(unexpected.get(page)).toEqual([]);
    await writeFile(info.outputPath('skeleton-status.json'), JSON.stringify({ mode, variant, probe,
      rejected_requests: unexpected.get(page), manual_screen_reader: 'NOT_RUN', fixture_kind: 'generated-presentation' }, null, 2));
  });

  for (const treatment of ['reduced-motion', 'forced-colors'] as const) {
    test(`SK05 ${mode} ${variant} ${treatment} preserves static loading and visible boundaries`, async ({ page, baseURL }, info) => {
      await page.emulateMedia({ reducedMotion: 'no-preference', forcedColors: 'none' });
      await openGallery(page, baseURL, mode, variant);
      await startLoading(page);
      const before = await geometry(page);
      const normal = await page.locator('.ot-skeleton').screenshot();
      await page.emulateMedia(treatment === 'reduced-motion' ? { reducedMotion: 'reduce' } : { forcedColors: 'active' });
      const after = await geometry(page);
      expect(after.slot).toEqual(before.slot); expect(after.skeleton).toEqual(before.skeleton);
      expect(await page.locator('.ot-skeleton, .ot-skeleton *').evaluateAll(nodes => nodes.every(node => {
        const style = getComputedStyle(node); return style.animationName === 'none' && style.transitionDuration === '0s';
      }))).toBe(true);
      expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
      await expect(page.getByRole('status')).toHaveText('Loading…');
      if (treatment === 'reduced-motion') {
        expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
        expect(await page.locator('.ot-skeleton').screenshot()).toEqual(normal);
      } else {
        const system = await page.evaluate(() => {
          const node = document.createElement('span'); node.style.color = 'CanvasText'; node.style.backgroundColor = 'Canvas';
          document.body.append(node); const style = getComputedStyle(node);
          const values = { text: style.color, background: style.backgroundColor }; node.remove(); return values;
        });
        expect(after.labelStyle!.color).toBe(system.text);
        for (const [index, bar] of after.bars.entries()) {
          expect(bar.background).toBe(system.background); expect(bar.borderColor).toBe(system.text);
          expect(bar.borderStyle).toBe('solid'); expect(bar.borderWidth).toBe('1px');
          expect([bar.width, bar.height]).toEqual([before.bars[index].width, before.bars[index].height]);
        }
      }
      const captureResult = await capture(page, info, `skeleton-${mode}-${variant}-${treatment}.png`);
      await writeFile(info.outputPath('skeleton-treatment.json'), JSON.stringify({ mode, variant, treatment,
        before, after, capture: captureResult, fixture_kind: 'generated-presentation' }, null, 2));
    });
  }
}
