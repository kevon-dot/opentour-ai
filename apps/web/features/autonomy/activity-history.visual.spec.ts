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
    await expect(load).toHaveCSS('min-block-size', '44px');
    expect((await load.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    const first = rows.nth(1).getByRole('button');
    await page.keyboard.press('Tab');
    await expect(first).toBeFocused();
    await page.keyboard.press('Space');
    await expect(first).toHaveAttribute('aria-expanded', 'true');
    let detail = main.getByRole('region', { name: 'Evidence reconstruction advanced' });
    await expect(detail).toContainText('16 Nov 2026, 09:42:30 UTC');
    await expect(detail).toContainText('Reason: EXACT_JOIN_PROGRESS');
    const actions = main.getByRole('group', { name: 'Activity actions' });
    const receipt = actions.getByRole('link', { name: 'View receipt', exact: true });
    await expect(detail.getByRole('link')).toHaveCount(0);
    await expect(receipt).toHaveAttribute('href', '/receipts/reconstruct-015');
    await expect(receipt).toHaveCSS('background-color', 'rgb(36, 91, 238)');
    await expect(receipt).toHaveCSS('color', 'rgb(255, 255, 255)');
    await expect(receipt).toHaveCSS('min-block-size', '44px');
    const panelBox = (await detail.boundingBox())!;
    const receiptBox = (await receipt.boundingBox())!;
    const loadBox = (await load.boundingBox())!;
    expect(receiptBox.y).toBeCloseTo(panelBox.y + panelBox.height + 16, 1);
    expect(receiptBox.height).toBeGreaterThanOrEqual(44);
    if (width === 320) {
      expect(receiptBox.width).toBe(288);
      expect(loadBox.x).toBe(receiptBox.x);
      expect(loadBox.y).toBeCloseTo(receiptBox.y + receiptBox.height + 12, 1);
    } else {
      expect(receiptBox.width).toBe(280);
      expect(loadBox.x).toBeCloseTo(receiptBox.x + receiptBox.width + 12, 1);
      expect(loadBox.y).toBe(receiptBox.y);
    }
    expect(loadBox.width).toBe(receiptBox.width);
    // Reach the receipt after the four event disclosures, then move directly to Load older activity.
    for (let i = 0; i < 4; i++) await page.keyboard.press('Tab');
    await expect(receipt).toBeFocused();
    await expect(receipt).toHaveCSS('outline-style', 'solid');
    await capture('expanded-focus');
    await page.keyboard.press('Tab');
    await expect(load).toBeFocused();
    await rows.nth(2).getByRole('button').click();
    await expect(first).toHaveAttribute('aria-expanded', 'false');
    detail = main.getByRole('region', { name: 'Label verification advanced' });
    await expect(detail).toContainText('Operation: labels-015');
    await expect(detail).toContainText('Source receipt: labels-015');
    await expect(receipt).toHaveAttribute('href', '/receipts/labels-015');
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
    await rows.nth(1).getByRole('button').click();
    await expect(receipt).toHaveAttribute('href', '/receipts/current-receipt');
    await receipt.focus();
    await show('read-only');
    await expect(load).toBeFocused();
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
