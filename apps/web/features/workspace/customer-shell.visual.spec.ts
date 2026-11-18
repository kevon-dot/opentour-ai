import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { build } from 'vite';

const navigation = ['Overview', 'Workflows', 'Sources', 'Learning assets', 'Experiments', 'Benchmarks', 'Releases', 'Delivery'];
const tabLabels = ['Overview', 'Mind web', 'Datasets', 'Experiments', 'Benchmarks', 'Releases'];
const identities = [
  { mode: 'Customer controlled', name: 'Acme Labs', scope: 'Production workspace' },
  { mode: 'OpenTour managed', name: 'Northstar Research and Development Customer Operations', scope: 'Production workspace' },
] as const;
let script: string;
let css: string;
test.beforeAll(async () => {
  const root = join(process.cwd(), 'apps/web');
  const input = join(root, 'shell-fixture.tsx');
  const bundle = await build({
    root, configFile: false, publicDir: false, envFile: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } }, define: { 'process.env.NODE_ENV': '"production"' },
    plugins: [{ name: 'local-shell-fixtures', resolveId: id => id === input ? input : null,
      load: id => id === input ? `
        import { useState } from 'react';
        import { createRoot } from 'react-dom/client';
        import { CustomerShell } from './features/workspace/customer-shell';
        const index = Number(document.body.dataset.fixture);
        const identity = ${JSON.stringify(identities)}[index];
        const links = ${JSON.stringify(navigation)}.map((label,i)=>({label,href:'#nav-'+i}));
        function Fixture() {
          const [selectedKey,onSelectionChange]=useState('Overview');
          return <CustomerShell brand="OpenTour" workspace={{customer:identity.name,scope:identity.scope}}
            navigation={links} activeHref={document.body.dataset.active || links[0].href}
            utilities={['Environment','Usage','Settings'].map(label=>({label,href:'#'+label}))}
            delivery={{title:'Your delivery team',detail:'White-glove support is included.'}}
            profile="JD  Jordan Davis"
            breadcrumb={{label:'Breadcrumb',items:[{label:identity.name,href:'#workspaces'},{label:'Production'}]}}
            context={{origin:'Demo data',mode:identity.mode,cloud:'AWS',region:'us-east-1'}}
            page={{title:'Refund status',subtitle:'Refund status · receipt-backed answers · policy v2',primaryAction:{label:'View benchmark',onPress(){document.body.dataset.action='benchmark'}}}}
            tabs={{label:'Page tabs',selectedKey,onSelectionChange,items:${JSON.stringify(tabLabels)}.map(label=>({key:label,label,panel:null}))}}
          />;
        }
        createRoot(document.querySelector('#root')).render(<Fixture/>);
      ` : null }],
    build: { write: false, lib: { entry: input, name: 'ShellFixture', formats: ['es'] } },
  });
  const result = Array.isArray(bundle) ? bundle[0] : bundle;
  if (!('output' in result)) throw Error('Browser bundle missing');
  const js = result.output.find(asset => asset.type === 'chunk');
  const styles = result.output.find(asset => asset.type === 'asset' && asset.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !styles || styles.type !== 'asset') throw Error('Shell assets missing');
  script = js.code; css = String(styles.source);
});

