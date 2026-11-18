import { test, expect, type Page } from '@playwright/test';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import { resolve } from 'node:path';
import { writeFile } from 'node:fs/promises';
import type { TooltipFixtureKind } from './tooltip.fixtures';

declare global { interface Window { showTooltipFixture(kind: TooltipFixtureKind): void; } }
const origin = process.env.OPENTOUR_FIXTURE_ORIGIN ?? 'http://127.0.0.1:4313';
let html = '';
test.use({ launchOptions: { args: ['--disable-gpu', '--disable-skia-runtime-opts', '--deterministic-mode',
  '--run-all-compositor-stages-before-draw', '--num-raster-threads=1', '--js-flags=--random-seed=42'] } });
test.beforeAll(async () => {
  const output = await buildInlineFixture(resolve(import.meta.dirname, '__tooltip_fixture.tsx'), 'iife', `
    import { createRoot } from 'react-dom/client';
    import { flushSync } from 'react-dom';
    import { TooltipFixture } from './tooltip.fixtures';
    import tokens from '../../../styles/product-tokens.css?inline';
    import display from './display.css?inline';
    import tooltip from './tooltip.css?inline';
    import fixtures from './tooltip.fixtures.css?inline';
    const style = document.createElement('style'); style.textContent = tokens + display + tooltip + fixtures; document.head.append(style);
    const root = createRoot(document.querySelector('#root'));
    window.showTooltipFixture = kind => flushSync(() => root.render(<TooltipFixture key={kind} kind={kind}/>));
  `);
  html = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<style>html{background:#fafbfd}body{margin:0}' + output.css + '</style></head><body><div id="root"></div><script>' +
    output.script.replaceAll('</script', '<\\/script') + '</script></body></html>';
});
async function open(page: Page, kind: TooltipFixtureKind = 'evidence') {
  await page.route('**/*', route => route.request().url() === origin + '/' ? route.fulfill({ contentType: 'text/html', body: html }) : route.abort());
  await page.goto(origin);
  await page.evaluate(kind => window.showTooltipFixture(kind), kind);
  await page.evaluate(async () => { await document.fonts.load('400 12px Geist'); await document.fonts.load('500 14px Geist'); await document.fonts.load('600 24px Geist'); await document.fonts.ready; });
  expect(await page.evaluate(() => [...document.fonts].some(face => face.family === 'Geist' && face.status === 'loaded'))).toBe(true);
}
async function show(page: Page, kind: TooltipFixtureKind) {
  await page.evaluate(kind => window.showTooltipFixture(kind), kind);
  const trigger = kind === 'evidence' ? page.getByRole('button', { name: 'View exact upper bound' }) : page.getByTestId('first-trigger');
  await trigger.focus();
  await expect(page.getByRole('tooltip')).toBeVisible();
  return trigger;
}
for (const [width, height] of [[320, 800], [768, 1024], [1280, 900], [1440, 1024]] as const) {
  for (const scale of [1, 2]) {
    test(`tooltip at ${width}px and ${scale * 100}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height });
      await open(page);
      await page.evaluate(scale => { document.documentElement.style.fontSize = `${16 * scale}px`; }, scale);
      for (const kind of ['evidence', 'help', 'long'] as const) {
        const trigger = await show(page, kind);
        const tip = page.getByRole('tooltip');
        const box = await tip.boundingBox();
        const anchor = await trigger.boundingBox();
        expect(box).not.toBeNull(); expect(anchor).not.toBeNull();
        expect(box!.x).toBeGreaterThanOrEqual(16);
        expect(box!.x + box!.width).toBeLessThanOrEqual(width - 16);
        expect(box!.height).toBeGreaterThan(0);
        await expect(tip).toHaveCSS('font-size', `${12 * scale}px`);
        await expect(tip).toHaveCSS('line-height', `${18 * scale}px`);
        await expect(tip).toHaveCSS('font-family', 'Geist, sans-serif');
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        if (scale === 1) {
          expect(box!.width).toBe(kind === 'evidence' ? Math.min(360, width - 32) : 288);
          expect(box!.y).toBe(anchor!.y + anchor!.height + 16);
          if (kind === 'help') expect(box!.height).toBe(42);
          if (kind === 'evidence') {
            expect(box!.height).toBe(width === 320 ? 88 : 70);
            await expect(tip).toHaveCSS('background-color', 'rgb(255, 255, 255)');
          } else await expect(tip).toHaveCSS('background-color', 'rgb(24, 34, 55)');
        }
        const name = `tooltip-${kind}-${width}-${scale * 100}.png`;
        await info.attach(`region-${kind}-${width}-${scale * 100}.png`, { body: await tip.screenshot({ animations: 'disabled' }), contentType: 'image/png' });
        const geometry = await tip.evaluate(node => {
          const box = node.getBoundingClientRect();
          return { x: box.x, y: box.y, width: box.width, height: box.height, paragraphs: [...node.querySelectorAll('p')].map(p => {
            const bounds = p.getBoundingClientRect(); const style = getComputedStyle(p);
            const ranges: { text: string; y: number }[] = [];
            const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
              const text = walker.currentNode;
              for (let i = 0; i < (text.textContent?.length ?? 0); i++) {
                const range = document.createRange(); range.setStart(text, i); range.setEnd(text, i + 1);
                const y = range.getBoundingClientRect().y; const last = ranges.at(-1);
                if (last?.y === y) last.text += text.textContent![i]; else ranges.push({ text: text.textContent![i], y });
              }
            }
            return { text: p.textContent, x: bounds.x - box.x, y: bounds.y - box.y, width: bounds.width, height: bounds.height, font: style.fontFamily, size: style.fontSize, line: style.lineHeight, lines: ranges.map(r => r.text) };
          }) };
        });
        await info.attach(`geometry-${kind}-${width}-${scale * 100}.json`, { body: JSON.stringify(geometry), contentType: 'application/json' });
        if (kind === 'evidence' && width === 320 && scale === 2) {
          expect(geometry.height).toBe(250);
          expect(geometry.paragraphs[0].lines).toEqual(['Exact upper bound: ', '0.00993608194445', '772']);
          expect(geometry.paragraphs[1].lines).toEqual(['Observed Nov 12, ', '09:42 UTC · 300 ', 'eligible cases']);
          const copied = await tip.locator('p').first().evaluate(node => {
            const selection = window.getSelection()!; const range = document.createRange(); range.selectNodeContents(node);
            selection.removeAllRanges(); selection.addRange(range); const result = selection.toString(); selection.removeAllRanges(); return result;
          });
          expect(copied).toBe('Exact upper bound: 0.00993608194445772');
        }
        // Capture the real viewport: expanding it for fullPage can repaint a top-layer popover mid-capture.
        const shot = await page.screenshot({ animations: 'disabled' });
        const surface = await page.evaluate(async ({ data, x, y }) => {
          const image = new Image(); image.src = `data:image/png;base64,${data}`; await image.decode();
          const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
          const context = canvas.getContext('2d')!; context.drawImage(image, 0, 0);
          return [...context.getImageData(Math.floor(x), Math.floor(y), 1, 1).data];
        }, { data: shot.toString('base64'), x: geometry.x + geometry.width / 2, y: geometry.y + geometry.height - 4 });
        expect(surface).toEqual(kind === 'evidence' ? [255, 255, 255, 255] : [24, 34, 55, 255]);
        await writeFile(info.outputPath(name), shot);
        await info.attach(name, { body: shot, contentType: 'image/png' });
        expect(shot).toMatchSnapshot(name, { maxDiffPixels: 0, threshold: 0 });
      }
    });
  }
}
test('hover bridge and tooltip hover persist until pointer leaves both areas', async ({ page }) => {
  await open(page, 'help');
  const trigger = page.getByTestId('first-trigger');
  await trigger.hover();
  const box = (await page.getByRole('tooltip').boundingBox())!;
  await page.mouse.move(box.x + 20, box.y - 8);
  await expect(page.getByRole('tooltip')).toBeVisible();
  await page.mouse.move(box.x + 20, box.y + 10);
  await expect(page.getByRole('tooltip')).toBeVisible();
  await page.mouse.move(2, 2);
  await expect(page.getByRole('tooltip')).toBeHidden();
});
test('Escape preserves focus and suppresses the current hover/focus engagement', async ({ page }) => {
  await open(page, 'interactions');
  const trigger = page.getByTestId('first-trigger');
  await trigger.focus(); await trigger.hover();
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused(); await expect(page.getByRole('tooltip')).toBeHidden();
  await page.mouse.move(2, 2); await trigger.hover();
  await expect(page.getByRole('tooltip')).toBeHidden();
  await page.mouse.move(2, 2); await page.getByRole('button', { name: 'Next control' }).focus();
  await trigger.focus(); await expect(page.getByRole('tooltip')).toBeVisible();
});
test('blur keeps hovered help, and independent triggers retain names and distinct descriptions', async ({ page }) => {
  await open(page, 'interactions');
  const first = page.getByTestId('first-trigger'); const second = page.getByTestId('second-trigger');
  await first.focus(); await first.hover(); await second.focus();
  await expect(page.getByRole('tooltip')).toHaveCount(2);
  await expect(first).toHaveAccessibleName('View details');
  await expect(first).toHaveAccessibleDescription('Required values remain available here. View exact values and source details.');
  await expect(second).toHaveAccessibleDescription('A different description for the comparison.');
  expect(await first.getAttribute('aria-describedby')).not.toBe(await second.getAttribute('aria-describedby'));
  await page.mouse.move(2, 2);
  await expect(page.getByRole('tooltip')).toHaveCount(1);
  await expect(second).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Export details' })).toBeFocused();
  await expect(page.getByText('Export is unavailable for your current access.')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByText('Export is unavailable for your current access.')).toBeVisible();
});
test('exact values stay reachable when evidence tooltip is dismissed', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 }); await open(page);
  const trigger = await show(page, 'evidence');
  await expect(trigger).toHaveAccessibleDescription('Exact upper bound: 0.00993608194445772 Observed Nov 12, 09:42 UTC · 300 eligible cases');
  const tableBefore = await page.getByRole('region', { name: /Always available exact values/ }).boundingBox();
  await page.keyboard.press('Escape');
  await expect(trigger).toHaveAccessibleDescription('Exact upper bound: 0.00993608194445772 Observed Nov 12, 09:42 UTC · 300 eligible cases');
  await expect(page.getByRole('cell', { name: '0.00993608194445772', exact: true })).toHaveText('0.00993608194445772');
  await expect(trigger).toBeFocused();
  expect(await page.getByRole('region', { name: /Always available exact values/ }).boundingBox()).toEqual(tableBefore);
  await page.keyboard.press('Tab');
  await expect(page.getByRole('region', { name: /Always available exact values/ })).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect.poll(() => page.getByRole('region', { name: /Always available exact values/ }).evaluate(node => node.scrollLeft)).toBeGreaterThan(0);
});
test('edge placement flips, wraps large text and recomputes after viewport resize', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 400 }); await open(page, 'edge');
  await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
  const trigger = await show(page, 'edge');
  const box = (await page.getByRole('tooltip').boundingBox())!;
  const anchor = (await trigger.boundingBox())!;
  expect(box.y + box.height).toBe(anchor.y - 16);
  expect(await page.getByRole('tooltip').evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await page.setViewportSize({ width: 768, height: 1024 });
  await expect.poll(async () => (await page.getByRole('tooltip').boundingBox())!.x).toBeGreaterThan(16);
  expect((await page.getByRole('tooltip').boundingBox())!.y + (await page.getByRole('tooltip').boundingBox())!.height)
    .toBe((await trigger.boundingBox())!.y - 16);
});
test('scroll repositions an open tooltip without moving focus', async ({ page }) => {
  await open(page, 'scroll'); await page.getByTestId('first-trigger').focus();
  const before = (await page.getByRole('tooltip').boundingBox())!;
  await page.evaluate(() => window.scrollTo(0, 20));
  await expect.poll(async () => (await page.getByRole('tooltip').boundingBox())!.y).toBe(before.y - 20);
  await expect(page.getByTestId('first-trigger')).toBeFocused();
});
test('forced colors retain the boundary, readable text and focus indication', async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 800 }); await page.emulateMedia({ forcedColors: 'active' });
  await open(page, 'help'); await show(page, 'help');
  await expect(page.getByRole('tooltip')).toHaveCSS('border-top-width', '1px');
  await expect(page.getByRole('tooltip')).toHaveCSS('color', 'rgb(0, 0, 0)');
  await expect(page.getByTestId('first-trigger')).toHaveCSS('outline-width', '2px');
  const shot = await page.screenshot({ animations: 'disabled' });
  await writeFile(info.outputPath('tooltip-forced-colors.png'), shot);
  await info.attach('tooltip-forced-colors.png', { body: shot, contentType: 'image/png' });
  expect(shot).toMatchSnapshot('tooltip-forced-colors.png', { maxDiffPixels: 0, threshold: 0 });
});
