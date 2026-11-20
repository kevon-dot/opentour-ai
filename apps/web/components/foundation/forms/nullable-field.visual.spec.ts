import { expect, test, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';

const labels = ['Set a value', 'No end date', 'Leave unchanged'] as const;
const helpers = ['An explicit date will be saved.', 'No value will be saved.', 'This field will not be changed.'];
const modes = ['value', 'null', 'omitted'] as const;
let fixture: Awaited<ReturnType<typeof buildInlineFixture>>;

test.beforeAll(async () => {
  fixture = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/nullable-field.tsx'), 'iife');
});

async function open(page: Page, width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.setContent('<!doctype html><html lang="en"><title>Nullable field</title><div id="root"></div></html>');
  await page.addStyleTag({ content: fixture.css + `html{font-size:${scale}%}body{margin:16px;background:#f5f5f5}output{display:block;overflow-wrap:anywhere}` });
  await page.addScriptTag({ content: fixture.script });
  const field = page.getByRole('group', { name: 'End date · Optional' });
  await expect(field).toBeVisible();
  await page.evaluate(async () => {
    await document.fonts.load('500 14px Geist');
    await document.fonts.ready;
  });
  return field;
}

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`nullable alternatives at ${width}px and ${scale}% text`, async ({ page }, info) => {
    const field = await open(page, width, scale);
    const input = field.getByRole('textbox', { name: 'End date · Optional' });
    await expect(input).toHaveAccessibleDescription(helpers[0]);
    await expect(input).toHaveValue('2026-12-31');
    for (const [index, mode] of modes.entries()) {
      const name = labels[index];
      await field.getByRole('button', { name, exact: true }).click();
      await page.mouse.move(0, 0);
      await expect(field.getByRole('button', { pressed: true })).toHaveText(name);
      await expect(field.getByRole('button', { pressed: false })).toHaveCount(2);
      await expect(field.getByText(helpers[index], { exact: true })).toBeVisible();
      if (mode !== 'value') {
        await expect(input).toHaveCount(0);
        await expect(field.locator('.ot-text-field')).toContainText(mode === 'null' ? 'No end date' : 'Use existing value');
      }
      const frame = field.locator('.ot-text-field');
      await expect(frame).toHaveCSS('border-radius', '8px');
      await expect(frame).toHaveCSS('padding', '16px');
      await expect(frame.locator('.ot-text-field-helper')).toHaveCSS('font-size', scale === 100 ? '12px' : '24px');
      await expect(field).toHaveCSS('gap', '12px');
      const pressed = field.getByRole('button', { pressed: true });
      await expect(pressed).toHaveCSS('background-color', 'rgb(36, 91, 238)');
      const bounds = await field.boundingBox();
      expect(bounds?.width).toBe(Math.min(360, width - 32));
      if (scale === 100) expect(bounds?.height).toBe(276);
      for (const button of await field.getByRole('button').all()) {
        const box = await button.boundingBox();
        expect(box?.width).toBe(bounds?.width);
        expect(box!.height).toBeGreaterThanOrEqual(44);
        expect(await button.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await field.screenshot({ path: info.outputPath(`nullable-${width}-${scale}-${mode}.png`), caret: 'hide' });
    }
    await field.getByRole('button', { name: labels[0] }).click();
    await expect(input).toHaveValue('2026-12-31');
  });
}

test('uses native keyboard activation, preserves the empty draft, and hides revoked omission', async ({ page }, info) => {
  const field = await open(page, 320, 200);
  const input = field.getByRole('textbox', { name: 'End date · Optional' });
  const button = (index: number) => field.getByRole('button', { name: labels[index], exact: true });
  const history = page.getByRole('status', { name: 'Callback history' });
  await page.getByRole('button', { name: 'Toggle allowed omission' }).focus();
  await page.keyboard.press('Tab');
  await expect(input).toBeFocused();
  await expect(field.locator('.ot-text-field')).toHaveCSS('border-top-width', '2px');
  await input.fill('');
  await page.keyboard.press('Tab');
  await expect(button(0)).toBeFocused();
  await expect(button(0)).toHaveCSS('outline-style', 'solid');
  await page.keyboard.press('Tab');
  await expect(button(1)).toBeFocused();
  await page.keyboard.press('Space');
  await expect(button(1)).toBeFocused();
  await expect(button(1)).toHaveAttribute('aria-pressed', 'true');
  await field.screenshot({ path: info.outputPath('nullable-keyboard-focus.png') });
  await page.keyboard.press('Tab');
  await expect(button(2)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(button(2)).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Toggle allowed omission' }).click();
  await expect(button(2)).toHaveCount(0);
  await expect(field.getByText('Use existing value')).toHaveCount(0);
  await expect(field.getByRole('button', { pressed: true })).toHaveCount(0);
  await page.keyboard.press('Tab');
  await expect(button(0)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(input).toHaveValue('');
  await expect(history).toHaveText(JSON.stringify([{ kind: 'value', raw: '' }, { kind: 'null' }, { kind: 'omitted' }, { kind: 'value', raw: '' }]));
});

test('waits for caller updates before changing the editor or selected alternative', async ({ page }) => {
  const field = await open(page, 768, 100);
  await page.getByRole('button', { name: 'Toggle caller updates' }).click();
  await field.getByRole('textbox').fill('  raw draft  ');
  await field.getByRole('button', { name: labels[1] }).click();
  await expect(field.getByRole('button', { pressed: true })).toHaveText(labels[0]);
  await expect(field.getByRole('textbox')).toHaveValue('2026-12-31');
  await expect(page.getByRole('status', { name: 'Callback history' })).toHaveText(
    JSON.stringify([{ kind: 'value', raw: '  raw draft  ' }, { kind: 'null' }]));
});
