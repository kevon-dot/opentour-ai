import { test, expect } from '@playwright/test';
test('actual suite directory selects its descendant', async ({ page }) => {
  await page.goto('/__harness/ready');
  await expect(page.locator('body')).toContainText('simulated-browser-harness');
});
