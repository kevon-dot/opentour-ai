import { expect, test, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../tests/fixtures/presentation/build.ts';
import type { SetupStep } from './setup-step-indicator';

const positions = ['organization', 'workspace', 'project'] as const;
const labels = {
  organization: ['Organization · Current step', 'Workspace · Not started', 'Project · Not started'],
  workspace: ['Organization · Done', 'Workspace · Current step', 'Project · Not started'],
  project: ['Organization · Done', 'Workspace · Done', 'Project · Current step'],
};
const frames = [
  { width: 320, height: 536, inset: 16, bodyPadding: 16 },
  { width: 768, height: 468, inset: 24, bodyPadding: 24 },
  { width: 1280, height: 484, inset: 280, bodyPadding: 32 },
  { width: 1440, height: 484, inset: 360, bodyPadding: 32 },
];

let bundle: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  bundle = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/setup-step-indicator.tsx'), 'iife');
});

async function open(page: Page, scale = 100) {
  await page.setContent('<!doctype html><html lang="en"><title>Setup progress</title><div id="fixture"></div></html>');
  await page.addStyleTag({ content: bundle.css });
  await page.addStyleTag({ content: `html { font-size: ${scale}% }` });
  await page.addScriptTag({ content: bundle.script });
  await expect(page.getByRole('list', { name: 'Setup progress' })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

async function supply(page: Page, step: SetupStep) {
  await page.evaluate(detail => window.dispatchEvent(new CustomEvent('setup-fixture-current', { detail })), step);
}

async function checkPosition(page: Page, step: SetupStep) {
  const list = page.getByRole('list', { name: 'Setup progress' });
  await expect(list.getByRole('listitem')).toHaveCount(3);
  await expect(list.locator('.ot-setup-step-title')).toHaveText(labels[step]);
  await expect(list.locator('[aria-current="step"]')).toHaveCount(1);
  await expect(list.locator('[aria-current="step"] .ot-setup-step-title')).toHaveText(labels[step][positions.indexOf(step)]);
}

for (const { width, height, inset, bodyPadding } of frames) {
  for (const scale of [100, 200]) {
    test(`renders all supplied positions at ${width}px with ${scale}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height });
      await open(page, scale);
      const list = page.getByRole('list');
      const firstCard = list.getByRole('listitem').first();
      await expect(firstCard.locator('.ot-setup-step-title')).toHaveCSS('font-size', scale === 100 ? '14px' : '28px');
      await expect(firstCard.locator('.ot-setup-step-details')).toHaveCSS('font-family', 'Geist, sans-serif');
      if (scale === 100) {
        expect(await firstCard.boundingBox()).toEqual({
          x: inset, y: 54 + bodyPadding + 32 + 16, width: width - 2 * inset, height: width === 320 ? 106 : 84,
        });
        expect(await page.locator('main').evaluate(main => main.getBoundingClientRect().bottom)).toBe(height);
      }
      for (const position of positions) {
        await supply(page, position);
        await checkPosition(page, position);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        expect(await list.locator('li > span').evaluateAll(spans => spans.every(span =>
          span.scrollWidth <= span.clientWidth && span.scrollHeight <= span.clientHeight))).toBe(true);
        await page.screenshot({ path: info.outputPath(`setup-${position}-${width}-${scale}.png`), fullPage: true });
      }
    });
  }
}

test('forced colors preserves all three visible statuses at 200% text', async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 536 });
  await page.emulateMedia({ forcedColors: 'active' });
  await open(page, 200);
  await supply(page, 'workspace');
  await checkPosition(page, 'workspace');
  await expect(page.getByRole('listitem').first()).toHaveCSS('border-top-style', 'solid');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
  await page.screenshot({ path: info.outputPath('setup-forced-colors.png'), fullPage: true });
});

test('mounts and re-renders without advancing, taking keyboard focus or requesting creation', async ({ page }) => {
  const external: string[] = [];
  await page.route(/^https?:/, route => {
    external.push(route.request().method() + ' ' + route.request().url());
    return route.abort();
  });
  await page.clock.install();
  await open(page);
  await checkPosition(page, 'organization');
  await page.clock.fastForward(60_000);
  await checkPosition(page, 'organization');
  await page.keyboard.press('Tab');
  expect(await page.getByRole('list').locator(':focus').count()).toBe(0);
  await supply(page, 'project');
  await checkPosition(page, 'project');
  await supply(page, 'workspace');
  await supply(page, 'workspace');
  await page.getByText('Workspace · Current step').click();
  await page.keyboard.press('Enter');
  await page.clock.fastForward(86_400_000);
  await checkPosition(page, 'workspace');
  expect(external).toEqual([]);
});
