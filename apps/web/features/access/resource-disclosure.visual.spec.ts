import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { buildInlineFixture } from '../../tests/fixtures/presentation/build.ts';

let fixture: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  fixture = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/resource-disclosure.tsx'), 'iife');
});

const reason = 'Your role does not include the evaluation capability.';
const protectedText = ['Receipt-status workflow', '300', 'Start evaluation', 'Eligible cases',
  'Fixture population receipts-nov-01-07', 'Refresh view', 'Benchmark evidence', reason];

for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`resource disclosure at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.setContent('<!doctype html><html lang="en"><title>Workspace</title><body><div id="root"></div></body></html>');
      const inset = width === 320 ? 16 : width === 768 ? 24 : 32;
      await page.addStyleTag({ content: fixture.css + `
        html {font-size:${scale}%} body {margin:0;background:var(--ot-surface-canvas)} main {padding:${inset}px}
        .fixture-workflow {margin:0;color:var(--ot-text-secondary);font:var(--ot-type-body-size)/var(--ot-type-body-line) Geist,sans-serif}
        .fixture-evidence h2 {margin:0 0 8px;font:var(--ot-type-label-weight) var(--ot-type-label-size)/var(--ot-type-label-line) Geist,sans-serif}
        .fixture-evidence table {inline-size:100%;table-layout:fixed;border-collapse:collapse}
        main .ot-metric {inline-size:min(500px, 100%)}
      ` });
      await page.evaluate(fixture.script);
      const main = page.getByRole('main');
      const back = main.getByRole('button', { name: 'Return to workspace' });
      const heading = main.getByRole('heading', { name: 'Resource not found' });
      const capture = async (state: string) => {
        await page.evaluate(() => document.fonts.ready);
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
        const stem = `disclosure-${state}-${width}-${scale}`;
        await page.screenshot({ path: info.outputPath(stem + '.png'), fullPage: true, animations: 'disabled' });
        await writeFile(info.outputPath(stem + '.json'), JSON.stringify({ state, aria: await main.ariaSnapshot() }));
      };
      const checkUndisclosed = async () => {
        await expect(heading).toBeVisible();
        await expect(main.getByRole('status')).toContainText('This resource is unavailable in the current context.');
        await expect(back).toBeVisible();
        await expect(heading).toHaveCSS('font-size', scale === 200 ? '48px' : '24px');
        await expect(main.locator('button')).toHaveCount(1);
        await expect(page.locator('[role="tooltip"], [title], [aria-describedby]')).toHaveCount(0);
        for (const surface of [await page.locator('body').innerHTML(), await page.locator('body').ariaSnapshot()]) {
          for (const sentinel of protectedText) expect(surface).not.toContain(sentinel);
        }
      };
      await page.evaluate(() => window.showDisclosure('undisclosed'));
      await checkUndisclosed();
      await capture('undisclosed');
      await page.evaluate(() => window.showDisclosure('known'));
      const action = main.getByRole('button', { name: 'Start evaluation' });
      const help = main.getByRole('button', { name: 'Refresh view' });
      await expect(main.getByText('Receipt-status workflow · Revision 8')).toBeVisible();
      await expect(main.getByText('300', { exact: true })).toHaveCount(2);
      await expect(main.locator('.ot-metric')).toHaveCSS('width', Math.min(500, width - 2 * inset) + 'px');
      await expect(action).toHaveAccessibleDescription(reason);
      await expect(main.getByText(reason)).toBeVisible();
      await expect(action).toHaveAttribute('aria-disabled', 'true');
      await expect(action).toHaveCSS('width', Math.min(280, width - 2 * inset) + 'px');
      await expect(action).toHaveCSS('background-color', 'rgb(237, 240, 245)');
      await capture('known');
      await page.keyboard.press('Tab');
      await expect(help).toBeFocused();
      await page.keyboard.press('Tab');
      await expect(action).toBeFocused();
      await expect(action).toHaveCSS('outline-style', 'solid');
      await capture('known-focus');
      await page.keyboard.press('Enter');
      await page.keyboard.press('Space');
      await action.scrollIntoViewIfNeeded();
      const point = await action.boundingBox();
      expect(point).not.toBeNull();
      await page.mouse.click(point!.x + point!.width / 2, point!.y + point!.height / 2);
      expect(await page.evaluate(() => window.disclosureEvents.activations)).toBe(0);
      await page.keyboard.press('Shift+Tab');
      await expect(help).toBeFocused();
      await expect(page.getByRole('tooltip')).toHaveText('Fixture population receipts-nov-01-07');
      await page.evaluate(() => window.showDisclosure('undisclosed'));
      await checkUndisclosed();
      await expect(heading).toBeFocused();
      await page.keyboard.press('Tab');
      await expect(back).toBeFocused();
      await capture('after-switch');
      await page.keyboard.press('Enter');
      expect(await page.evaluate(() => window.disclosureEvents)).toEqual({ activations: 0, returns: 1 });
    });
  }
}
