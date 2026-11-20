import { expect, test, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';

let fixture: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  fixture = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/unit-field.tsx'), 'iife');
});
async function open(page: Page, width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.setContent('<!doctype html><html lang="en"><meta charset="utf-8"><title>Unit fields</title><main></main></html>');
  await page.addStyleTag({ content: fixture.css + `
    html { font-size:${scale}% } body {margin:0}
    section {display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),360px));
      gap:32px; padding:clamp(16px,3vw,32px); align-items:start; background:#f5f5f5}` });
  await page.addScriptTag({ content: fixture.script });
  await expect(page.getByRole('textbox')).toHaveCount(10);
  await page.evaluate(async () => { await document.fonts.ready; });
}

const families = {
  currency: { label: 'Maximum spend', unit: 'USD', other: 'EUR', raw: '100.00', invalid: '−10.00',
    helper: 'US dollars. Exact cents are used when you submit.', error: 'Enter a nonnegative amount with at most 2 decimal places.' },
  duration: { label: 'Timeout', unit: 'seconds', other: 'milliseconds', raw: '120', invalid: '1.5',
    helper: 'Enter a whole number of seconds.', error: 'Enter a whole number of seconds.' },
};
for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test('unit field families at ' + width + 'px and ' + scale + '% text', async ({ page }, info) => {
    await open(page, width, scale);
    for (const [kind, spec] of Object.entries(families)) {
      const family = page.getByRole('region', { name: kind });
      const get = (state: string) => family.locator('#' + kind + '-' + state);
      const frame = (state: string) => family.locator('.ot-text-field').filter({ has: page.locator('#' + kind + '-' + state) });
      await expect(family.getByRole('textbox', { name: spec.label + ' · Required', exact: true })).toHaveCount(5);
      await expect(get('default')).toHaveValue(spec.raw);
      await expect(get('default')).toHaveAccessibleDescription(spec.unit + ' ' + spec.helper);
      await expect(get('error')).toHaveValue(spec.invalid);
      await expect(get('error')).toHaveAttribute('aria-invalid', 'true');
      await expect(get('error')).toHaveAccessibleDescription(spec.unit + ' ' + spec.error);
      await expect(get('disabled')).toBeDisabled();
      await expect(get('disabled')).toHaveAccessibleDescription(spec.unit + ' ' + spec.helper + ' Unavailable: your current role cannot edit this field.');
      await expect(get('read-only')).toHaveAttribute('readonly', '');
      await get('default').focus();
      await page.keyboard.press('Tab');
      await expect(get('focus')).toBeFocused();
      await expect(frame('focus')).toHaveCSS('border-top', '2px solid rgb(36, 91, 238)');
      await expect(frame('disabled')).toHaveCSS('opacity', '0.6');
      await expect(frame('error')).toHaveCSS('background-color', 'rgb(255, 232, 227)');
      await expect(frame('error')).toHaveCSS('color', 'rgb(170, 69, 58)');
      await expect(frame('default')).toHaveCSS('width', width === 320 ? '288px' : '360px');
      await expect(get('default')).toHaveCSS('font-size', scale === 100 ? '14px' : '28px');
      const fits = await family.locator('label, p, .ot-text-field-unit, .ot-text-field-value').evaluateAll(nodes =>
        nodes.every(n => n.scrollWidth <= n.clientWidth + 1 && n.scrollHeight <= n.clientHeight + 1));
      expect(fits).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
      // The unit sits beside the raw value with a small interword gap even at 200% text.
      const value = await get('default').boundingBox();
      const unit = await family.locator('#' + kind + '-default-unit').boundingBox();
      if (!unit || !value) throw new Error('Missing value or unit');
      const gap = kind === 'currency' ? value.x - unit.x - unit.width : unit.x - value.x - value.width;
      expect(gap).toBeGreaterThan(0);
      expect(gap).toBeLessThanOrEqual(8);
      if (kind === 'duration') expect(value.width).toBeLessThan(40 * scale / 100);
      await family.screenshot({ path: info.outputPath('unit-' + kind + '-' + width + '-' + scale + '.png'), caret: 'hide' });
      await page.keyboard.press('Tab');
      await expect(get('error')).toBeFocused();
      await page.keyboard.press('Tab');
      await expect(get('read-only')).toBeFocused();
      await page.keyboard.type('2');
      await expect(get('read-only')).toHaveValue(spec.raw);
    }
  });
}
for (const [kind, spec] of Object.entries(families)) {
  test(kind + ' changes its unit independently of the exact draft', async ({ page }) => {
    await open(page, 320, 200);
    const field = page.locator('#' + kind + '-default');
    const unit = page.locator('#' + kind + '-default-unit');
    for (const draft of ['', '0', spec.invalid, '000.20000000000000000100', ' 1.. ']) {
      await field.fill(draft);
      await field.press('Tab');
      await page.getByRole('button', { name: 'Change ' + kind + ' unit' }).click();
      await expect(unit).toHaveText(spec.other);
      await expect(field).toHaveAccessibleDescription(new RegExp('^' + spec.other + ' '));
      await expect(field).toHaveValue(draft);
      await page.getByRole('button', { name: 'Change ' + kind + ' unit' }).click();
      await expect(unit).toHaveText(spec.unit);
      await expect(field).toHaveValue(draft);
    }
  });
}
