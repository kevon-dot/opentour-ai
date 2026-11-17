import { test, expect } from '@playwright/test';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { build } from 'vite';
import type { MetricSemanticsProps } from './metric-semantics.types';

const context = {
  title: 'Eligible cases', tone: 'blue', unit: 'cases', numerator: 'Not reported',
  denominator: 'Not reported', observationWindow: 'Nov 1–7', evidence: 'Observed',
  direction: 'No favorable direction',
} as const;
const fixtures: MetricSemanticsProps[] = [
  { ...context, state: 'observed-zero', value: '0', numerator: '0', reason: 'Observed · 0 cases' },
  { ...context, state: 'missing', evidence: 'Missing', reason: 'Source has not reported a count' },
  { ...context, state: 'zero-denominator', title: 'Error rate', unit: '%', numerator: '0', denominator: '0',
    evidence: 'Empty population', direction: 'Lower is better', reason: '0 eligible cases · Rate is not defined' },
  { ...context, state: 'unknown', title: 'Error rate', unit: '%', evidence: 'Unknown', direction: 'Lower is better',
    reason: 'Observation status has not been confirmed.' },
  { ...context, state: 'observed', tone: 'mint', title: 'Critical error rate', value: '0.0%', unit: '%',
    numerator: '0', denominator: '300', direction: 'Lower is better', reason: '0 / 300 · 0 errors / 300 cases' },
];

let script: string;
let styles: string;
test.beforeAll(async () => {
  // Bundle the real component and its pinned local font without starting a server.
  const root = join(process.cwd(), 'apps/web');
  const entry = join(root, 'metric-browser-fixture.tsx');
  const result = await build({
    root, configFile: false, envFile: false, publicDir: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
    plugins: [{
      name: 'metric-fixture', resolveId: id => id === entry ? entry : null,
      load: id => id === entry ? `
        import { createRoot } from 'react-dom/client';
        import { MetricSemantics } from './components/foundation/data-display/metric-semantics';
        createRoot(document.querySelector('main')).render(
          <>{${JSON.stringify(fixtures)}.map((props,i) => <MetricSemantics key={i} {...props} />)}</>
        );` : null,
    }],
    build: { write: false, lib: { entry, name: 'MetricFixture', formats: ['iife'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing metric fixture bundle');
  const js = bundle.output.find(item => item.type === 'chunk');
  const css = bundle.output.find(item => item.type === 'asset' && item.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing metric fixture assets');
  script = js.code;
  styles = String(css.source);
});

for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [1, 2]) {
    test(`metric semantics at ${width}px and ${scale * 100}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.setContent('<html lang="en"><main></main></html>');
      await page.addStyleTag({ content: styles + `
        html {font-size:${scale * 100}%} body {margin:0}
        main {display:grid;gap:var(--ot-space-16);max-width:500px}
      ` });
      await page.addScriptTag({ content: script });
      const cards = page.locator('.ot-metric');
      await expect(cards).toHaveCount(fixtures.length);
      await page.evaluate(() => document.fonts.ready);
      // Native fixture guide 296:259. Each pair is [Metric height, metadata height].
      const expectedHeights = scale === 1 ? Array.from({ length: 5 }, () => [122, 90]) :
        width === 320 ? [[196, 252], [308, 252], [384, 252], [232, 216], [232, 216]] :
          [[196, 180], [196, 216], [272, 180], [232, 216], [196, 180]];
      const lines = await cards.evaluateAll(items => items.map(card => {
        const fields = [...card.children, card.nextElementSibling!];
        return fields.map(field => {
          const rows = new Map<number, string>();
          const walker = document.createTreeWalker(field, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const text = walker.currentNode;
            for (let index = 0; index < text.textContent!.length; index++) {
              const range = document.createRange();
              range.setStart(text, index);
              range.setEnd(text, index + 1);
              const rect = range.getBoundingClientRect();
              if (rect.width > 0) rows.set(rect.y, (rows.get(rect.y) ?? '') + range.toString());
            }
          }
          return [...rows.values()].map(row => row.trim());
        });
      }));
      await writeFile(info.outputPath('lines.json'), JSON.stringify(lines));
      // SHA-256 of each native SVG text layer's tspan lines in DOM order, with
      // entities decoded and trailing layout spaces trimmed. lines.json records any mismatch.
      const nativeLineHashes = {
        normal: 'c261fcb06929cd550f57868f2c7ea436057fbcf361ef16be5fbdbb93b0ec1136',
        largeNarrow: 'd0be1862448679d9eeac0436386321861a7307510ddb22e0b9795bfd497e4bdc',
        largeWide: 'db09252f670fc111eb1a16fe2360788fbbfac25012e7167ec41f3704f586d4b3',
      };
      const profile = scale === 1 ? 'normal' : width === 320 ? 'largeNarrow' : 'largeWide';
      expect(lines[3][1]).toEqual(['Unknown']);
      expect(createHash('sha256').update(JSON.stringify(lines)).digest('hex')).toBe(nativeLineHashes[profile]);
      const actualHeights = await cards.evaluateAll(items => items.map(item => [
        item.getBoundingClientRect().height, item.nextElementSibling!.getBoundingClientRect().height,
      ]));
      expect(actualHeights).toEqual(expectedHeights);
      const values = ['0', 'Not available', 'No eligible cases', 'Unknown', '0.0%'];
      for (const [i, props] of fixtures.entries()) {
        await expect(cards.nth(i).getByRole('term')).toHaveText(props.title);
        await expect(cards.nth(i).getByRole('definition').first()).toHaveText(values[i]);
        const accessible = await cards.nth(i).locator('..').ariaSnapshot();
        for (const text of [props.reason, props.direction, props.evidence, props.unit, props.observationWindow]) {
          expect(accessible).toContain(text);
        }
      }
      await expect(cards.first().locator('.ot-metric-value')).toHaveCSS('font-size', `${32 * scale}px`);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
      const image = await page.screenshot({ fullPage: true, path: info.outputPath('metrics.png') });
      await info.attach('metric-browser-render', { body: image, contentType: 'image/png' });
    });
  }
}
