import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { buildInlineFixture } from '../../tests/fixtures/presentation/build.ts';
import { historyEntries, referenceEntries } from './effort-ledger.fixtures';

let fixture: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  fixture = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/effort-ledger.tsx'), 'iife');
});

for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`effort ledger at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.setContent('<!doctype html><html lang="en"><title>Effort ledger</title><body><div id="root"></div></body></html>');
      const inset = width === 320 ? 16 : width === 768 ? 24 : 32;
      await page.addStyleTag({ content: fixture.css + `html {font-size:${scale}%} body {margin:0;background:var(--ot-surface-canvas)} main {padding:${inset}px}` });
      await page.evaluate(fixture.script);
      const main = page.getByRole('main');
      const table = main.getByRole('table', { name: 'Effort ledger' });
      const capture = async (state: string) => {
        await page.evaluate(() => document.fonts.ready);
        const stem = `effort-ledger-${state}-${width}-${scale}`;
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
        await page.screenshot({ path: info.outputPath(stem + '.png'), fullPage: true, animations: 'disabled' });
        await writeFile(info.outputPath(stem + '.json'), JSON.stringify({ state, aria: await main.ariaSnapshot() }));
      };
      for (const state of ['reference', 'history'] as const) {
        await page.evaluate(state => window.showEffortLedger(state), state);
        const entries = state === 'reference' ? referenceEntries : historyEntries;
        await expect(table.getByRole('row')).toHaveCount(entries.length + 1);
        await expect(table.getByRole('columnheader')).toHaveText(['Entry / category', 'Active / waiting', 'Attribution']);
        const rows = table.getByRole('row');
        for (const [index, entry] of entries.entries()) {
          const row = rows.nth(index + 1);
          await expect(row.getByRole('rowheader')).toContainText(entry.id + ' · ' + entry.category);
          const values = row.getByRole('cell');
          await expect(values.nth(0)).toContainText(entry.activeMinutes === null ? 'Active minutes unavailable' : entry.activeMinutes + ' min active');
          await expect(values.nth(0)).toContainText(entry.waitingMinutes === null ? 'Waiting minutes unavailable' : entry.waitingMinutes + ' min waiting');
          await expect(values.nth(1)).toContainText([entry.person, entry.actorClass, entry.sourceQuality].join(' · '));
        }
        const narrow = width - 2 * inset - 34 <= 576 * scale / 100;
        await expect(rows.nth(1)).toHaveCSS('display', narrow ? 'block' : 'table-row');
        await expect(rows.nth(1).getByRole('rowheader')).toHaveCSS('font-size', scale === 200 ? '28px' : '14px');
        expect(await table.locator('tbody th, tbody td').evaluateAll(cells => cells.every(el => el.scrollWidth <= el.clientWidth + 1))).toBe(true);
        if (state === 'history') {
          await expect(rows.nth(1)).toContainText('Work item: work-042');
          await expect(rows.nth(1)).toContainText('UTC interval: 2026-11-16 09:00 – 2026-11-16 09:18');
          await expect(rows.nth(7)).toContainText('Supersedes: effort-001');
        }
        await expect(main.getByRole('button')).toHaveCount(0);
        await capture(state);
      }
      await page.evaluate(() => window.showEffortLedger('revoked'));
      await expect(main.getByRole('group', { name: 'Access revoked' })).toBeFocused();
      await expect(table).toHaveCount(0);
      for (const surface of [await main.innerHTML(), await main.ariaSnapshot()]) {
        expect(surface).not.toMatch(/Jordan Davis|Fixture specialist|Mina Patel|work-042|effort-0|Waiting minutes|Supersedes/);
      }
      await capture('revoked');
    });
  }
}
