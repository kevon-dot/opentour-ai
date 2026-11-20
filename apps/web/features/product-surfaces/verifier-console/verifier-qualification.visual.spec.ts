import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import { expectRecordRows, renderEvidencePage } from '../../../tests/fixtures/presentation/evidence-page';
import type { VerifierFixture } from './verifier-qualification.fixtures';

let bundle: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  bundle = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/verifier-qualification.tsx'), 'iife');
});
for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`verifier slices and revision impact at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await renderEvidencePage(page, bundle, width, scale);
      const main = page.getByRole('main');
      const stacked = width === 320 || (width === 768 && scale === 200);
      const exact = [
        ['Known-answer audit', 'Passed', 'Current exact fixture manifest'],
        ['Critical-risk slice', 'Unresolved', 'Insufficient current evidence'],
        ['Protected partition', 'Not evaluated', 'Separate grant required'],
      ];
      const capture = async (state: VerifierFixture) => {
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        await page.screenshot({ path: info.outputPath(`verifier-${state}-${width}-${scale}.png`), fullPage: true });
      };
      const choose = async (state: VerifierFixture) => page.evaluate(value => window.showVerifierFixture(value), state);
      await expectRecordRows(page, 'Verifier qualification', ['Slice', 'Result', 'Reason'], exact, stacked);
      await expectRecordRows(page, 'Requalification impact', ['Resource', 'State', 'Next step'], [
        ['Verifier revision 4', 'Current', 'Inspect qualification'],
        ['Labels from revision 3', 'Historical', 'Do not silently relabel'],
        ['Affected derived datasets', 'Unknown', 'Inspect exact impact result'],
      ], stacked);
      await capture('exact');
      await choose('measured');
      await expectRecordRows(page, 'Verifier qualification', ['Slice', 'Result', 'Reason'], [
        exact[0], exact[1], ['Generated comparison A', 'Failed', 'Qualification operation failed'],
      ], stacked);
      await expectRecordRows(page, 'Comparison evidence', ['Slice', 'Measure', 'Value'], [
        ['Known-answer audit', 'Samples', '200'], ['Known-answer audit', 'Agreement', '197 / 200'],
        ['Known-answer audit', 'Disagreement', '3 / 200'], ['Critical-risk slice', 'Samples', '0'],
        ['Generated comparison A', 'Samples', '731'], ['Generated comparison A', 'Agreement', '731 / 731'],
        ['Generated comparison A', 'Disagreement', '0 / 731'],
      ], stacked);
      await capture('measured');
      await choose('exact');
      for (const content of [await main.innerHTML(), await main.ariaSnapshot()]) {
        for (const removed of ['Generated comparison A', '731', '197 / 200', 'Comparison evidence']) expect(content).not.toContain(removed);
      }
      await choose('replacement');
      await expect(main.getByRole('heading', { name: 'Verifier revision 5' })).toBeVisible();
      await expectRecordRows(page, 'Requalification impact', ['Resource', 'State', 'Next step'], [
        ['Labels from revision 3', 'Historical', 'Do not silently relabel'],
        ['Labels from revision 4', 'Historical', 'Inspect revision 4 evidence'],
      ], stacked);
      await page.keyboard.press('Tab');
      expect(await main.locator(':focus').count()).toBe(0);
      await capture('replacement');
    });
  }
}
