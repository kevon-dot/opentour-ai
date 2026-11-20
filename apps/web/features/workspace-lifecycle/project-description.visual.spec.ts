import { test, expect, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../tests/fixtures/presentation/build.ts';

const label = 'Description · Optional';
const sample = 'Refund outcome learning.';
const guidance = 'Optional; No value is distinct from empty';
const problem = 'Use 500 characters or fewer.';
const count = (length: number) => `${length} / 500 characters · ${guidance}`;
let fixture: Awaited<ReturnType<typeof buildInlineFixture>>;

test.beforeAll(async () => {
  fixture = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/project-description.tsx'), 'iife');
});

async function open(page: Page, width: number, scale: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.route('http://localhost/description', route => route.fulfill({
    contentType: 'text/html', body: '<!doctype html><html lang="en"><title>Project description</title><div id="root"></div></html>',
  }));
  await page.goto('http://localhost/description');
  const measure = width === 320 ? 240 : 672;
  await page.addStyleTag({ content: fixture.css + `html{font-size:${scale}%}body{margin:16px;background:#f5f5f5}form{width:${measure}px;max-width:100%}.typed{width:360px;max-width:100%;--ot-project-description-min-height:0px}output{display:block;overflow-wrap:anywhere}` });
  await page.addScriptTag({ content: fixture.script });
  await expect(page.locator('form').getByRole('textbox')).toHaveValue(sample);
  await page.evaluate(async () => {
    await document.fonts.load('500 14px Geist');
    await document.fonts.load('400 12px Geist');
    await document.fonts.ready;
  });
}

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`project description at ${width}px and ${scale}% text`, async ({ page }, info) => {
    await open(page, width, scale);
    const form = page.locator('form');
    const field = form.getByRole('textbox', { name: label });
    const settings = form.locator('.ot-project-description');
    const frame = settings.locator('.ot-text-field');
    await expect(field).toHaveAccessibleDescription(count(24));
    await expect(frame).toHaveCSS('padding', '16px');
    await expect(frame).toHaveCSS('border-radius', '8px');
    await expect(frame).toHaveCSS('column-gap', '8px');
    await expect(frame.locator('label')).toHaveCSS('font-size', scale === 100 ? '14px' : '28px');
    const box = await frame.boundingBox();
    expect(box!.width).toBe(width === 320 ? 240 : 672);
    expect(box!.height).toBeGreaterThanOrEqual(154);
    if (scale === 100) expect(box!.height).toBe(154);
    const capture = async (state: string, target: typeof settings) => {
      expect(await target.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      await target.screenshot({ path: info.outputPath(`description-${width}-${scale}-${state}.png`), caret: 'hide' });
    };
    await capture('settings', settings);
    const typed = page.getByRole('region', { name: 'Typed default' });
    const typedInput = typed.getByRole('textbox', { name: label });
    await expect(typedInput).toHaveAccessibleDescription((await typedInput.inputValue()).length + ' / 500 characters');
    expect(await typedInput.evaluate(el => el.scrollHeight <= el.clientHeight + 1)).toBe(true);
    await capture('default', typed);
    await form.getByRole('button', { name: 'Save changes' }).focus();
    await page.keyboard.press('Tab');
    await expect(typedInput).toBeFocused();
    await expect(typed.locator('.ot-text-field')).toHaveCSS('border-top', '2px solid rgb(36, 91, 238)');
    await capture('focus', typed);
    const unavailable = page.getByRole('region', { name: 'Unavailable' });
    await expect(unavailable.getByRole('textbox')).toBeDisabled();
    await expect(unavailable.getByRole('textbox')).toHaveAccessibleDescription(/Unavailable: your current role cannot edit this field\./);
    await expect(unavailable.locator('.ot-text-field')).toHaveCSS('opacity', '0.6');
    await capture('disabled', unavailable);
    await page.keyboard.press('Tab');
    const readonly = page.getByRole('region', { name: 'Read only' });
    await expect(readonly.getByRole('textbox')).toBeFocused();
    await expect(readonly.getByRole('textbox')).toHaveJSProperty('readOnly', true);
    await page.keyboard.press('Tab');
    await capture('readonly', readonly);
    await field.fill('a'.repeat(501));
    await field.blur();
    await expect(field).toHaveValue('a'.repeat(501));
    await expect(field).toHaveAccessibleDescription(count(501) + ' ' + problem);
    await expect(frame).toHaveCSS('background-color', 'rgb(255, 232, 227)');
    await expect(frame).toHaveCSS('color', 'rgb(170, 69, 58)');
    expect(await field.evaluate(el => el.scrollHeight <= el.clientHeight + 1)).toBe(true);
    await capture('error', settings);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test('retains a pasted over-limit draft, links the save error to the actual textarea, and clears it on correction', async ({ page, context }) => {
  await open(page, 768, 100);
  const field = page.locator('form').getByRole('textbox', { name: label });
  const original = await field.elementHandle();
  for (const draft of ['', 'two\nlines', 'a'.repeat(499), 'a'.repeat(500)]) {
    await field.fill(draft);
    await expect(field).toHaveValue(draft);
    await expect(field).toHaveAccessibleDescription(count(draft.length));
    await expect(field).not.toHaveAttribute('aria-invalid', 'true');
  }
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: 'http://localhost' });
  const pasted = 'a'.repeat(499) + '\nb';
  await page.evaluate(text => navigator.clipboard.writeText(text), pasted);
  await field.focus();
  await field.press('ControlOrMeta+a');
  await field.press('ControlOrMeta+v');
  await expect(field).toHaveValue(pasted);
  await expect(field).toHaveAccessibleDescription(count(501) + ' ' + problem);
  await page.getByRole('button', { name: 'Save changes' }).click();
  const link = page.getByRole('link', { name: /Description/ });
  await expect(link).toHaveAttribute('href', '#project-description');
  await expect(page.getByRole('region', { name: 'Check 1 field before continuing' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(link).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(field).toBeFocused();
  expect(await field.evaluate((el, before) => el === before, original)).toBe(true);
  await field.evaluate((el: HTMLTextAreaElement) => el.setSelectionRange(el.value.length, el.value.length));
  await field.press('Backspace');
  await expect(field).toHaveValue(pasted.slice(0, -1));
  await expect(link).toHaveCount(0);
  await expect(field).toHaveAccessibleDescription(count(500));
  await expect(field).not.toHaveAttribute('aria-invalid', 'true');
});

test('preserves the caller-owned value and keeps server error associations on the same control', async ({ page }) => {
  await open(page, 320, 200);
  const field = page.locator('form').getByRole('textbox', { name: label });
  await page.getByRole('button', { name: 'Toggle caller updates' }).click();
  await field.fill('not yet accepted');
  await expect(field).toHaveValue(sample);
  await page.getByRole('button', { name: 'Supply error' }).click();
  await expect(field).toHaveAccessibleDescription(count(24) + ' Review this description before saving.');
  await expect(field).toHaveAttribute('aria-describedby', 'project-description-helper project-description-error');
  await page.getByRole('button', { name: 'Toggle caller updates' }).click();
  await field.fill('');
  await expect(field).toHaveAccessibleDescription(count(0));
  await expect(field).toHaveAttribute('id', 'project-description');
});
