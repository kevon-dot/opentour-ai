import { expect, type Page } from '@playwright/test';

/** Render the actual component bundle with the product font and the standard board insets. */
export async function renderEvidencePage(page: Page, bundle: { css: string; script: string }, width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.setContent('<!doctype html><html lang="en"><title>Evidence specimen</title><main></main></html>');
  const inset = width === 320 ? 16 : width === 768 ? 24 : 32;
  await page.addStyleTag({ content: bundle.css + `html {font-size:${scale}%} body {margin:0; background:var(--ot-surface-canvas)} main {padding:${inset}px; max-width:1216px; margin-inline:auto}` });
  await page.addScriptTag({ content: bundle.script });
  await expect(page.getByRole('heading').first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

export async function expectRecordRows(page: Page, title: string, columns: string[], rows: string[][], stacked: boolean) {
  const region = page.getByRole('region', { name: title, exact: true });
  if (stacked) {
    await expect(region.getByRole('table')).toHaveCount(0);
    const list = region.getByRole('list', { name: title, exact: true });
    await expect(list.getByRole('listitem')).toHaveCount(rows.length);
    for (const [index, values] of rows.entries()) {
      const row = list.getByRole('listitem').nth(index);
      await expect(row.getByRole('term')).toHaveText(columns.map(label => label + ':'));
      await expect(row.getByRole('definition')).toHaveText(values);
    }
  } else {
    await expect(region.getByRole('list')).toHaveCount(0);
    const table = region.getByRole('table', { name: title, exact: true });
    await expect(table.getByRole('columnheader')).toHaveText(columns);
    await expect(table.getByRole('rowheader')).toHaveText(rows.map(row => row[0]));
    await expect(table.getByRole('cell')).toHaveText(rows.flatMap(row => row.slice(1)));
  }
}
