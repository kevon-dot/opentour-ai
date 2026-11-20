import { expect, test } from '@playwright/test';
import { buildEvidenceFixture, captureEvidence, mountEvidenceFixture } from '../../tests/fixtures/presentation/evidence-capture.ts';
import type { ActivityFixture } from './activity-history.fixtures';

let fixture: Awaited<ReturnType<typeof buildEvidenceFixture>>;
test.beforeAll(async () => { fixture = await buildEvidenceFixture('activity-history'); });

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`activity history at ${width}px and ${scale}% text`, async ({ page }, info) => {
    await mountEvidenceFixture(page, fixture, width, scale);
    const main = page.getByRole('main');
    const show = (state: ActivityFixture) => page.evaluate(value => window.showActivityHistory(value), state);
    const capture = (state: string) => captureEvidence(page, info, `activity-${state}-${width}-${scale}`);
    const table = main.getByRole('table', { name: 'Activity history' });
    const rows = table.getByRole('row');
    const load = main.getByRole('button', { name: 'Load older activity' });
    await show('reference');
    await expect(rows).toHaveCount(5);
    const first = rows.nth(1).getByRole('button');
    await page.keyboard.press('Tab');
    await expect(first).toBeFocused();
    await page.keyboard.press('Space');
    await expect(first).toHaveAttribute('aria-expanded', 'true');
    let detail = main.getByRole('region', { name: 'Evidence reconstruction advanced' });
    await expect(detail).toContainText('16 Nov 2026, 09:42:30 UTC');
    await expect(detail).toContainText('Reason: EXACT_JOIN_PROGRESS');
    await expect(detail.getByRole('link', { name: 'View receipt' })).toHaveAttribute('href', '/receipts/reconstruct-015');
    await capture('expanded-focus');
    await rows.nth(2).getByRole('button').click();
    await expect(first).toHaveAttribute('aria-expanded', 'false');
    detail = main.getByRole('region', { name: 'Label verification advanced' });
    await expect(detail).toContainText('Operation: labels-015');
    await expect(detail).toContainText('Source receipt: labels-015');
    await load.focus();
    await page.keyboard.press('Enter');
    expect(await page.evaluate(() => window.olderActivityCalls)).toBe(1);
    await expect(rows).toHaveCount(5);
    await show('appended');
    await expect(rows).toHaveCount(6);
    await expect(rows.nth(4)).toContainText('Prior candidate held');
    const live = main.getByRole('status');
    await expect(live).toHaveText('1 older event loaded');
    const announcement = await live.getByText('1 older event loaded').elementHandle();
    await expect(load).toBeFocused();
    await capture('appended');
    await show('exhausted');
    await expect(rows).toHaveCount(7);
    await expect(live).toContainText('You have reached the beginning.');
    await expect(live).toContainText('1 older event loaded');
    expect(await announcement!.evaluate(element => element.isConnected)).toBe(false);
    await expect(load).toBeFocused();
    await page.keyboard.press('Space');
    expect(await page.evaluate(() => window.olderActivityCalls)).toBe(1);
    await capture('exhausted');
    await show('replacement');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(1)).toContainText('New snapshot event');
    await expect(live).toHaveText('Activity refreshed; prior snapshot expired');
    await expect(main.getByRole('link')).toHaveCount(0);
    await capture('replacement');
    await show('read-only');
    await expect(main.getByText('Read-only access')).toBeVisible();
    await rows.nth(1).getByRole('button').click();
    await expect(main.getByText('Reason: Visible summary only')).toBeVisible();
    for (const surface of [await main.innerHTML(), await main.ariaSnapshot()]) {
      expect(surface).not.toMatch(/labels-015|reconstruct-015|current-receipt|EXACT_JOIN_PROGRESS|View receipt|System/);
    }
    await expect(main.getByRole('link')).toHaveCount(0);
    await capture('read-only');
  });
}

test('forced-colors keyboard focus and the actual count for overlapping older events', async ({ page }, info) => {
  await page.emulateMedia({ forcedColors: 'active' });
  await mountEvidenceFixture(page, fixture, 320, 200);
  await page.evaluate(() => window.showActivityHistory('reference'));
  const load = page.getByRole('button', { name: 'Load older activity' });
  await expect(load).toBeVisible();
  await page.keyboard.press('Tab');
  for (let i = 0; i < 4; i++) await page.keyboard.press('Tab');
  await expect(load).toBeFocused();
  await page.keyboard.press('Enter');
  await page.evaluate(() => window.showActivityHistory('exhausted'));
  await expect(page.getByRole('status')).toContainText('2 older events loaded');
  await expect(load).toBeFocused();
  await captureEvidence(page, info, 'activity-forced-focus');
});
