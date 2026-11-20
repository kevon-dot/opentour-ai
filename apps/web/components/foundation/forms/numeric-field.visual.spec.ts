import { expect, test, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';

let fixture: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  fixture = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/numeric-field.tsx'), 'esm');
});

async function open(page: Page, width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.route('http://numeric.test/', route => route.fulfill({ contentType: 'text/html',
    body: '<!doctype html><html lang="en"><title>Numeric fields</title><main></main></html>' }));
  await page.route('http://numeric.test/fixture.js', route => route.fulfill({ contentType: 'text/javascript', body: fixture.script }));
  await page.goto('http://numeric.test/');
  await page.addStyleTag({ content: fixture.css + `
    html {font-size:${scale}%}
    body {margin:0}
    section {display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),360px));
      gap:32px; padding:clamp(16px,3vw,32px); align-items:start; background:#f5f5f5}
  ` });
  await page.addScriptTag({ type: 'module', url: 'http://numeric.test/fixture.js' });
  await expect(page.getByRole('textbox')).toHaveCount(10);
  await page.evaluate(async () => {
    await Promise.all(['500 14px Geist', '400 12px Geist'].map(font => document.fonts.load(font)));
    await document.fonts.ready;
  });
}

const families = {
  integer: { label: 'Maximum attempts', raw: '3', invalid: '2.5', error: 'Enter a whole number. 2.5 is not valid.',
    helper: 'Enter a whole number from the allowed range.' },
  decimal: { label: 'Threshold', raw: '0.25', invalid: '0..25', error: 'Enter a valid decimal.',
    helper: 'Enter a decimal using the precision shown in this policy.' },
};

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test('numeric families at ' + width + 'px and ' + scale + '% text', async ({ page }, info) => {
    await open(page, width, scale);
    for (const [kind, example] of Object.entries(families)) {
      const family = page.getByRole('region', { name: kind, exact: true });
      const get = (state: string) => family.locator('#' + kind + '-' + state);
      await expect(family.getByRole('textbox', { name: example.label + ' · Required', exact: true })).toHaveCount(5);
      await expect(get('default')).toHaveValue(example.raw);
      await expect(get('default')).toHaveAccessibleDescription(example.helper);
      await expect(get('error')).toHaveValue(example.invalid);
      await expect(get('error')).toHaveAttribute('aria-invalid', 'true');
      await expect(get('error')).toHaveAccessibleDescription(example.error);
      await expect(get('disabled')).toBeDisabled();
      await expect(get('disabled')).toHaveAccessibleDescription(example.helper + ' Unavailable: your current role cannot edit this field.');
      await expect(get('read-only')).toHaveAttribute('readonly', '');
      await get('default').focus();
      await page.keyboard.press('Tab');
      await expect(get('focus')).toBeFocused();
      const frame = (state: string) => get(state).locator('..');
      await expect(frame('focus')).toHaveCSS('border-top', '2px solid rgb(36, 91, 238)');
      await expect(frame('error')).toHaveCSS('background-color', 'rgb(255, 232, 227)');
      await expect(frame('error')).toHaveCSS('color', 'rgb(170, 69, 58)');
      await expect(frame('disabled')).toHaveCSS('opacity', '0.6');
      await expect(frame('read-only')).toHaveCSS('opacity', '1');
      await expect(get('default')).toHaveCSS('font-size', scale === 100 ? '14px' : '28px');
      await expect(frame('default')).toHaveCSS('padding-top', '16px');
      const geometry = await frame('default').boundingBox();
      expect(geometry?.width).toBe(width === 320 ? 288 : 360);
      if (width >= 768 && scale === 100) expect(geometry?.height).toBe(108);
      // The helper and external reason must grow vertically with text, without clipping at any width.
      const fits = await family.locator('p, label, .ot-text-field').evaluateAll(nodes =>
        nodes.every(n => n.scrollWidth <= n.clientWidth + 1 && n.scrollHeight <= n.clientHeight + 1));
      expect(fits).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
      await family.screenshot({ path: info.outputPath('numeric-' + kind + '-' + width + '-' + scale + '.png'),
        caret: 'hide', animations: 'disabled' });
      await page.keyboard.press('Tab');
      await expect(get('error')).toBeFocused();
      await page.keyboard.press('Tab');
      await expect(get('read-only')).toBeFocused();
      await page.keyboard.type('9');
      await expect(get('read-only')).toHaveValue(example.raw);
    }
  });
}

test('keyboard edits and real clipboard paste preserve raw drafts through validation and recovery', async ({ page }) => {
  await open(page, 768, 100);
  for (const [kind, example] of Object.entries(families)) {
    const edit = page.locator('#' + kind + '-default');
    const source = page.locator('#' + kind + '-error');
    await source.focus();
    await source.press('ControlOrMeta+a');
    await source.press('ControlOrMeta+c');
    await edit.focus();
    await edit.press('ControlOrMeta+a');
    await edit.press('ControlOrMeta+v');
    await expect(edit).toHaveValue(example.invalid);
    await edit.press('Tab');
    await expect(edit).toHaveAccessibleDescription(example.error);
    await expect(edit).toHaveValue(example.invalid);
    await edit.fill('');
    await edit.press('Tab');
    await expect(edit).toHaveValue('');
    await expect(edit).not.toHaveAttribute('aria-invalid', 'true');
    await edit.pressSequentially('0');
    await expect(edit).toHaveValue('0');
    const draft = kind === 'decimal' ? '0.00000000000000000001000' : '0000000000000000000000042';
    await edit.fill('');
    await edit.pressSequentially(draft);
    await edit.press('Tab');
    await expect(edit).toHaveValue(draft);
    await expect(edit).toHaveAccessibleDescription(example.helper);
  }
});
