import { test, expect, type Page } from '@playwright/test';
import { resolve } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import type { StatusCopyProps } from './status-copy.types';

declare global {
  interface Window {
    showStatus(notice: StatusCopyProps | null, toast: StatusCopyProps | null): void;
    mutations: string[];
    persistentNode: Element;
  }
}

const conflict = { presentation: 'notice', fixture: 'UI-TX-SAVE/conflict' } as const;
const accepted = { presentation: 'toast', fixture: 'UI-TX-CANCEL/cancel_requested' } as const;
const origin = 'http://status-copy.test';
let html: string;

test.beforeAll(async () => {
  // Bundle the real component and its local font assets into an offline document.
  const output = await buildInlineFixture(resolve(process.cwd(), 'apps/web/components/foundation/async-state/fixture-entry.tsx'), 'iife', `
    import { createRoot } from 'react-dom/client';
    import { flushSync } from 'react-dom';
    import { StatusCopy } from './status-copy';
    import tokens from '../../../styles/product-tokens.css?inline';
    import feedback from '../tokens/feedback.css?inline';
    const styles = document.createElement('style');
    styles.textContent = tokens + feedback; document.head.append(styles);
    const root = createRoot(document.querySelector('main'));
    window.showStatus = (notice, toast) => flushSync(() => root.render(<>
      {notice && <StatusCopy key="persistent" {...notice} />}
      {toast && <StatusCopy key="supplementary" {...toast} />}
    </>));
  `, { requireStylesheet: false });
  html = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
    // The native frame has an opaque surface. Paint it in the document so rounded
    // edges blend against that surface during rasterization.
    '<style>html{background:var(--ot-surface-base)}body{margin:0} main{display:grid;gap:16px;padding:16px}.ot-notice{max-width:440px}.ot-toast{max-width:360px}</style>' +
    '<main></main><script>' + output.script.replaceAll('</script', '<\\/script') + '</script>';
});

async function open(page: Page) {
  await page.route('**/*', route => {
    if (route.request().url() !== origin + '/') return route.abort();
    return route.fulfill({ contentType: 'text/html', body: html });
  });
  await page.goto(origin);
  await page.evaluate(([notice, toast]) => window.showStatus(notice, toast), [conflict, accepted] as const);
  await page.evaluate(async () => {
    await document.fonts.load('500 14px Geist'); await document.fonts.load('400 12px Geist');
    await document.fonts.ready;
  });
  expect(await page.evaluate(() => [...document.fonts].some(face => face.family === 'Geist' && face.status === 'loaded'))).toBe(true);
}

const viewports = [
  [320, 800, 136, 116, 378, 338], [768, 1024, 98, 116, 266, 262],
  [1280, 900, 98, 116, 266, 262], [1440, 1024, 98, 116, 266, 262],
] as const;
for (const [width, height, normalNotice, normalToast, largeNotice, largeToast] of viewports) {
  for (const scale of [1, 2]) {
    test('native viewport ' + width + ' at ' + scale * 100 + '% text', async ({ page }, info) => {
      await page.setViewportSize({ width, height });
      await open(page);
      await page.evaluate(multiplier => { document.documentElement.style.fontSize = (16 * multiplier) + 'px'; }, scale);
      const notices = page.getByRole('status');
      await expect(notices).toHaveCount(2);
      await expect(notices.first()).toContainText('This configuration changed. Review the latest version.');
      await expect(notices.last()).toContainText('Cancellation requested');
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
      const first = await notices.first().boundingBox();
      const second = await notices.last().boundingBox();
      const noticeHeight = scale === 1 ? normalNotice : largeNotice;
      const toastHeight = scale === 1 ? normalToast : largeToast;
      expect(first).toEqual({ x: 16, y: 16, width: Math.min(width - 32, 440), height: noticeHeight });
      expect(second).toEqual({ x: 16, y: 32 + noticeHeight, width: Math.min(width - 32, 360), height: toastHeight });
      await expect(notices.first()).toHaveCSS('background-color', 'rgb(255, 242, 213)');
      await expect(notices.last()).toHaveCSS('background-color', 'rgb(255, 255, 255)');
      await expect(notices.first().locator('p').first()).toHaveCSS('font-size', scale * 14 + 'px');
      await expect(notices.first().locator('p').last()).toHaveCSS('line-height', scale * 18 + 'px');
      const name = 'status-' + width + '-' + scale * 100 + '.png';
      const image = await page.screenshot({ animations: 'disabled' });
      await info.attach(name, { body: image, contentType: 'image/png' });
      expect(image).toMatchSnapshot(name, { maxDiffPixels: 0, threshold: 0 });
    });
  }
}

test('COPY-03 repeated updates and COPY-04 independent removal preserve focus', async ({ page }) => {
  await open(page);
  await page.evaluate(() => {
    const sentinel = document.createElement('button');
    sentinel.textContent = 'Current control';
    document.body.append(sentinel); sentinel.focus();
    window.persistentNode = document.querySelector('.ot-notice')!;
    window.mutations = [];
    new MutationObserver(() => window.mutations.push(window.persistentNode.textContent || ''))
      .observe(window.persistentNode, { characterData: true, childList: true, subtree: true });
  });
  const hold = { presentation: 'notice', fixture: 'UI-TX-REVOKE/control_blocked' } as const;
  for (let index = 0; index < 5; index++) {
    await page.evaluate(([notice, toast]) => window.showStatus(notice, toast), [hold, accepted] as const);
    await expect(page.getByRole('button')).toBeFocused();
  }
  expect(await page.evaluate(() => window.mutations)).toHaveLength(1);
  await page.evaluate(notice => window.showStatus(notice, null), hold);
  await expect(page.getByRole('status')).toHaveCount(1);
  expect(await page.evaluate(() => window.persistentNode === document.querySelector('.ot-notice'))).toBe(true);
  await expect(page.getByRole('status')).toContainText('Blocking new work');
  await page.evaluate(notice => window.showStatus(notice, { presentation: 'toast', fixture: 'UI-TX-SAVE/failed', urgent: true }), hold);
  await expect(page.getByRole('alert')).toContainText('Could not save changes');
  await expect(page.getByRole('button')).toBeFocused();
  await page.evaluate(() => window.showStatus(null, null));
  await expect(page.getByRole('status')).toHaveCount(0);
  await expect(page.getByRole('button')).toBeFocused();
});
