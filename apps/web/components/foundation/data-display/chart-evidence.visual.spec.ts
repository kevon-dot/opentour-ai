import { test, expect } from '@playwright/test';
import { join } from 'node:path';
import { build } from 'vite';
import type { EvidenceCoverageProps, EvidenceStatementTone, RateComparisonProps, SignedBarsProps } from './chart-evidence.types';

const coverage: EvidenceCoverageProps = {
  title: 'Evidence coverage', denominator: 1600, note: 'Sample window · Nov 1–7 · observed 09:42 UTC',
  segments: [
    { key: 'verified', label: 'Verified', series: 'mint', value: { status: 'observed', count: 1284 } },
    { key: 'awaiting', label: 'Awaiting evidence', series: 'amber', value: { status: 'observed', count: 196 } },
    { key: 'excluded', label: 'Excluded', series: 'coral', value: { status: 'observed', count: 120 } },
  ],
};
const rate: RateComparisonProps = {
  title: 'Material errors fell by half', subtitle: 'Same 200 episodes · lower is better · simulated fixture A',
  rows: [
    { key: 'repair', label: 'Best locked repair', series: 'lilac', value: { status: 'observed', numerator: 60, denominator: 200, rate: 0.3 } },
    { key: 'r1', label: 'Candidate r1', series: 'mint', value: { status: 'observed', numerator: 30, denominator: 200, rate: 0.15 } },
  ], rateFractionDigits: 0,
  method: 'Paired episodes · frozen two-sided 95% exact-marginal interval with Bonferroni coverage. Retention uses a frozen one-sided bound. No method switch after observing results.',
};
const signed: SignedBarsProps = {
  label: 'Signed amounts', unit: { currency: 'USD', fractionDigits: 0 }, domain: { min: -900, max: 1800 },
  rows: [-900, 300, 1800].map(value => ({ key: String(value), value: { status: 'observed', value } })),
};

const notices: [EvidenceStatementTone, string, string][] = [
    ['success', '0 / 300', '0.0% observed · 95% upper bound 0.994% · Limit 1.000% · Passed'],
    ['hold', '0 / 240', '95% upper bound 1.240% · Limit 1.000% · Inconclusive'],
    ['info', '60 / 200', '30 / 200 · 15 pp · 50% relative error reduction'],
    ['hold', '180 of 200 pairs scored', '20 candidate results missing · Incomplete'],
    ['info', 'A 50', 'B 30 · Other 20 · View all categories'],
    ['hold', '400 cases', '−0.5 pp · Lower bound −3.98 pp · Required lower bound greater than −1.00 pp · Inconclusive'],
  ];

let script: string;
let styles: string;
test.beforeAll(async () => {
  // Bundle the real component and its pinned local font without starting a server.
  const root = join(process.cwd(), 'apps/web');
  const entry = join(root, 'chart-browser-fixture.tsx');
  const result = await build({
    root, configFile: false, envFile: false, publicDir: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
    plugins: [{
      name: 'chart-fixture', resolveId: id => id === entry ? entry : null,
      load: id => id === entry ? `
        import { createRoot } from 'react-dom/client';
        import { ChartEvidenceNotice, EvidenceCoverage, RateComparison, SignedBars } from './components/foundation/data-display/chart-evidence';
        const boards = ${JSON.stringify(notices)};
        createRoot(document.querySelector('main')).render(<>
          <EvidenceCoverage {...${JSON.stringify(coverage)}} />
          <RateComparison {...${JSON.stringify(rate)}} />
          <SignedBars {...${JSON.stringify(signed)}} />
          {boards.map(([tone, headline, body]) => <ChartEvidenceNotice key={headline} {...{tone,headline,details:[body]}} />)}
        </>        );` : null,
    }],
    build: { write: false, lib: { entry, name: 'ChartFixture', formats: ['iife'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing chart fixture bundle');
  const js = bundle.output.find(item => item.type === 'chunk');
  const css = bundle.output.find(item => item.type === 'asset' && item.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing chart fixture assets');
  script = js.code;
  styles = String(css.source);
});

for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`chart evidence at ${width}px, ${scale}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.setContent('<html lang="en"><main></main></html>');
      await page.addStyleTag({ content: styles + `
        html { font-size: ${scale}% } body { margin: 0 }
        main { display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr) }
        .ot-chart-coverage { max-width: 390px } .ot-chart-rate { max-width: 758px }
        .ot-chart-signed, .ot-notice { max-width: 512px }
      ` });
      await page.addScriptTag({ content: script });
      await expect(page.getByRole('figure')).toHaveCount(2);
      await page.evaluate(() => document.fonts.ready);
      for (const [, headline, body] of notices) {
        const notice = page.getByRole('status').filter({ has: page.getByText(headline, { exact: true }) });
        await expect(notice).toContainText(body);
      }
      const measurements = await page.evaluate(() => {
        const box = (selector: string) => [...document.querySelectorAll(selector)].map(n => n.getBoundingClientRect().width);
        return {
          overflow: document.documentElement.scrollWidth - window.innerWidth,
          segment: box('.ot-chart-coverage-segment'), track: box('.ot-chart-coverage-bar')[0],
          rates: box('.ot-chart-rate-fill'), rateTracks: box('.ot-chart-rate-track'),
          negative: box('.ot-chart-signed-bar--negative'), positive: box('.ot-chart-signed-bar--positive'),
          row: box('.ot-chart-signed-row')[0],
        };
      });
      expect(measurements.overflow).toBe(0);
      const proportions = [1284, 196, 120].map(n => n / 1600);
      for (const [i, proportion] of proportions.entries()) {
        expect(measurements.segment[i]).toBeCloseTo((measurements.track - 4) * proportion, 0);
      }
      for (const [i, fraction] of [0.30, 0.15].entries()) {
        expect(measurements.rates[i]).toBeCloseTo(measurements.rateTracks[i] * fraction, 0);
      }
      expect(measurements.negative[0]).toBeCloseTo(measurements.row / 3, 0);
      expect(measurements.positive[0]).toBeCloseTo(measurements.row / 9, 0);
      expect(measurements.positive[1]).toBeCloseTo(2 * measurements.row / 3, 0);
      await info.attach(`charts-${width}-${scale}.png`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    });
  }
}