for (const [i, identity] of identities.entries()) {
  for (const width of [320, 768, 1280, 1440]) {
    for (const scale of [1, 2]) {
      test(`${identity.mode} at ${width}px and ${scale * 100}% text`, async ({ page }, info) => {
        const requests: string[] = [];
        page.on('pageerror', error => { throw error; });
        const height = width === 320 ? 800 : width === 768 ? 1024 : width === 1280 ? 900 : 1116;
        await page.setViewportSize({ width, height });
        await page.route('http://shell.test/', route => route.fulfill({contentType:'text/html',body:`<!doctype html><html lang="en"><body data-fixture="${i}" style="margin:0"><div id="root"></div></body></html>`}));
        await page.goto('http://shell.test/');
        page.on('request', request => { if (/^https?:/.test(request.url()) && request.url() !== 'http://shell.test/fixture.js') requests.push(request.url()); });
        await page.addStyleTag({ content: css + `html{font-size:${scale * 100}%}` });
        await page.route('http://shell.test/fixture.js', route => route.fulfill({contentType:'text/javascript',body:script}));
        await page.addScriptTag({ url: 'http://shell.test/fixture.js', type: 'module' });
        const main = page.getByRole('main');
        await expect(main.getByRole('heading', { level: 1 })).toHaveText('Refund status');
        await page.evaluate(() => document.fonts.ready);
        const checkBounds = async () => expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        await checkBounds();
        const shell = page.locator('.ot-shell');
        const snapshot = async (state: string) => {
          const name = `${i}-${width}-${scale}x-${state}`;
          await page.screenshot({ fullPage: state === 'closed', path: info.outputPath(name + '.png') });
          const geometry = await shell.evaluate(root => [...root.querySelectorAll<HTMLElement>(
            '.ot-shell-sidebar,.ot-shell-mobile,.ot-shell-context,.ot-shell-workspace,.ot-shell-links,.ot-shell-utilities,.ot-shell-delivery,.ot-shell-profile,.ot-shell-heading,.ot-tablist,.ot-shell-drawer,h1,.ot-button,.ot-tab,.ot-badge,.ot-nav-item'
          )].filter(el => el.checkVisibility()).map(el => {
            const r = el.getBoundingClientRect(); return { cls: el.className, text: el.innerText,
              x: r.x, y: r.y, width: r.width, height: r.height };
          }));
          await writeFile(info.outputPath(name + '.json'), JSON.stringify(geometry));
          const layers = await shell.evaluate(root => {
            const dialog = root.querySelector('dialog')!;
            const modal = dialog.open;
            const area = root.getBoundingClientRect();
            const scene = { width: innerWidth, height: modal ? innerHeight : Math.max(area.height, innerHeight),
              mode: modal ? 'open' : 'closed', entries: [] as Record<string, unknown>[] };
            function walk(el: Element, parent: number) {
              const style = getComputedStyle(el);
              if (!el.checkVisibility() || el.matches('.ot-shell-skip')) return;
              const rect = el.getBoundingClientRect();
              const id = scene.entries.length;
              const row = { parent, tag: el.tagName, cls: el.className, x: rect.x, y: rect.y,
                w: rect.width, h: rect.height, fill: style.backgroundColor, color: style.color,
                border: style.borderTopWidth, stroke: style.borderTopColor, radius: style.borderTopLeftRadius,
                weight: style.fontWeight, fontSize: style.fontSize, lineHeight: style.lineHeight,
                text: el.textContent, current: el.getAttribute('aria-current') || el.getAttribute('aria-selected'),
                runs: [] as {text:string;x:number;y:number;width:number;height:number}[] };
              scene.entries.push(row);
              for (const node of el.childNodes) {
                if (node instanceof Element) walk(node, id);
                else if (node.nodeType === Node.TEXT_NODE && node.textContent?.trim()) {
                  const byLine = new Map<number, {text:string;x:number;y:number;width:number;height:number}>();
                  for (let i = 0; i < node.textContent.length; i++) {
                    const range = document.createRange();
                    range.setStart(node, i); range.setEnd(node, i + 1);
                    const glyph = range.getBoundingClientRect();
                    if (!glyph.width) continue;
                    const line = byLine.get(glyph.y) || {text:'',x:glyph.x,y:glyph.y,width:0,height:glyph.height};
                    line.text += range.toString(); line.width = glyph.right - line.x;
                    byLine.set(glyph.y, line);
                  }
                  row.runs.push(...byLine.values());
                }
              }
            }
            walk(root, -1);
            return scene;
          });
          await writeFile(info.outputPath(name + '.layers.json'), JSON.stringify(layers));
          return geometry;
        };
        const closed = await snapshot('closed');
        const sidebar = page.getByRole('complementary', { name: 'Workspace' });
        if (width < 1024) {
          await expect(sidebar).toBeHidden();
          const trigger = page.getByRole('button', { name: 'Open navigation' });
          expect((await trigger.boundingBox())!.width).toBe(width - 32);
          if (scale === 1 && i === 0) {
            expect(await trigger.boundingBox()).toEqual({ x: 16, y: 54, width: width - 32, height: 44 });
            expect(closed.find(el => el.cls === 'ot-shell-mobile')!.height).toBe(144);
          }
          await trigger.click();
          const modal = page.getByRole('dialog', { name: 'Workspace navigation' });
          await expect(modal.getByRole('button', { name: 'Close navigation' })).toBeFocused();
          await expect(modal.getByRole('navigation', { name: 'Workspace navigation' }).getByRole('link')).toHaveText(navigation);
          await snapshot('open');
          const close = modal.getByRole('button', { name: 'Close navigation' });
          await close.focus();
          await page.keyboard.press('Tab');
          await expect(modal.getByRole('link', { name: 'Overview', exact: true })).toBeFocused();
          await page.keyboard.press('Shift+Tab');
          await expect(close).toBeFocused();
          await modal.getByRole('link', { name: 'Settings' }).focus();
          await page.keyboard.press('Tab');
          await expect(close).toBeFocused();
          await page.keyboard.press('Shift+Tab');
          await expect(modal.getByRole('link', { name: 'Settings' })).toBeFocused();
          await modal.getByRole('link', { name: 'Settings' }).scrollIntoViewIfNeeded();
          await expect(modal.getByText(identity.name, { exact: true })).toBeVisible();
          await page.keyboard.press('Escape');
          await expect(trigger).toBeFocused();
          await expect(modal).toBeHidden();
          await trigger.click();
          await page.setViewportSize({ width: 1280, height });
          await expect(modal).toBeHidden();
          await expect(sidebar.getByRole('link', { name: 'Overview', exact: true })).toBeFocused();
          await page.setViewportSize({ width, height });
        } else {
          expect(closed.find(el => el.cls === 'ot-shell-sidebar')!.width).toBe(208);
          await expect(sidebar.locator('a[aria-current="page"]')).toHaveText('Overview');
          if (width === 1440 && scale === 1 && i === 0) {
            // Original 13:4 rail + 13:32 context bar. Positions are in viewport coordinates.
            const bounds = (selector: string) => shell.locator(selector).first().boundingBox();
            expect(await bounds('.ot-shell-workspace')).toEqual({ x: 16, y: 66, width: 176, height: 96 });
            expect(await bounds('.ot-shell-context')).toEqual({ x: 208, y: 0, width: 1232, height: 66 });
            expect(await bounds('.ot-badge--learning')).toEqual({ x: 804, y: 20, width: 78, height: 26 });
            expect(await bounds('.ot-badge--info')).toEqual({ x: 898, y: 20, width: 132, height: 26 });
            expect(await bounds('.ot-shell-delivery')).toEqual({ x: 16, y: 972, width: 176, height: 86 });
          }
          await expect(sidebar.getByRole('navigation', { name: 'Workspace utilities' }).getByRole('link')).toHaveText(['Environment','Usage','Settings']);
          await expect(page.getByRole('button', { name: 'Open navigation' })).toBeHidden();
        }
        await page.getByRole('link', { name: 'Skip to main content' }).focus();
        await page.keyboard.press('Enter');
        await expect(main).toBeFocused();
        await main.getByRole('button', { name: 'View benchmark' }).click();
        await expect(page.locator('body')).toHaveAttribute('data-action','benchmark');
        await main.getByRole('tab', { name: 'Benchmarks' }).click();
        await expect(main.getByRole('tab', { name: 'Benchmarks' })).toHaveAttribute('aria-selected','true');
        await checkBounds();
        expect(requests).toEqual([]);
      });
    }
  }
}

test('resizing an open utility-active drawer restores desktop utility focus', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.route('http://shell.test/', route => route.fulfill({
    contentType: 'text/html', body: '<body data-fixture="0" data-active="#Settings"><div id="root"></div></body>',
  }));
  await page.route('http://shell.test/fixture.js', route => route.fulfill({ contentType: 'text/javascript', body: script }));
  await page.goto('http://shell.test/');
  await page.addStyleTag({ content: css });
  await page.addScriptTag({ url: 'http://shell.test/fixture.js', type: 'module' });
  await page.getByRole('button', { name: 'Open navigation' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('link', { name: 'Settings' })).toHaveAttribute('aria-current', 'page');
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(dialog).toBeHidden();
  await expect(page.getByRole('link', { name: 'Settings' })).toBeFocused();
});
