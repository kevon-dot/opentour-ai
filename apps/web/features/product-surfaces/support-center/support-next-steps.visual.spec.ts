import { expect, test, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import type { SupportFixture } from './support-next-steps.fixtures';

let bundle: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  bundle = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/support-next-steps.tsx'), 'iife');
});
async function open(page: Page, width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.setContent('<!doctype html><html lang="en"><title>Support detail</title><main></main></html>');
  await page.addStyleTag({ content: bundle.css + `html {font-size:${scale}%} body {margin:0; background:var(--ot-surface-canvas)} main {max-width:1216px; margin:auto; padding:${width === 320 ? 16 : width === 768 ? 24 : 32}px}` });
  await page.addScriptTag({ content: bundle.script });
  await expect(page.getByRole('heading', { name: 'Support and incident' })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}
async function show(page: Page, name: SupportFixture) {
  await page.evaluate(name => window.showSupportFixture(name), name);
}
const case41 = [
  ['Support case 41', 'Open', 'Await owner assignment'],
  ['Related incident', 'Current incident state', 'Follow operational next step'],
  ['Additional grant', 'No current permission', 'Customer reviews an explicit current request'],
];
for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`support projections at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await open(page, width, scale);
      const region = page.getByRole('region', { name: 'Support and incident' });
      const table = region.getByRole('table');
      const labels = ['Resource', 'State', 'Next step'];
      if (width < 480 * scale / 100) {
        await expect(table).toHaveCount(0);
        const list = region.getByRole('list', { name: 'Support and incident' });
        await expect(list.getByRole('listitem')).toHaveCount(3);
        for (const [index, values] of case41.entries()) {
          const row = list.getByRole('listitem').nth(index);
          await expect(row.getByRole('term')).toHaveText(labels.map(label => label + ':'));
          await expect(row.getByRole('definition')).toHaveText(values);
        }
      } else {
        await expect(table).toHaveAccessibleName('Support and incident');
        await expect(table.getByRole('columnheader')).toHaveText(labels);
        await expect(table.getByRole('rowheader')).toHaveText(case41.map(row => row[0]));
        await expect(table.getByRole('cell')).toHaveText(case41.flatMap(row => row.slice(1)));
      }
      for (const state of ['case41', 'closed', 'long', 'restricted'] as const) {
        await show(page, state);
        if (state === 'closed') {
          for (const value of ['Closed', 'Investigating', 'Expired', 'Access removal pending']) {
            expect(await region.ariaSnapshot()).toContain(value);
          }
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        await page.screenshot({ path: info.outputPath(`support-${state}-${width}-${scale}.png`), fullPage: true });
      }
    });
  }
}
test('scope changes replace visible, hidden and accessible data and never create keyboard actions', async ({ page }, info) => {
  await open(page, 320, 200);
  await show(page, 'previous');
  expect(await page.locator('main').ariaSnapshot()).toContain('fixture-private-owner');
  await show(page, 'restricted');
  expect(await page.locator('main').innerHTML()).not.toContain('fixture-private');
  expect(await page.locator('main').ariaSnapshot()).not.toContain('fixture-private');
  await expect(page.getByText('Additional grant', { exact: true }).filter({ visible: true })).toHaveCount(1);
  await page.keyboard.press('Tab');
  expect(await page.locator('main :focus').count()).toBe(0);
  await page.emulateMedia({ forcedColors: 'active' });
  await page.screenshot({ path: info.outputPath('support-forced-colors.png'), fullPage: true });
  await show(page, 'cleared');
  await expect(page.locator('main')).toBeEmpty();
});
