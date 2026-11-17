import { test, expect, type Page } from '@playwright/test';
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
const fixtures = [
  { ...context, state: 'observed-zero', value: '0', numerator: '0', reason: 'Observed · 0 cases' },
  { ...context, state: 'missing', evidence: 'Missing', reason: 'Source has not reported a count' },
  { ...context, state: 'zero-denominator', title: 'Error rate', unit: '%', numerator: '0', denominator: '0',
    evidence: 'Empty population', direction: 'Lower is better', reason: '0 eligible cases · Rate is not defined' },
  { ...context, state: 'unknown', title: 'Error rate', unit: '%', evidence: 'Unknown', direction: 'Lower is better',
    reason: 'Observation status has not been confirmed.' },
  { ...context, state: 'observed', tone: 'mint', title: 'Critical error rate', value: '0.0%', unit: '%',
    numerator: '0', denominator: '300', direction: 'Lower is better', reason: '0 / 300 · 0 errors / 300 cases' },
] satisfies MetricSemanticsProps[];

// Original UI-DATA fixture IDs and supplied visible text, in the ticket’s source order.
const sourceFixtures = [
  { id: "24:3", value: "0", props: {
    state: "observed-zero", title: "Eligible cases", tone: "blue",
    value: "0", reason: "cases",
    unit: "cases", numerator: "0", denominator: "Not reported",
    observationWindow: "Nov 1–7", evidence: "Observed", direction: "No favorable direction",
  } },
  { id: "24:10", value: "Not available", props: {
    state: "missing", title: "Eligible cases", tone: "blue",
    reason: "Source has not reported a count",
    unit: "cases", numerator: "Not reported", denominator: "Not reported",
    observationWindow: "Not reported", evidence: "Missing", direction: "No favorable direction",
  } },
  { id: "24:17", value: "No eligible cases", props: {
    state: "zero-denominator", title: "Error rate", tone: "blue",
    reason: "No eligible cases in this population. The rate is undefined.",
    unit: "%", numerator: "0", denominator: "0",
    observationWindow: "Nov 1–7", evidence: "Empty population", direction: "Lower is better",
  } },
  { id: "24:24", value: "0 / 300", props: {
    state: "gate", title: "Critical error rate", tone: "mint",
    value: "0 / 300", reason: "0.0% observed · 95% upper bound 0.994% · Limit 1.000% · Passed",
    unit: "%", numerator: "0", denominator: "300",
    observationWindow: "Not reported", evidence: "Observed", direction: "Lower is better",
  } },
  { id: "24:31", value: "0 / 240", props: {
    state: "gate", title: "Critical error rate", tone: "amber",
    value: "0 / 240", reason: "95% upper bound 1.240% · Limit 1.000% · Inconclusive",
    unit: "%", numerator: "0", denominator: "240",
    observationWindow: "Not reported", evidence: "Observed", direction: "Lower is better",
  } },
  { id: "24:38", value: "60 / 200", props: {
    state: "comparison", title: "Paired comparison", tone: "lilac",
    value: "60 / 200", reason: "30 / 200 · 15 pp · 50% relative error reduction",
    unit: "errors", numerator: "60 baseline / 30 candidate", denominator: "200",
    observationWindow: "Not reported", evidence: "Paired observations", direction: "Lower is better",
  } },
  { id: "24:45", value: "0", props: {
    state: "comparison", title: "Errors", tone: "amber",
    value: "0", reason: "5 · +5 errors · Relative change not defined",
    unit: "errors", numerator: "5 candidate", denominator: "0 baseline",
    observationWindow: "Not reported", evidence: "Comparison", direction: "Lower is better",
  } },
  { id: "24:52", value: "180 of 200 pairs scored", props: {
    state: "incomplete", title: "Paired comparison", tone: "amber",
    value: "180 of 200 pairs scored", reason: "20 candidate results missing · Incomplete",
    unit: "pairs", numerator: "180 complete", denominator: "180 complete pairs",
    observationWindow: "Not reported", evidence: "Incomplete", direction: "Higher is better",
  } },
  { id: "24:59", value: "300", props: {
    state: "stale", title: "Eligible cases", tone: "blue",
    value: "300", reason: "Stale · Last updated 09:42 UTC",
    unit: "cases", numerator: "Not reported", denominator: "Not reported",
    observationWindow: "Not reported", evidence: "Stale", direction: "No favorable direction",
  } },
  { id: "24:66", value: "Access revoked", props: {
    state: "revoked", title: "Private metric", tone: "blue",
    reason: "Access has been revoked. Previously visible values are no longer available.",
  } },
  { id: "24:73", value: "$1,800 benefit", props: {
    state: "forecast", title: "Net value", tone: "amber",
    value: "$1,800 benefit", reason: "$2,700 cost · −$900 net value · Forecast",
    unit: "USD", numerator: "Not reported", denominator: "Not reported",
    observationWindow: "Not reported", evidence: "Forecast", direction: "Higher is better",
  } },
  { id: "24:80", value: "$0 net value", props: {
    state: "forecast", title: "Net value", tone: "blue",
    value: "$0 net value", reason: "Forecast",
    unit: "USD", numerator: "Not reported", denominator: "Not reported",
    observationWindow: "Not reported", evidence: "Forecast", direction: "Higher is better",
  } },
  { id: "24:87", value: "Result unavailable", props: {
    state: "invalid", title: "Error rate", tone: "amber",
    reason: "Invalid result contract",
    unit: "%", numerator: "11", denominator: "10",
    observationWindow: "Not reported", evidence: "Invalid", direction: "Lower is better",
  } },
  { id: "24:122", value: "400 cases", props: {
    state: "gate", title: "Retention gate", tone: "amber",
    value: "400 cases", reason: "−0.5 pp · Lower bound −3.98 pp · Required lower bound greater than −1.00 pp · Inconclusive",
    unit: "pp", numerator: "Not reported", denominator: "400 cases",
    observationWindow: "Not reported", evidence: "Inconclusive", direction: "Higher is better",
  } },
  { id: "24:129", value: "Evaluation completed", props: {
    state: "summary", title: "Release overview", tone: "amber",
    value: "Evaluation completed", reason: "Candidate on hold · Incumbent active · 0% candidate traffic",
    unit: "% candidate traffic", numerator: "Not reported", denominator: "Not reported",
    observationWindow: "Not reported", evidence: "Release summary", direction: "No favorable direction",
  } },
  { id: "24:136", value: "Switching…", props: {
    state: "switching", title: "Workspace overview", tone: "blue",
    reason: "Loading the selected context. Previous-context data is hidden.",
  } },
  { id: "24:163", value: "Comparison unavailable", props: {
    state: "incompatible", title: "Comparison", tone: "lilac",
    reason: "Population definitions differ",
    unit: "pp", numerator: "Not reported", denominator: "Not reported",
    observationWindow: "Nov 1–7 / Oct 1–7", evidence: "Incompatible populations", direction: "No favorable direction",
  } },
] satisfies { id: string; value: string; props: MetricSemanticsProps }[];

