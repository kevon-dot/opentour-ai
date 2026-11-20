import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import { expectRecordRows, renderEvidencePage } from '../../../tests/fixtures/presentation/evidence-page';
import type { ComparatorFixture } from './comparator-evidence.fixtures';

let bundle: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  bundle = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/comparator-evidence.tsx'), 'iife');
});
for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`comparator evidence at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await renderEvidencePage(page, bundle, width, scale);
      const main = page.getByRole('main');
      const stacked = width === 320 || (width === 768 && scale === 200);
      const columns = ['Alternative', 'Errors', 'Cost / case', 'Latency p95', 'Exclusions'];
      const expected = [
        ['Current baseline', '0 / 200', 'USD 0.030', '1,200 ms', '0'],
        ['Retrieval repair', '0 / 200', 'USD 0.020', '900 ms', '0'],
        ['Policy repair', '0 / 200', 'USD 0.010', '800 ms', '0'],
      ];
      const choose = async (value: ComparatorFixture) => page.evaluate(value => window.showComparatorFixture(value), value);
      const capture = async (state: ComparatorFixture) => {
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        await page.screenshot({ path: info.outputPath(`comparator-${state}-${width}-${scale}.png`), fullPage: true });
      };
      await expectRecordRows(page, 'Current evidence', columns, expected, stacked);
      await expectRecordRows(page, 'Evidence parity', ['Alternative', 'Same population', 'Same evidence', 'Exclusions'],
        expected.map(row => [row[0], 'Yes', 'Yes', '0']), stacked);
      await expectRecordRows(page, 'Alternative effort', ['Alternative', 'Effort', 'Cost', 'Latency'],
        expected.map((row, i) => [row[0], ['Current operation', 'Configuration change', 'Policy review'][i], row[2] + ' / case', row[3] + ' p95']), stacked);
      await expectRecordRows(page, 'Training recommendation', ['Result', 'Meaning', 'Next step'],
        [['Do not train', 'No demonstrated training advantage', 'Prepare exact repair']], stacked);
      await expect(main.getByText('Quality results are tied', { exact: true })).toBeVisible();
      await expect(main.getByText('Matched', { exact: true })).toBeVisible();
      await capture('tied');

      await choose('mismatched');
      await expect(main.getByText('Mismatched (blocking)', { exact: true })).toBeVisible();
      await expectRecordRows(page, 'Current evidence', columns, [expected[0], expected[1], ['Policy repair', '2 / 180', 'USD 0.010', '800 ms', '20']], stacked);
      await expect(main.getByText('180 of 200 cases evaluated')).toBeVisible();
      await capture('mismatched');
      await choose('unknown');
      await expect(main.getByText('Unknown', { exact: true }).first()).toBeVisible();
      await expectRecordRows(page, 'Current evidence', columns, [expected[0],
        ['Retrieval repair', 'Unavailable — Result not reported / 200', 'Unavailable — Cost observation missing', 'Unavailable — Timing sample missing', '0']], stacked);
      await expectRecordRows(page, 'Training recommendation', ['Result', 'Meaning', 'Next step'],
        [['Defer', 'Evidence or declared tie resolution incomplete', 'Collect evidence']], stacked);
      await capture('unknown');
      await choose('train');
      await expectRecordRows(page, 'Training recommendation', ['Result', 'Meaning', 'Next step'],
        [['Train', 'Declared evidence supports training', 'Review bounded training plan']], stacked);
      await expect(main.getByRole('button')).toHaveCount(0);
      await page.keyboard.press('Tab');
      expect(await main.locator(':focus').count()).toBe(0);
      await capture('train');
    });
  }
}
