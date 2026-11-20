import { expect, test, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../tests/fixtures/presentation/build.ts';
import type { PublishedTerms } from './terms-acknowledgment';

const initial: PublishedTerms = {
  revision: '2026-11-01', effective_at: '2026-11-01T00:00:00Z', document_url: 'https://terms.example/november',
};
const next: PublishedTerms = {
  revision: 'r2-current', effective_at: '2026-12-11T00:00:00Z', document_url: 'https://terms.example/december',
};
let bundle: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  bundle = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/terms-acknowledgment.tsx'), 'iife');
});
async function open(page: Page, width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.setContent('<!doctype html><html lang="en"><title>Organization terms</title><div id="root"></div></html>');
  await page.addStyleTag({ content: bundle.css + `html {font-size:${scale}%}` });
  await page.addScriptTag({ content: bundle.script });
  await expect(page.getByRole('checkbox')).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}
async function supply(page: Page, terms: PublishedTerms | null) {
  await page.evaluate(detail => window.dispatchEvent(new CustomEvent('supply-terms', { detail })), terms);
}
const checkbox = (page: Page) => page.getByRole('checkbox', { name: 'I agree to the published terms', exact: true });
const create = (page: Page) => page.getByRole('button', { name: 'Create organization', exact: true });
for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`revision-bound acknowledgment at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await open(page, width, scale);
      const capture = async (state: string) => {
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        await page.screenshot({ path: info.outputPath(`terms-${state}-${width}-${scale}.png`), fullPage: true });
      };
      const link = page.getByRole('link', { name: 'Read published terms · Opens in a new tab', exact: true });
      await expect(link).toHaveAttribute('target', '_blank');
      await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
      await expect(link).toHaveAttribute('href', initial.document_url);
      await expect(checkbox(page)).not.toBeChecked();
      await expect(checkbox(page)).toHaveAccessibleDescription('Required · Published revision 2026-11-01');
      await expect(page.locator('time')).toHaveText('1 Nov 2026');
      await expect(create(page)).toBeDisabled();
      await capture('unchecked');
      await checkbox(page).check();
      await expect(create(page)).toBeEnabled();
      await capture('checked');

      await supply(page, next);
      await expect(page.getByRole('status')).toContainText('Published terms changed');
      await expect(checkbox(page)).not.toBeChecked();
      await expect(create(page)).toBeDisabled();
      await expect(page.getByRole('link', { name: 'Read current terms · Opens in a new tab' })).toHaveAttribute('href', next.document_url);
      await expect(checkbox(page)).toHaveAccessibleDescription('Required · Published revision r2-current');
      await expect(page.locator('time')).toHaveText('11 Dec 2026');
      await capture('changed');

      await checkbox(page).check();
      await expect(create(page)).toBeEnabled();
      await supply(page, null);
      await expect(page.getByRole('status')).toContainText('Terms unavailable');
      await expect(checkbox(page)).toBeDisabled();
      await expect(checkbox(page)).not.toBeChecked();
      await expect(create(page)).toBeDisabled();
      await expect(page.getByRole('link')).toHaveCount(0);
      await expect(page.locator('time')).toHaveCount(0);
      await expect(page.locator('main')).not.toContainText('r2-current');
      await expect(page.getByRole('button', { name: 'Reload terms' })).toBeEnabled();
      await capture('unavailable');
    });
  }
}

test('keyboard consent, revocation, recovery and revision rollback never submit', async ({ page }, info) => {
  const requests: string[] = [];
  await page.route(/^https?:/, route => { requests.push(route.request().method()); return route.abort(); });
  await open(page, 320, 200);
  await page.emulateMedia({ forcedColors: 'active' });
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('textbox')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(checkbox(page)).toBeFocused();
  await page.keyboard.press('Space');
  await expect(checkbox(page)).toBeChecked();
  await page.screenshot({ path: info.outputPath('terms-forced-colors-focus.png'), fullPage: true });
  await page.keyboard.press('Space');
  await expect(create(page)).toBeDisabled();
  await page.keyboard.press('Space');
  await page.keyboard.press('Tab');
  await expect(create(page)).toBeFocused();
  await page.keyboard.press('Enter');
  await supply(page, next);
  await expect(checkbox(page)).not.toBeChecked();
  await supply(page, initial);
  await expect(checkbox(page)).not.toBeChecked();
  await expect(create(page)).toBeDisabled();
  await checkbox(page).check();
  await supply(page, null);
  const reload = page.getByRole('button', { name: 'Reload terms' });
  await expect(reload).toBeEnabled();
  await page.getByRole('textbox').focus();
  await page.keyboard.press('Tab');
  await expect(reload).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(checkbox(page)).toBeEnabled();
  await expect(checkbox(page)).not.toBeChecked();
  await expect(create(page)).toBeDisabled();
  expect(requests).toEqual([]);
});
