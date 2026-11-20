import { expect, test } from '@playwright/test';
import { buildEvidenceFixture, captureEvidence, mountEvidenceFixture } from '../../tests/fixtures/presentation/evidence-capture.ts';
import type { EvidenceFixture } from './offline-evidence.fixtures';

let fixture: Awaited<ReturnType<typeof buildEvidenceFixture>>;
test.beforeAll(async () => { fixture = await buildEvidenceFixture('offline-evidence'); });

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`local evidence at ${width}px and ${scale}% text`, async ({ page }, info) => {
    await mountEvidenceFixture(page, fixture, width, scale);
    const main = page.getByRole('main');
    const show = (state: EvidenceFixture) => page.evaluate(value => window.showOfflineEvidence(value), state);
    const capture = (state: string) => captureEvidence(page, info, `local-evidence-${state}-${width}-${scale}`);
    const retry = main.getByRole('button', { name: 'Retry local evidence' });
    const assertHosted = async () => {
      await expect(main.getByText('300', { exact: true })).toBeVisible();
      await expect(main.getByText('b7 · Sealed')).toBeVisible();
      await expect(main.getByText('Hosted metadata last observed Nov 12, 09:42 UTC')).toBeVisible();
      await expect(main.getByText('Observed · Nov 1–7 · Population receipts-nov-01-07')).toBeVisible();
    };
    await show('available');
    await expect(main.getByText('Synthetic local receipt A')).toBeVisible();
    await assertHosted();
    await capture('available');
    await show('offline');
    await expect(main.getByText('Private evidence unavailable')).toBeVisible();
    await assertHosted();
    for (const surface of [await main.innerHTML(), await main.ariaSnapshot()]) expect(surface).not.toMatch(/Synthetic local receipt|Prior scoped observation/);
    await capture('offline');
    const offline = await main.innerHTML();
    await page.keyboard.press('Tab');
    await expect(retry).toBeFocused();
    await capture('retry-focus');
    await page.keyboard.press('Enter');
    expect(await page.evaluate(() => window.localRetryCalls)).toBe(1);
    await retry.click();
    expect(await page.evaluate(() => window.localRetryCalls)).toBe(2);
    expect(await main.innerHTML()).toBe(offline);
    await show('recovered');
    await expect(main.getByText('Synthetic local receipt B')).toBeVisible();
    await expect(retry).toHaveCount(0);
    await assertHosted();
    await capture('recovered');
    await show('local-restricted');
    await expect(main.getByText('Access unavailable')).toBeVisible();
    await assertHosted();
    for (const surface of [await main.innerHTML(), await main.ariaSnapshot()]) expect(surface).not.toMatch(/Synthetic local receipt|New scoped observation/);
    await show('missing');
    await expect(main.getByText('Observation time unavailable')).toBeVisible();
    await expect(main.getByText('Unavailable', { exact: true })).toHaveCount(2);
    await capture('missing');
    await show('restricted');
    await expect(main.getByText('Access unavailable')).toBeVisible();
    for (const surface of [await main.innerHTML(), await main.ariaSnapshot()]) expect(surface).not.toMatch(/300|Sealed|Nov 12|receipts-nov|Synthetic local receipt|Retry local evidence/);
    await capture('restricted');
  });
}

test('offline retry focus in forced colors', async ({ page }, info) => {
  await page.emulateMedia({ forcedColors: 'active' });
  await mountEvidenceFixture(page, fixture, 320, 200);
  await page.evaluate(() => window.showOfflineEvidence('offline'));
  const retry = page.getByRole('button', { name: 'Retry local evidence' });
  await expect(retry).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(retry).toBeFocused();
  await captureEvidence(page, info, 'local-evidence-forced-focus');
});
