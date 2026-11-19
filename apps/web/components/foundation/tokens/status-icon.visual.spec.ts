import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { build } from 'vite';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { statusIconFixtures as fixtures } from './status-icon.fixtures';
import tokens from '../../../styles/product-tokens.json' with { type: 'json' };

let script: string, styles: string;
const rejected = new WeakMap<Page, string[]>();
test.beforeAll(async () => {
  const result = await build({ root: join(process.cwd(), 'apps/web'), configFile: false, envFile: false, publicDir: false,
    logLevel: 'error', oxc: { jsx: { runtime: 'automatic' } },
    build: { write: false, lib: { entry: join(process.cwd(), 'apps/web/components/foundation/tokens/testing/status-icon-gallery.tsx'), name: 'StatusIconGallery', formats: ['iife'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing gallery bundle.');
  const js = bundle.output.find(value => value.type === 'chunk');
  const css = bundle.output.find(value => value.type === 'asset' && value.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing gallery assets.');
  script = js.code; styles = String(css.source);
});
test.afterEach(async ({ page }) => { expect(rejected.get(page)).toEqual([]); });

async function open(page: Page, baseURL: string | undefined, mode: string,
  viewport = { width: 1280, height: 900 }, scale = 100) {
  if (!baseURL) throw new Error('Use the supervised browser runner.');
  const requests: string[] = []; rejected.set(page, requests);
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin === baseURL && url.pathname === '/status-icon-fixture' && request.method() === 'GET') {
      await route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Generated status icons</title></head><body><div id="root"></div></body></html>' });
    } else { requests.push(`${request.method()}:${url.origin}${url.pathname}`); await route.abort(); }
  });
  await page.setViewportSize(viewport);
  await page.goto(`${baseURL}/status-icon-fixture?mode=${mode}`);
  await page.addStyleTag({ content: styles + `html { font-size: ${scale}% }` });
  await page.addScriptTag({ content: script });
  await expect(page.getByRole('heading', { name: fixtures.title })).toBeVisible();
  await page.evaluate(async () => { await Promise.all([document.fonts.load('400 12px Geist'), document.fonts.load('500 14px Geist'), document.fonts.load('600 18px Geist')]); await document.fonts.ready; });
  expect(await page.evaluate(() => document.fonts.check('500 14px Geist'))).toBe(true);
  await expect(page.locator('.ot-status-icon')).toHaveCount(7);
}

async function capture(page: Page, info: TestInfo, name: string, selector?: string) {
  const bytes = selector ? await page.locator(selector).screenshot({ animations: 'disabled' }) : await page.screenshot({ fullPage: true, animations: 'disabled' });
  await writeFile(info.outputPath(name), bytes);
  return { path: name, sha256: createHash('sha256').update(bytes).digest('hex') };
}
function rgb(hex: string) { return `rgb(${hex.slice(1).match(/../g)!.map(value => parseInt(value, 16)).join(', ')})`; }

async function measurements(page: Page) {
  return page.locator('.ot-status-icon').evaluateAll(nodes => nodes.map(node => {
    const symbol = node.querySelector('.ot-status-icon-symbol')!, glyph = symbol.firstElementChild!, label = node.querySelector('.ot-status-icon-label')!;
    const box = (element: Element) => { const { x, y, width, height } = element.getBoundingClientRect(); return { x, y, width, height }; };
    const s = getComputedStyle(symbol), l = getComputedStyle(label);
    return { symbol: box(symbol), glyph: box(glyph), label: box(label), text: label.textContent,
      gap: label.getBoundingClientRect().top - symbol.getBoundingClientRect().bottom,
      background: s.backgroundColor, foreground: s.color, radius: s.borderRadius,
      border: [s.borderTopWidth, s.borderTopStyle, s.borderTopColor], font: [l.fontSize, l.lineHeight, l.fontWeight, l.color],
      noClip: label.scrollWidth <= label.clientWidth && label.scrollHeight <= label.clientHeight,
      visibleCheck: symbol.querySelector('img') ? { ...box(symbol.querySelector('img')!), loaded: (symbol.querySelector('img') as HTMLImageElement).complete,
        naturalWidth: (symbol.querySelector('img') as HTMLImageElement).naturalWidth, naturalHeight: (symbol.querySelector('img') as HTMLImageElement).naturalHeight } : null,
      svgStroke: symbol.querySelector('path') ? { color: getComputedStyle(symbol.querySelector('path')!).stroke,
        width: getComputedStyle(symbol.querySelector('path')!).strokeWidth } : null,
    };
  }));
}

for (const mode of fixtures.modes) {
  for (const viewport of fixtures.viewports) for (const scale of [100, 200]) {
    test(`SI01 SI03 ${mode} status icons at ${viewport.width}px ${scale}% text retain exact labels and geometry`, async ({ page, baseURL }, info) => {
      await open(page, baseURL, mode, viewport, scale);
      const geometry = await measurements(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
      for (const [index, row] of geometry.entries()) {
        expect([row.symbol.width, row.symbol.height, row.glyph.width, row.glyph.height, row.gap]).toEqual([28, 28, 20, 20, 12]);
        expect(row.glyph.x - row.symbol.x).toBe(4); expect(row.glyph.y - row.symbol.y).toBe(4);
        expect(row.noClip).toBe(true);
        expect(row.font).toEqual([`${14 * scale / 100}px`, `${20 * scale / 100}px`, '500', rgb(tokens.colors['text-primary'])]);
        expect(row.symbol.x).toBeGreaterThanOrEqual(0); expect(row.label.x + row.label.width).toBeLessThanOrEqual(viewport.width);
        if (index < 5) {
          const tone = fixtures.examples[index].tone;
          const color = tone === 'info' ? 'action-primary' : tone === 'failed' ? 'failure-text' : `${tone}-text` as const;
          const background = tone === 'info' ? 'action-subtle' : tone === 'failed' ? 'failure-background' : `${tone}-background` as const;
          expect(row.foreground).toBe(rgb(tokens.colors[color])); expect(row.background).toBe(rgb(tokens.colors[background]));
          expect(row.text).toBe(fixtures.examples[index].label);
        }
      }
      expect(geometry[0].visibleCheck).toMatchObject({ loaded: true, naturalWidth: 15, naturalHeight: 11 });
      expect(geometry[0].visibleCheck!.width).toBeCloseTo(15.3333, 1);
      expect(geometry[0].visibleCheck!.height).toBeCloseTo(11.1667, 1);
      expect(geometry[0].visibleCheck!.x - geometry[0].glyph.x).toBeCloseTo(2.333333, 1);
      expect(geometry[0].visibleCheck!.y - geometry[0].glyph.y).toBe(4);
      for (const row of geometry.slice(1)) expect(row.svgStroke).toEqual({ color: row.foreground, width: '1.6px' });
      await expect(page.getByText(fixtures.nonstandard.label, { exact: true })).toBeVisible();
      await expect(page.getByText(fixtures.long.label, { exact: true })).toBeVisible();
      const captures = [await capture(page, info, `status-icons-${mode}-${viewport.width}-${scale}.png`)];
      if (viewport.width === 1280 && scale === 100) {
        for (const example of fixtures.examples) captures.push(await capture(page, info, `status-icon-native-${mode}-${example.tone}.png`, `[data-tone="${example.tone}"] .ot-status-icon-symbol`));
        captures.push(await capture(page, info, `status-icon-composition-${mode}.png`, '.status-icon-guide'));
        const dimensions = await page.locator('.status-icon-guide').boundingBox();
        expect([dimensions!.width, dimensions!.height]).toEqual([1096, 164]);
      }
      await writeFile(info.outputPath('status-icon-geometry.json'), JSON.stringify({ mode, viewport, scale, geometry, captures,
        figma_nodes: fixtures.figmaNodes, fixture_kind: 'generated-presentation' }, null, 2));
    });
  }

  test(`SI02 ${mode} decorative icons add no duplicate announcement or keyboard stop`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    await expect(page.locator('.ot-status-icon [aria-hidden="true"]')).toHaveCount(13);
    expect(await page.locator('.ot-status-icon').evaluateAll(nodes => nodes.every(node => !node.querySelector('a, button, [tabindex], [role], [aria-live], [aria-label], title')))).toBe(true);
    const snapshot = await page.locator('.status-icon-additional').ariaSnapshot();
    expect(snapshot).toBe(`- text: ${fixtures.nonstandard.label} ${fixtures.long.label}`);
    await expect(page.locator('.status-icon-additional .ot-status-icon').nth(0)).toMatchAriaSnapshot(`- text: ${fixtures.nonstandard.label}`);
    await expect(page.locator('.status-icon-additional .ot-status-icon').nth(1)).toMatchAriaSnapshot(`- text: ${fixtures.long.label}`);
    await page.getByRole('button', { name: fixtures.beforeLabel }).focus(); await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: fixtures.afterLabel })).toBeFocused();
    await page.evaluate(() => {
      const probe = { mutations: 0 };
      (window as typeof window & { statusIconProbe: typeof probe }).statusIconProbe = probe;
      new MutationObserver(records => { probe.mutations += records.length; }).observe(document.querySelector('.status-icon-examples')!, { childList: true, attributes: true, characterData: true, subtree: true });
    });
    for (let i = 1; i <= 5; i++) {
      await page.getByRole('button', { name: fixtures.rerenderLabel }).dispatchEvent('click');
      await expect(page.locator('main')).toHaveAttribute('data-renders', String(i));
      await expect(page.getByRole('button', { name: fixtures.afterLabel })).toBeFocused();
    }
    const result = await page.evaluate(() => ({ mutations: (window as typeof window & { statusIconProbe: { mutations: number } }).statusIconProbe.mutations,
      announcements: document.querySelectorAll('[role="status"], [role="alert"], [aria-live]').length,
      localKeys: Object.keys(localStorage), sessionKeys: Object.keys(sessionStorage) }));
    expect(result).toEqual({ mutations: 0, announcements: 0, localKeys: [], sessionKeys: [] });
    await writeFile(info.outputPath('status-icon-semantics.json'), JSON.stringify({ mode, result, accessible_text: snapshot,
      requests_rejected: rejected.get(page), manual_screen_reader: 'NOT_RUN', fixture_kind: 'generated-presentation' }, null, 2));
  });

  for (const treatment of ['forced-colors', 'reduced-motion'] as const) {
    test(`SI04 ${mode} ${treatment} retains exact text and non-color shapes`, async ({ page, baseURL }, info) => {
      await page.emulateMedia({ forcedColors: 'none', reducedMotion: 'no-preference' });
      await open(page, baseURL, mode);
      const before = await measurements(page); const original = await page.locator('.status-icon-guide').screenshot();
      await page.emulateMedia(treatment === 'forced-colors' ? { forcedColors: 'active' } : { reducedMotion: 'reduce' });
      const after = await measurements(page);
      for (const [index, row] of after.entries()) {
        expect(row.symbol).toEqual(before[index].symbol); expect(row.glyph).toEqual(before[index].glyph); expect(row.label).toEqual(before[index].label);
        expect(row.text).toBe(before[index].text);
      }
      expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
      if (treatment === 'reduced-motion') expect(await page.locator('.status-icon-guide').screenshot()).toEqual(original);
      else {
        const system = await page.evaluate(() => {
          const node = document.createElement('span'); node.style.color = 'CanvasText'; node.style.background = 'Canvas'; document.body.append(node);
          const style = getComputedStyle(node); const result = { color: style.color, background: style.backgroundColor }; node.remove(); return result;
        });
        for (const row of after) {
          expect(row.foreground).toBe(system.color); expect(row.background).toBe(system.background);
          expect(row.border).toEqual(['1px', 'solid', system.color]); expect(row.font[3]).toBe(system.color);
        }
        const check = await page.locator('.ot-status-icon-check').evaluate(node => {
          const style = getComputedStyle(node, '::before'); return { color: style.backgroundColor, mask: style.maskImage !== 'none', width: parseFloat(style.width), height: parseFloat(style.height) };
        });
        expect(check.color).toBe(system.color); expect(check.mask).toBe(true);
        expect(check.width).toBeCloseTo(15.3333, 1); expect(check.height).toBeCloseTo(11.1667, 1);
      }
      const image = await capture(page, info, `status-icons-${mode}-${treatment}.png`);
      await writeFile(info.outputPath('status-icon-treatment.json'), JSON.stringify({ mode, treatment, before, after, image, fixture_kind: 'generated-presentation' }, null, 2));
    });
  }
}
