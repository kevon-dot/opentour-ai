import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../tests/fixtures/presentation/build.ts';
import { renderEvidencePage } from '../../tests/fixtures/presentation/evidence-page';
import type { BlockerFixture } from './blocking-reasons.fixtures';

let bundle: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  bundle = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/blocking-reasons.tsx'), 'iife');
});
const titles = ['Source · Timestamp meaning not verified', 'Evidence · Independent proof insufficient', 'Release · Promotion authority missing'];
const names = ['Open source correction', 'View proof assessment', 'Review release access'];
const paths = ['/fixture/source-correction', '/fixture/proof-assessment', '/fixture/release-access'];
for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`blocking reasons at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await renderEvidencePage(page, bundle, width, scale);
      const main = page.getByRole('main');
      const list = main.getByRole('list');
      const choose = (state: BlockerFixture) => page.evaluate(state => window.showBlockerFixture(state), state);
      const capture = async (state: string, fullPage = true) => {
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        await page.screenshot({ path: info.outputPath(`blockers-${state}-${width}-${scale}.png`), fullPage });
      };
      await expect(list.getByRole('heading')).toHaveText(titles);
      await expect(main.getByRole('heading', { name: 'Next action · Your action' })).toBeVisible();
      await expect(main.getByText('Next scheduled check 16 Nov 2026, 09:45 UTC')).toBeVisible();
      await capture('three');
      for (const [i, name] of names.entries()) {
        const link = main.getByRole('group', { name: titles[i] }).getByRole('link', { name, exact: true });
        await expect(link).toHaveAttribute('href', paths[i]);
        await page.keyboard.press('Tab');
        await expect(link).toBeFocused();
        await expect(link).toHaveCSS('outline-style', 'solid');
        if (i === 1) await capture('focus', false);
      }

      await choose('reduced');
      await expect(list.getByRole('heading')).toHaveText([titles[2], titles[0]]);
      await expect(main.getByRole('link')).toHaveText([names[2], names[0]]);
      await expect(main.getByRole('heading', { name: 'Next action · OpenTour action' })).toBeVisible();
      for (const content of [await main.innerHTML(), await main.ariaSnapshot()]) {
        for (const removed of [titles[1], names[1], paths[1], 'proof-preflight-042']) expect(content).not.toContain(removed);
      }
      await capture('reduced');
      await choose('one');
      await expect(list.getByRole('listitem')).toHaveCount(1);
      await expect(main.getByText('Next action · Waiting for external response').first()).toBeVisible();
      await expect(main.getByRole('link')).toHaveCount(0);
      await expect(main).not.toContainText('Next scheduled check');
      await capture('one');
      await choose('many');
      await expect(list.getByRole('listitem')).toHaveCount(30);
      await expect(main.getByRole('heading', { name: 'Next action · Automatic' })).toBeVisible();
      for (const kind of ['Automatic', 'Your action', 'OpenTour action', 'Waiting for external response', 'No action scheduled']) {
        await expect(list.getByText('Next action · ' + kind, { exact: true })).toHaveCount(6);
      }
      const links = main.getByRole('link');
      await expect(links).toHaveCount(30);
      // Restart traversal from the supplied first link after a previously focused projection was removed.
      await links.first().focus();
      for (let i = 0; i < 30; i++) {
        if (i) await page.keyboard.press('Tab');
        await expect(links.nth(i)).toBeFocused();
        await expect(links.nth(i)).toHaveAccessibleName('Inspect prerequisite ' + (i + 1));
      }
      await capture('last', false);
      await choose('none');
      await expect(main.getByRole('heading', { name: 'Next action · No action scheduled' })).toBeVisible();
      await expect(main.getByRole('link')).toHaveCount(0);
      await expect(main.getByRole('listitem')).toHaveCount(0);
      await capture('none');
    });
  }
}

test('each permitted recovery navigates to its supplied existing fixture destination', async ({ page }) => {
  await page.route('https://workspace.example/fixture/**', route => {
    const index = paths.indexOf(new URL(route.request().url()).pathname);
    return route.fulfill({ status: index < 0 ? 404 : 200, contentType: 'text/html',
      body: index < 0 ? '' : '<h1>' + names[index] + '</h1>' });
  });
  for (const [i, path] of paths.entries()) {
    await renderEvidencePage(page, bundle, 320, 200);
    await page.evaluate(() => {
      const base = document.createElement('base');
      base.href = 'https://workspace.example/';
      document.head.prepend(base);
    });
    const link = page.getByRole('link', { name: names[i], exact: true });
    await link.focus();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL('https://workspace.example' + path);
    await expect(page.getByRole('heading', { name: names[i], exact: true })).toBeVisible();
  }
});