let script: string;
let styles: string;
test.beforeAll(async () => {
  // Bundle the real component and its pinned local font without starting a server.
  const rowsForSource = sourceFixtures.map(row => row.props);
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
        const fixtures = document.querySelector('main').dataset.gallery === 'source'
          ? ${JSON.stringify(rowsForSource)} : ${JSON.stringify(fixtures)};
        createRoot(document.querySelector('main')).render(
          <>{fixtures.map((props,i) => <MetricSemantics key={i} {...props} />)}</>
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

async function openGallery(page: Page, width: number, scale: number, gallery: 'core' | 'source') {
  await page.setViewportSize({ width, height: 900 });
  await page.setContent('<html lang="en"><main data-gallery="' + gallery + '"></main></html>');
  await page.addStyleTag({ content: styles + `
    html {font-size:${scale * 100}%} body {margin:0}
    main {display:grid;gap:var(--ot-space-16);max-width:500px}
  ` });
  await page.addScriptTag({ content: script });
}

async function renderedLines(page: Page) {
  return page.locator('.ot-metric').evaluateAll(items => items.map(card => {
    const fields = Array.from(card.children);
    if (card.nextElementSibling) fields.push(card.nextElementSibling);
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
}

for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [1, 2]) {
    test(`metric semantics at ${width}px and ${scale * 100}% text`, async ({ page }, info) => {
      await openGallery(page, width, scale, 'core');
      const cards = page.locator('.ot-metric');
      await expect(cards).toHaveCount(fixtures.length);
      await page.evaluate(() => document.fonts.ready);
      // Native fixture guide 296:259. Each pair is [Metric height, metadata height].
      const expectedHeights = scale === 1 ? Array.from({ length: 5 }, () => [122, 90]) :
        width === 320 ? [[196, 252], [308, 252], [384, 252], [232, 216], [232, 216]] :
          [[196, 180], [196, 216], [272, 180], [232, 216], [196, 180]];
      const lines = await renderedLines(page);
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

    test(`source metric fixtures at ${width}px and ${scale * 100}% text`, async ({ page }, info) => {
      await openGallery(page, width, scale, 'source');
      const cards = page.locator('.ot-metric');
      await expect(cards).toHaveCount(sourceFixtures.length);
      await page.evaluate(() => document.fonts.ready);
      for (const [i, row] of sourceFixtures.entries()) {
        const card = cards.nth(i);
        await expect(card.getByRole('definition').first()).toHaveText(row.value);
        await expect(card.getByRole('definition').last()).toHaveText(row.props.reason);
        if (row.props.state === 'revoked' || row.props.state === 'switching') {
          await expect(card.locator('..')).not.toContainText(/300|org_a|Not reported/);
        }
      }
      const lines = await renderedLines(page);
      const heights = await cards.evaluateAll(items => items.map(card => [
        card.getBoundingClientRect().height, card.nextElementSibling?.getBoundingClientRect().height ?? 0,
      ]));
      const canvasHeight = await page.locator('main').evaluate(main => main.getBoundingClientRect().height);
      await writeFile(info.outputPath('lines.json'), JSON.stringify(lines));
      // The guide binds these digests to 17 native cards, every text line, and both
      // card/context heights. Retain exact literal copy and preserve independently testable line breaks.
      const sourceHashes = {
        'normalNarrow': '2919bce4c0bc86c495c229a7821abe0b29d953a8268cbbfc0311095f24b25d95',
        'normalWide': 'a0d441137f6ebdb7dc4dd62734ab37e36fdaa57f44070e32d3df59d8c97f04c5',
        'largeNarrow': 'e528906ef275f95da25368d968f7ece297f5505964185438fff4c59dfaf828f1',
        'largeWide': '4dd798b7d8568c1dbd3fa3a72e9edb0ecb21051612bf74fc5688abc3c1cea65d'
      };
      const profile = scale === 1 ? (width === 320 ? 'normalNarrow' : 'normalWide') :
        (width === 320 ? 'largeNarrow' : 'largeWide');
      const evidence = JSON.stringify([lines, heights, canvasHeight]);
      expect(createHash('sha256').update(evidence).digest('hex')).toBe(sourceHashes[profile]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
      const image = await page.screenshot({ fullPage: true, path: info.outputPath('source.png') });
      await info.attach('original-source-fixtures', { body: image, contentType: 'image/png' });
    });
  }
}
