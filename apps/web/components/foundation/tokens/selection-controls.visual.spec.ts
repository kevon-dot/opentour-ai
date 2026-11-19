import { test, expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { build } from 'vite';
import { join } from 'node:path';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { selectionControlCaptureInventory, selectionControlCaptureName, selectionControlFixtures as fixtures,
  type SelectionControlCaptureState } from './selection-controls.fixtures';
import tokens from '../../../styles/product-tokens.json' with { type: 'json' };

type Box = { x: number; y: number; width: number; height: number };
type Request = { control: string; next: boolean };
type Expected = { specimen: string; kind: 'checkbox' | 'switch'; label: string; helper: string; checked: boolean; state: string };

const directory = join(process.cwd(), 'apps/web/components/foundation/tokens');
const inventory = new Set(selectionControlCaptureInventory());
const desktop = { width: 1280, height: 900 };
const glyphPathSha256 = '51e06728c6c9fa2ab427a8596ec6bbd2deb5325f29a12e2ed0085db61ee8132f';
const { interaction } = fixtures;
const sha256 = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
const rgb = (hex: string) => `rgb(${hex.slice(1).match(/../g)!.map(value => parseInt(value, 16)).join(', ')})`;
const triple = (hex: string) => hex.slice(1).match(/../g)!.map(value => parseInt(value, 16));
const color = (name: keyof typeof tokens.colors) => rgb(tokens.colors[name]);
const near = (actual: number, expected: number) => expect(Math.abs(actual - expected)).toBeLessThan(0.01);

/** Every control the gallery renders, in document order, with the value and treatment its parent supplies at load. */
const expectedControls: Expected[] = [
  ...[...fixtures.checkbox, ...fixtures.suppliedCheckboxes].map(item => ({ ...item, specimen: `checkbox-${item.key}`, kind: 'checkbox' as const })),
  ...[...fixtures.switch, ...fixtures.suppliedSwitches].map(item => ({ ...item, specimen: `switch-${item.key}`, kind: 'switch' as const })),
  ...fixtures.contexts.flatMap(context => [
    ...context.checkboxes.map(item => ({ ...item, specimen: item.key, kind: 'checkbox' as const })),
    ...context.switches.map(item => ({ ...item, specimen: item.key, kind: 'switch' as const })),
  ]),
  { ...fixtures.long.checkbox, specimen: fixtures.long.checkbox.key, kind: 'checkbox' as const },
  { ...fixtures.long.switch, specimen: fixtures.long.switch.key, kind: 'switch' as const },
  { specimen: interaction.selection.key, kind: 'checkbox' as const, label: interaction.selection.label, helper: interaction.selection.helpers.false, checked: false },
  { specimen: interaction.requestOnly.key, kind: 'checkbox' as const, label: interaction.requestOnly.label, helper: interaction.requestOnly.helper, checked: false },
  { specimen: interaction.setting.key, kind: 'switch' as const, label: interaction.setting.label, helper: interaction.setting.helpers.false, checked: false },
  { specimen: interaction.preference.key, kind: 'switch' as const, label: interaction.preference.label, helper: interaction.preference.helpers.false, checked: false },
].map(({ specimen, kind, label, helper, checked, ...rest }) => ({ specimen, kind, label, helper, checked, state: 'state' in rest && rest.state ? rest.state : 'enabled' }));
const singleLine = new Set([...fixtures.checkbox, ...fixtures.suppliedCheckboxes].map(item => `checkbox-${item.key}`)
  .concat([...fixtures.switch, ...fixtures.suppliedSwitches].map(item => `switch-${item.key}`)));

let script: string, styles: string;
let artworkFiles: { off: string; on: string };
const rejected = new WeakMap<Page, string[]>();
test.beforeAll(async () => {
  const result = await build({ root: join(process.cwd(), 'apps/web'), configFile: false, envFile: false, publicDir: false,
    logLevel: 'error', oxc: { jsx: { runtime: 'automatic' } },
    build: { write: false, lib: { entry: join(directory, 'testing/selection-controls-gallery.tsx'), name: 'SelectionControlsGallery', formats: ['iife'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing gallery bundle.');
  const js = bundle.output.find(value => value.type === 'chunk');
  const css = bundle.output.find(value => value.type === 'asset' && value.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing gallery assets.');
  script = js.code; styles = String(css.source);
  // The rendered images are compared with the tracked exports, whose bytes are bound by the provenance record.
  const source = JSON.parse(await readFile(join(directory, 'selection-controls-assets/source.json'), 'utf8'));
  const [off, on] = await Promise.all(['switch-off', 'switch-on'].map(async name => {
    const bytes = await readFile(join(directory, `selection-controls-assets/${name}.svg`));
    expect(sha256(bytes)).toBe(source.files[`selection-controls-assets/${name}.svg`]);
    return bytes.toString('utf8');
  }));
  artworkFiles = { off, on };
});
test.afterEach(async ({ page }) => { expect(rejected.get(page)).toEqual([]); });

async function open(page: Page, baseURL: string | undefined, mode: string, viewport = desktop, scale = 100) {
  if (!baseURL) throw new Error('Use the supervised browser runner.');
  const requests: string[] = []; rejected.set(page, requests);
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin === baseURL && url.pathname === '/selection-controls-fixture' && request.method() === 'GET') {
      await route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Generated selection controls</title></head><body><div id="root"></div></body></html>' });
    } else { requests.push(`${request.method()}:${url.origin}${url.pathname}`); await route.abort(); }
  });
  await page.setViewportSize(viewport);
  await page.goto(`${baseURL}/selection-controls-fixture?mode=${mode}`);
  await page.addStyleTag({ content: styles + `html { font-size: ${scale}% }` });
  await page.addScriptTag({ content: script });
  await expect(page.getByRole('heading', { name: fixtures.title })).toBeVisible();
  await page.evaluate(async () => {
    await Promise.all([document.fonts.load('400 12px Geist'), document.fonts.load('500 14px Geist'), document.fonts.load('600 18px Geist')]);
    await document.fonts.ready;
    await Promise.all([...document.images].map(image => image.complete ? undefined
      : new Promise(done => { image.addEventListener('load', done, { once: true }); image.addEventListener('error', done, { once: true }); })));
  });
  expect(await page.evaluate(() => document.fonts.check('500 14px Geist'))).toBe(true);
  await expect(page.locator('.ot-selection')).toHaveCount(expectedControls.length);
  await expect(page.locator('main')).toHaveAttribute('data-mode', mode);
  await page.mouse.move(0, 0);
}

const control = (page: Page, specimen: string) => page.locator(`[data-specimen="${specimen}"] .ot-selection-control`);
const card = (page: Page, specimen: string) => page.locator(`[data-specimen="${specimen}"] .ot-selection`);
const region = (page: Page, name: string) => page.locator(`[data-region="${name}"]`);
const galleryButton = (page: Page, name: string) => page.getByRole('button', { name, exact: true });
async function expectRequests(page: Page, expected: Request[]) {
  await expect(page.locator('main')).toHaveAttribute('data-requests', JSON.stringify(expected));
  await expect(page.locator('.selection-request-count')).toHaveText(`${interaction.requestCount}: ${expected.length}`);
}

/** Integer scroll offsets keep element boxes on whole pixels for clipping and pixel sampling. */
async function reveal(target: Locator) {
  await target.evaluate(node => { scrollTo(0, Math.max(0, Math.floor(node.getBoundingClientRect().top + scrollY) - 96)); });
}

async function capture(page: Page, info: TestInfo, mode: string, viewport: { width: number }, scale: number,
  state: SelectionControlCaptureState, target?: Locator) {
  const name = selectionControlCaptureName(mode, viewport.width, scale, state);
  if (!inventory.has(name)) throw new Error(`Undeclared capture ${name}`);
  const bytes = target ? await target.screenshot({ animations: 'disabled' }) : await page.screenshot({ fullPage: true, animations: 'disabled' });
  await writeFile(info.outputPath(name), bytes);
  return { path: name, sha256: sha256(bytes) };
}
async function record(info: TestInfo, name: string, value: Record<string, unknown>) {
  await writeFile(info.outputPath(name), JSON.stringify({ ...value, fixture_kind: 'generated-presentation',
    figma_nodes: fixtures.figmaNodes, manual_screen_reader: 'NOT_RUN' }, null, 2));
}

/** Runs in the page. Describes each switch image and identifies which tracked export it actually renders. */
function describeArtwork(nodes: Element[], files: { off: string; on: string }) {
  const parse = (text: string) => {
    const root = new DOMParser().parseFromString(text, 'image/svg+xml').documentElement;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); const blank: Node[] = [];
    while (walker.nextNode()) if (!walker.currentNode.textContent?.trim()) blank.push(walker.currentNode);
    for (const node of blank) node.parentNode?.removeChild(node);
    return root;
  };
  const canonical = (root: Element) => new XMLSerializer().serializeToString(root);
  const exports = { 'switch-off.svg': canonical(parse(files.off)), 'switch-on.svg': canonical(parse(files.on)) };
  return nodes.map(node => {
    const image = node as HTMLImageElement, src = image.currentSrc || image.src, comma = src.indexOf(',');
    // The production bundle embeds each export; transport quoting and whitespace differ, the parsed document does not.
    const text = !src.startsWith('data:image/svg+xml') ? ''
      : src.slice(0, comma).endsWith(';base64') ? atob(src.slice(comma + 1)) : decodeURIComponent(src.slice(comma + 1));
    const root = text ? parse(text) : null, markup = root ? canonical(root) : '';
    const { x, y, width, height } = image.getBoundingClientRect();
    return { specimen: image.closest('[data-specimen]')?.getAttribute('data-specimen') ?? null, hidden: image.hidden,
      box: { x: x + scrollX, y: y + scrollY, width, height }, loaded: image.complete, naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight,
      embedded: text.length > 0, root: root ? [root.getAttribute('width'), root.getAttribute('height'), root.getAttribute('viewBox')] : null,
      knob: root?.querySelector('circle')?.getAttribute('cx') ?? null,
      source: Object.entries(exports).find(([, value]) => value === markup)?.[0] ?? null };
  });
}
const artwork = (page: Page) => page.locator('.ot-switch-art img').evaluateAll(describeArtwork, artworkFiles);
async function visibleArtwork(page: Page, specimen: string) {
  const images = await card(page, specimen).locator('.ot-switch-art img:not([hidden])').evaluateAll(describeArtwork, artworkFiles);
  expect(images).toHaveLength(1);
  return images[0];
}

/** Boxes are in page coordinates, so they compare equal before and after any scrolling. */
async function measurements(page: Page) {
  return page.locator('.ot-selection').evaluateAll(nodes => nodes.map(node => {
    const box = (element: Element) => { const { x, y, width, height } = element.getBoundingClientRect(); return { x: x + scrollX, y: y + scrollY, width, height }; };
    const input = node.querySelector('.ot-selection-control') as HTMLInputElement | HTMLButtonElement;
    const art = node.querySelector('.ot-selection-art')!, label = node.querySelector('.ot-selection-label') as HTMLElement;
    const helper = node.querySelector('.ot-selection-helper') as HTMLElement, row = node.querySelector('.ot-selection-row')!;
    const glyph = art.querySelector('svg'), path = glyph?.querySelector('path'), image = art.querySelector('img:not([hidden])');
    const s = getComputedStyle(node), l = getComputedStyle(label), h = getComputedStyle(helper), a = getComputedStyle(art), c = getComputedStyle(input);
    return {
      specimen: node.closest('[data-specimen]')?.getAttribute('data-specimen') ?? null,
      kind: node.classList.contains('ot-selection--checkbox') ? 'checkbox' : 'switch',
      state: ['enabled', 'disabled', 'error', 'pending'].find(value => node.classList.contains(`ot-selection--${value}`)) ?? null,
      checked: input instanceof HTMLInputElement ? input.checked : input.getAttribute('aria-checked') === 'true',
      nativeDisabled: input.disabled, ariaDisabled: input.getAttribute('aria-disabled'), ariaInvalid: input.getAttribute('aria-invalid'),
      labelText: label.textContent, helperText: helper.textContent,
      // The elements the native control is actually named and described by.
      nameSource: (input instanceof HTMLInputElement ? input.labels?.[0] : document.getElementById(input.getAttribute('aria-labelledby') ?? ''))?.textContent ?? null,
      descriptionSource: document.getElementById(input.getAttribute('aria-describedby') ?? '')?.textContent ?? null,
      card: box(node), control: box(input), row: box(row), art: box(art), label: box(label), helper: box(helper),
      background: s.backgroundColor, border: [s.borderTopWidth, s.borderTopStyle, s.borderTopColor], radius: s.borderTopLeftRadius,
      padding: [s.paddingTop, s.paddingRight, s.paddingBottom, s.paddingLeft], gap: s.rowGap, opacity: s.opacity, overflow: s.overflowX,
      labelFont: [l.fontFamily, l.fontSize, l.lineHeight, l.fontWeight, l.color],
      helperFont: [h.fontFamily, h.fontSize, h.lineHeight, h.fontWeight, h.color],
      artStyle: { background: a.backgroundColor, border: [a.borderTopWidth, a.borderTopStyle, a.borderTopColor], radius: a.borderTopLeftRadius, opacity: a.opacity },
      outline: [c.outlineStyle, c.outlineWidth, c.outlineColor, c.outlineOffset],
      glyph: glyph && path ? { ...box(glyph), stroke: getComputedStyle(path).stroke, strokeWidth: getComputedStyle(path).strokeWidth, d: path.getAttribute('d') } : null,
      image: image ? box(image) : null,
      noClip: label.scrollWidth <= label.clientWidth && helper.scrollWidth <= helper.clientWidth && label.scrollHeight <= label.clientHeight && helper.scrollHeight <= helper.clientHeight,
    };
  }));
}
type Measurement = Awaited<ReturnType<typeof measurements>>[number];
const geometry = (rows: Measurement[]) => rows.map(({ card, control, row, art, label, helper, glyph, image }) =>
  ({ card, control, row, art, label, helper, glyph: glyph && { x: glyph.x, y: glyph.y, width: glyph.width, height: glyph.height }, image }));

/** Names, descriptions and states are read through the accessibility tree; each recorded value was asserted. */
async function semantics(page: Page, expected = expectedControls) {
  return Promise.all(expected.map(async item => {
    const target = control(page, item.specimen);
    await expect(target).toHaveRole(item.kind);
    await expect(target).toHaveAccessibleName(item.label);
    await expect(target).toHaveAccessibleDescription(item.helper);
    if (item.checked) await expect(target).toBeChecked(); else await expect(target).not.toBeChecked();
    if (item.state === 'error') await expect(target).toHaveAttribute('aria-invalid', 'true'); else await expect(target).not.toHaveAttribute('aria-invalid');
    return { specimen: item.specimen, role: item.kind, accessible_name: item.label, accessible_description: item.helper,
      checked: item.checked, state: item.state };
  }));
}

async function hitTarget(page: Page, specimen: string) {
  return card(page, specimen).evaluate(node => {
    const box = (element: Element) => { const { x, y, width, height } = element.getBoundingClientRect(); return { x, y, width, height }; };
    const input = node.querySelector('.ot-selection-control')!, outer = box(node);
    return { card: outer, control: box(input), tabStops: node.querySelectorAll('a, button, input, select, textarea, [tabindex]').length,
      probes: [0.02, 0.5, 0.98].flatMap(fx => [0.05, 0.5, 0.95].map(fy =>
        document.elementFromPoint(outer.x + outer.width * fx, outer.y + outer.height * fy) === input)) };
  });
}
function expectIntegratedTarget(target: Awaited<ReturnType<typeof hitTarget>>) {
  expect(target.control).toEqual(target.card);
  expect(target.card.width).toBeGreaterThanOrEqual(44); expect(target.card.height).toBeGreaterThanOrEqual(44);
  expect(target.probes).toEqual(Array(9).fill(true));
  expect(target.tabStops).toBe(1);
}

/** Moves real keyboard focus onto the target: focus the preceding tab stop, then press Tab. */
async function keyboardFocus(page: Page, target: Locator) {
  await target.evaluate(node => {
    const stops = [...document.querySelectorAll<HTMLElement>('main button, main input')].filter(element => !element.matches(':disabled'));
    stops[stops.indexOf(node as HTMLElement) - 1].focus();
  });
  await page.keyboard.press('Tab');
  await expect(target).toBeFocused();
  expect(await target.evaluate(node => node.matches(':focus-visible'))).toBe(true);
}

async function inViewport(page: Page, box: Box): Promise<Box> {
  const [left, top] = await page.evaluate(() => [scrollX, scrollY]);
  return { ...box, x: box.x - left, y: box.y - top };
}
async function pixels(page: Page, points: { x: number; y: number }[]) {
  const shot = await page.screenshot({ animations: 'disabled' });
  return page.evaluate(async ({ data, points }) => {
    const image = new Image(); image.src = `data:image/png;base64,${data}`; await image.decode();
    const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext('2d')!; context.drawImage(image, 0, 0);
    return points.map(({ x, y }) => [...context.getImageData(x, y, 1, 1).data].slice(0, 3));
  }, { data: shot.toString('base64'), points });
}
/** Samples the five pixels outside the middle of each card edge: a 2px gap, the 2px ring, then the page again. */
async function ringSamples(page: Page, pageBox: Box) {
  const box = await inViewport(page, pageBox);
  for (const value of Object.values(box)) expect(Number.isInteger(value)).toBe(true);
  const offsets = [1, 2, 3, 4, 5], midX = box.x + Math.floor(box.width / 2), midY = box.y + Math.floor(box.height / 2);
  const sides = {
    top: offsets.map(k => ({ x: midX, y: box.y - k })), bottom: offsets.map(k => ({ x: midX, y: box.y + box.height - 1 + k })),
    left: offsets.map(k => ({ x: box.x - k, y: midY })), right: offsets.map(k => ({ x: box.x + box.width - 1 + k, y: midY })),
  };
  const values = await pixels(page, [...Object.values(sides).flat(), { x: box.x, y: midY }, { x: box.x + 6, y: midY }]);
  return { top: values.slice(0, 5), bottom: values.slice(5, 10), left: values.slice(10, 15), right: values.slice(15, 20),
    border: values[20], surface: values[21] };
}
async function systemColors(page: Page) {
  return page.evaluate(() => {
    const swatch = document.createElement('span'); swatch.style.forcedColorAdjust = 'none'; document.body.append(swatch);
    const names = ['Canvas', 'CanvasText', 'Highlight', 'HighlightText', 'GrayText'] as const;
    const result = Object.fromEntries(names.map(name => { swatch.style.color = name; return [name, getComputedStyle(swatch).color]; })) as Record<typeof names[number], string>;
    swatch.remove(); return result;
  });
}
const channels = (value: string) => value.match(/\d+/g)!.slice(0, 3).map(Number);
const composite = (foreground: string, background: string) => {
  const values = foreground.match(/[\d.]+/g)!.map(Number), alpha = values[3] ?? 1;
  return channels(background).map((channel, index) => Math.round(values[index] * alpha + channel * (1 - alpha)));
};

for (const mode of fixtures.modes) {
  for (const viewport of fixtures.viewports) for (const scale of fixtures.scales) {
    test(`${mode} checkbox and switch at ${viewport.width}px ${scale}% text keep exact text, native geometry, colours and a 44px target`, async ({ page, baseURL }, info) => {
      await open(page, baseURL, mode, viewport, scale);
      const factor = scale / 100, rows = await measurements(page), images = await artwork(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
      expect(rows.map(row => row.specimen)).toEqual(expectedControls.map(item => item.specimen));
      for (const [index, row] of rows.entries()) {
        const item = expectedControls[index], failed = item.state === 'error';
        expect([row.kind, row.state, row.checked, row.labelText, row.helperText]).toEqual([item.kind, item.state, item.checked, item.label, item.helper]);
        expect([row.nameSource, row.descriptionSource]).toEqual([item.label, item.helper]);
        expect([row.nativeDisabled, row.ariaDisabled, row.ariaInvalid]).toEqual([item.state === 'disabled', item.state === 'pending' ? 'true' : null, item.state === 'error' ? 'true' : null]);
        // The native control is the whole card: one integrated target of at least 44 by 44.
        expect(row.control).toEqual(row.card);
        expect(row.card.width).toBeGreaterThanOrEqual(44); expect(row.card.height).toBeGreaterThanOrEqual(44);
        expect(row.card.x).toBeGreaterThanOrEqual(0); expect(row.card.x + row.card.width).toBeLessThanOrEqual(viewport.width);
        expect([row.border, row.radius, row.padding, row.gap, row.overflow]).toEqual([['1px', 'solid', color('border-control')], '8px', ['16px', '16px', '16px', '16px'], '8px', 'visible']);
        expect(row.background).toBe(failed ? color('failure-background') : color('surface-base'));
        expect(row.opacity).toBe(item.state === 'disabled' ? '0.55' : '1');
        expect(row.labelFont).toEqual(['Geist, sans-serif', `${14 * factor}px`, `${20 * factor}px`, '500', failed ? color('failure-text') : color('text-primary')]);
        expect(row.helperFont).toEqual(['Geist, sans-serif', `${12 * factor}px`, `${18 * factor}px`, '400', failed ? color('failure-text') : color('text-secondary')]);
        // Artwork never scales with text; it stays centred on the first label line.
        expect([row.art.width, row.art.height]).toEqual([item.kind === 'checkbox' ? 20 : 38, 20]);
        near(row.art.x - row.card.x, 17); near(row.row.y - row.card.y, 17);
        near(row.art.y - row.card.y, 17 + (20 * factor - 20) / 2);
        near(row.label.x - (row.art.x + row.art.width), 12);
        near(row.helper.x - row.card.x, 17); near(row.helper.y - (row.row.y + row.row.height), 8);
        near(row.card.y + row.card.height - (row.helper.y + row.helper.height), 17);
        expect(row.label.x + row.label.width).toBeLessThanOrEqual(row.card.x + row.card.width - 17 + 0.01);
        expect(row.helper.x + row.helper.width).toBeLessThanOrEqual(row.card.x + row.card.width - 15 + 0.01);
        expect(row.noClip).toBe(true);
        expect(row.outline[0]).toBe('none');
        if (item.kind === 'checkbox') {
          expect(row.artStyle).toEqual({ background: item.checked ? color('action-primary') : color('surface-base'),
            border: ['1px', 'solid', color('border-control')], radius: '4px', opacity: '1' });
          expect(row.image).toBeNull(); expect(row.glyph !== null).toBe(item.checked);
          if (row.glyph) {
            expect([row.glyph.width, row.glyph.height]).toEqual([16, 16]);
            near(row.glyph.x - row.art.x, 2); near(row.glyph.y - row.art.y, 2);
            expect([row.glyph.stroke, row.glyph.strokeWidth, sha256(row.glyph.d!)]).toEqual(['rgb(30, 30, 30)', '1.6px', glyphPathSha256]);
          }
        } else {
          expect(row.glyph).toBeNull(); expect(row.image).toEqual(row.art);
        }
        if (singleLine.has(item.specimen) && scale === 100) {
          expect([row.card.width, row.card.height]).toEqual([viewport.width === 320 ? 272 : 360, 80]);
          if (viewport.width !== 320) expect(row.label.width).toBe(270);
        }
      }
      // Both exports stay mounted per switch; only the one for the supplied value is shown, at its native 38 by 20.
      const switches = expectedControls.filter(item => item.kind === 'switch');
      expect(images).toHaveLength(switches.length * 2);
      for (const [index, item] of switches.entries()) {
        const [off, on] = images.slice(index * 2, index * 2 + 2);
        expect([off.specimen, on.specimen]).toEqual([item.specimen, item.specimen]);
        expect([off.hidden, on.hidden]).toEqual([item.checked, !item.checked]);
        expect([off.source, off.knob, on.source, on.knob]).toEqual(['switch-off.svg', '10', 'switch-on.svg', '28']);
        for (const image of [off, on]) {
          expect([image.embedded, image.loaded, image.naturalWidth, image.naturalHeight, image.root]).toEqual([true, true, 38, 20, ['38', '20', '0 0 38 20']]);
        }
        const shown = item.checked ? on : off;
        expect([shown.box.width, shown.box.height]).toEqual([38, 20]);
      }
      const contexts = await page.locator('[data-context]').evaluateAll(nodes => nodes.map(node => {
        const { width, height } = node.getBoundingClientRect(), style = getComputedStyle(node);
        const title = getComputedStyle(node.querySelector('h2')!), description = getComputedStyle(node.querySelector('p')!);
        return { key: node.getAttribute('data-context'), width, height, background: style.backgroundColor,
          border: [style.borderTopWidth, style.borderTopStyle, style.borderTopColor], radius: style.borderTopLeftRadius, padding: style.paddingTop, gap: style.rowGap,
          title: [node.querySelector('h2')!.textContent, title.fontSize, title.lineHeight, title.fontWeight, title.color],
          description: [node.querySelector('p')!.textContent, description.fontSize, description.lineHeight, description.fontWeight, description.color] };
      }));
      expect(contexts.map(context => context.key)).toEqual(fixtures.contexts.map(context => context.key));
      for (const [index, context] of contexts.entries()) {
        const source = fixtures.contexts[index];
        expect([context.background, context.border, context.radius, context.padding, context.gap]).toEqual([color('surface-canvas'), ['1px', 'solid', color('border-default')], '12px', '24px', '16px']);
        expect(context.title).toEqual([source.title, `${18 * factor}px`, `${26 * factor}px`, '600', color('text-primary')]);
        expect(context.description).toEqual([source.description, `${14 * factor}px`, `${22 * factor}px`, '400', color('text-secondary')]);
        if (viewport.width >= 768 && scale === 100) {
          expect([context.width, context.height]).toEqual([596, source.checkboxes.length + source.switches.length === 1 ? 210 : 306]);
        }
      }
      const observed = await semantics(page);
      // Recorded as observed, not asserted: the tree text format belongs to the browser tooling.
      const tree = viewport.width === 1280 && scale === 100 ? await page.locator('main').ariaSnapshot() : undefined;
      const captures = [await capture(page, info, mode, viewport, scale, 'gallery')];
      if (viewport.width === 1280 && scale === 100) {
        for (const key of ['unchecked', 'checked', 'disabled', 'error'] as const) captures.push(await capture(page, info, mode, viewport, scale, `checkbox-${key}`, card(page, `checkbox-${key}`)));
        for (const key of ['off', 'on', 'disabled', 'pending'] as const) captures.push(await capture(page, info, mode, viewport, scale, `switch-${key}`, card(page, `switch-${key}`)));
        for (const key of ['terms-loading', 'terms-error', 'automation', 'hover'] as const) captures.push(await capture(page, info, mode, viewport, scale, `context-${key}`, page.locator(`[data-context="${key}"]`)));
      }
      if (viewport.width === 320 && scale === 200) {
        for (const key of ['checkbox-family', 'switch-family', 'contexts'] as const) captures.push(await capture(page, info, mode, viewport, scale, key, region(page, key)));
      }
      await expectRequests(page, []);
      await record(info, 'selection-controls-geometry.json', { mode, viewport, scale, controls: rows, artwork: images, contexts, semantics: observed, accessibility_tree: tree, captures, requests: [] });
    });
  }

  test(`${mode} checkbox requests one change per pointer or Space activation anywhere on its card`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    const { selection, requestOnly } = interaction, target = control(page, selection.key), surface = card(page, selection.key);
    await reveal(surface);
    await semantics(page, [{ specimen: selection.key, kind: 'checkbox', label: selection.label, helper: selection.helpers.false, checked: false, state: 'enabled' }]);
    const hit = await hitTarget(page, selection.key);
    expectIntegratedTarget(hit);
    const log: Request[] = [];
    // Pointer over the helper text, far from the indicator.
    await target.click({ position: { x: hit.card.width - 24, y: hit.card.height - 12 } });
    log.push({ control: selection.key, next: true }); await expectRequests(page, log);
    await expect(target).toBeChecked(); await expect(target).toHaveAccessibleDescription(selection.helpers.true);
    await expect(surface.locator('.ot-checkbox-glyph')).toHaveCount(1);
    // Pointer over the indicator itself.
    await target.click({ position: { x: 27, y: 27 } });
    log.push({ control: selection.key, next: false }); await expectRequests(page, log);
    await expect(target).not.toBeChecked(); await expect(target).toHaveAccessibleDescription(selection.helpers.false);
    await expect(surface.locator('.ot-checkbox-glyph')).toHaveCount(0);
    await galleryButton(page, interaction.before).focus(); await page.keyboard.press('Tab');
    await expect(target).toBeFocused();
    await page.keyboard.press('Space');
    log.push({ control: selection.key, next: true }); await expectRequests(page, log);
    await expect(target).toBeChecked(); await expect(target).toBeFocused();
    // A native checkbox is not activated by Enter.
    await page.keyboard.press('Enter');
    await expectRequests(page, log); await expect(target).toBeChecked(); await expect(target).toBeFocused();
    await page.keyboard.press('Space');
    log.push({ control: selection.key, next: false }); await expectRequests(page, log);
    await expect(target).not.toBeChecked(); await expect(target).toBeFocused();
    await expect(target).toHaveAccessibleName(selection.label);
    const image = await capture(page, info, mode, desktop, 100, 'checkbox-interaction', region(page, 'interaction'));
    // Neither the indicator nor the helper is a tab stop: the next stop is the next native control.
    await page.keyboard.press('Tab');
    await expect(control(page, requestOnly.key)).toBeFocused();
    await record(info, 'selection-controls-checkbox-interaction.json', { mode, hit_target: hit, requests: log,
      activations: ['pointer:helper', 'pointer:indicator', 'keyboard:Space', 'keyboard:Enter (no request)', 'keyboard:Space'],
      accessible_name: selection.label, captures: [image] });
  });

  test(`${mode} switch requests one change per pointer, Space or Enter activation anywhere on its card`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    const { setting, requestOnly, preference } = interaction, target = control(page, setting.key), surface = card(page, setting.key);
    await reveal(surface);
    await semantics(page, [{ specimen: setting.key, kind: 'switch', label: setting.label, helper: setting.helpers.false, checked: false, state: 'enabled' }]);
    await expect(target).toHaveAttribute('aria-checked', 'false'); await expect(target).toHaveAttribute('type', 'button');
    expect((await visibleArtwork(page, setting.key)).source).toBe('switch-off.svg');
    const hit = await hitTarget(page, setting.key);
    expectIntegratedTarget(hit);
    const log: Request[] = [], artworkSeen: (string | null)[] = [];
    const expectValue = async (next: boolean) => {
      log.push({ control: setting.key, next }); await expectRequests(page, log);
      await expect(target).toHaveAttribute('aria-checked', String(next));
      await expect(target).toHaveAccessibleDescription(setting.helpers[`${next}`]);
      const shown = await visibleArtwork(page, setting.key);
      expect([shown.source, shown.knob, shown.box.width, shown.box.height]).toEqual([next ? 'switch-on.svg' : 'switch-off.svg', next ? '28' : '10', 38, 20]);
      artworkSeen.push(shown.source);
    };
    await target.click({ position: { x: hit.card.width - 24, y: hit.card.height - 12 } }); await expectValue(true);
    await target.click({ position: { x: 36, y: 27 } }); await expectValue(false);
    await control(page, requestOnly.key).focus(); await page.keyboard.press('Tab');
    await expect(target).toBeFocused();
    await page.keyboard.press('Space'); await expectValue(true); await expect(target).toBeFocused();
    await page.keyboard.press('Enter'); await expectValue(false); await expect(target).toBeFocused();
    await page.keyboard.press('Enter'); await expectValue(true); await expect(target).toBeFocused();
    await expect(target).toHaveAccessibleName(setting.label);
    const image = await capture(page, info, mode, desktop, 100, 'switch-interaction', region(page, 'interaction'));
    await page.keyboard.press('Tab');
    await expect(control(page, preference.key)).toBeFocused();
    await record(info, 'selection-controls-switch-interaction.json', { mode, hit_target: hit, requests: log, artwork: artworkSeen,
      activations: ['pointer:helper', 'pointer:artwork', 'keyboard:Space', 'keyboard:Enter', 'keyboard:Enter'],
      accessible_name: setting.label, captures: [image] });
  });

  test(`${mode} only a parent rerender changes the value, and unchanged rerenders preserve nodes and text without requests`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    const { selection, requestOnly } = interaction, held = control(page, requestOnly.key), committed = control(page, selection.key);
    await reveal(card(page, selection.key));
    // A request the parent does not apply leaves the supplied value, and each activation still asks for its opposite.
    const log: Request[] = [];
    for (const activate of [() => held.click(), () => held.click(), () => page.keyboard.press('Space')]) {
      await activate();
      log.push({ control: requestOnly.key, next: true }); await expectRequests(page, log);
      await expect(held).not.toBeChecked();
      expect(await held.evaluate(node => (node as HTMLInputElement).checked)).toBe(false);
      await expect(card(page, requestOnly.key).locator('.ot-checkbox-glyph')).toHaveCount(0);
      await expect(held).toHaveAccessibleDescription(requestOnly.helper);
    }
    await expect(held).toBeFocused();
    // A parent update changes the value without any request from the control.
    await galleryButton(page, interaction.parentUpdate).click();
    await expect(committed).toBeChecked(); await expect(committed).toHaveAccessibleDescription(selection.helpers.true);
    await expect(card(page, selection.key).locator('.ot-checkbox-glyph')).toHaveCount(1);
    await expectRequests(page, log);
    const identifiers = () => page.locator('.ot-selection').evaluateAll(nodes => nodes.map(node => {
      const ids = [...node.querySelectorAll('[id]')].map(element => element.id);
      const references = [...node.querySelectorAll('[for], [aria-labelledby], [aria-describedby]')]
        .flatMap(element => ['for', 'aria-labelledby', 'aria-describedby'].map(name => element.getAttribute(name)).filter((value): value is string => value !== null));
      return { ids, resolved: references.length >= 2 && references.every(reference => ids.includes(reference)) };
    }));
    const before = await identifiers(), allIds = before.flatMap(item => item.ids);
    expect(new Set(allIds).size).toBe(allIds.length);
    expect(before.every(item => item.resolved)).toBe(true);
    await page.evaluate(() => {
      const nodes = [...document.querySelectorAll('.ot-selection')];
      const probe = { contentMutations: 0, attributeWrites: 0, nodes, markup: nodes.map(node => node.outerHTML) };
      (window as typeof window & { selectionProbe: typeof probe }).selectionProbe = probe;
      // React may write an unchanged native input attribute. Record those writes separately;
      // node identity, final markup and text mutations prove the rendered value stays unchanged.
      const observer = new MutationObserver(records => {
        probe.attributeWrites += records.filter(record => record.type === 'attributes').length;
        probe.contentMutations += records.filter(record => record.type !== 'attributes').length;
      });
      for (const node of nodes) observer.observe(node, { childList: true, attributes: true, characterData: true, subtree: true });
    });
    const states = await measurements(page);
    for (let index = 1; index <= 5; index++) {
      await galleryButton(page, interaction.rerender).dispatchEvent('click');
      await expect(page.locator('main')).toHaveAttribute('data-renders', String(index));
      await expect(galleryButton(page, interaction.parentUpdate)).toBeFocused();
    }
    await expectRequests(page, log);
    expect(await identifiers()).toEqual(before);
    expect(await measurements(page)).toEqual(states);
    const result = await page.evaluate(() => {
      const probe = (window as typeof window & { selectionProbe: { contentMutations: number; attributeWrites: number; nodes: Element[]; markup: string[] } }).selectionProbe;
      const nodes = [...document.querySelectorAll('.ot-selection')];
      return { contentMutations: probe.contentMutations, attributeWrites: probe.attributeWrites,
        nodesUnchanged: nodes.length === probe.nodes.length && nodes.every((node, index) => node === probe.nodes[index]),
        markupUnchanged: nodes.every((node, index) => node.outerHTML === probe.markup[index]),
        announcements: document.querySelectorAll('[role="status"], [role="alert"], [role="log"], [aria-live], output').length,
        localKeys: Object.keys(localStorage), sessionKeys: Object.keys(sessionStorage) };
    });
    expect(result).toMatchObject({ contentMutations: 0, nodesUnchanged: true, markupUnchanged: true, announcements: 0, localKeys: [], sessionKeys: [] });
    const image = await capture(page, info, mode, desktop, 100, 'controlled-rerender', region(page, 'interaction'));
    await record(info, 'selection-controls-controlled-rerender.json', { mode, requests: log, unchanged_rerenders: 5, result,
      identifiers: before, requests_rejected: rejected.get(page), captures: [image] });
  });

  test(`${mode} disabled and error controls keep their name, visible reason and native blocking`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    const blocked = expectedControls.filter(item => item.state === 'disabled');
    expect(blocked.map(item => item.specimen)).toEqual(['checkbox-disabled', 'checkbox-disabled-checked', 'switch-disabled', 'switch-disabled-on',
      'terms-loading', 'automation-disabled']);
    const observed = await semantics(page, expectedControls.filter(item => item.state === 'disabled' || item.state === 'error'));
    for (const item of blocked) {
      const target = control(page, item.specimen);
      await expect(target).toBeDisabled();
      expect(await target.evaluate(node => [(node as HTMLInputElement).disabled, node.getAttribute('aria-disabled')])).toEqual([true, null]);
      await expect(card(page, item.specimen)).toHaveCSS('opacity', '0.55');
      // The reason stays visible inside the card and is the control's description, never its name.
      const helper = card(page, item.specimen).locator('.ot-selection-helper');
      await expect(helper).toBeVisible(); await expect(helper).toHaveText(item.helper);
      expect(await helper.getAttribute('id')).toBe(await target.getAttribute('aria-describedby'));
      await reveal(card(page, item.specimen));
      await target.click({ force: true });
      if (item.checked) await expect(target).toBeChecked(); else await expect(target).not.toBeChecked();
    }
    await expectRequests(page, []);
    expect((await visibleArtwork(page, 'switch-disabled')).source).toBe('switch-off.svg');
    expect((await visibleArtwork(page, 'switch-disabled-on')).source).toBe('switch-on.svg');
    await expect(card(page, 'checkbox-disabled-checked').locator('.ot-checkbox-glyph')).toHaveCount(1);
    // Error stays operable: it is a native invalid checkbox whose helper is the visible error text.
    const log: Request[] = [];
    for (const specimen of ['checkbox-error', 'terms-error']) {
      const target = control(page, specimen), surface = card(page, specimen);
      await expect(target).toBeEnabled();
      await expect(surface).toHaveCSS('background-color', color('failure-background'));
      await expect(surface.locator('.ot-selection-label')).toHaveCSS('color', color('failure-text'));
      await expect(surface.locator('.ot-selection-helper')).toHaveCSS('color', color('failure-text'));
      await expect(surface.locator('.ot-selection-helper')).toBeVisible();
      await reveal(surface); await target.click();
      log.push({ control: specimen, next: true }); await expectRequests(page, log);
      await expect(target).not.toBeChecked(); await expect(target).toHaveAttribute('aria-invalid', 'true');
    }
    // Keyboard order visits every operable control and gallery button once, and no disabled control, helper or glyph.
    const stops = await page.evaluate(() => [...document.querySelectorAll('main button, main input')].filter(element => !element.matches(':disabled'))
      .map(element => element.closest('[data-specimen]')?.getAttribute('data-specimen') ?? element.textContent));
    expect(stops).toHaveLength(expectedControls.length - blocked.length + 6);
    const focused = () => page.evaluate(() => document.activeElement?.closest('[data-specimen]')?.getAttribute('data-specimen')
      ?? document.activeElement?.textContent ?? null);
    await control(page, expectedControls[0].specimen).focus();
    const visited = [await focused()];
    for (let index = 1; index < stops.length; index++) { await page.keyboard.press('Tab'); visited.push(await focused()); }
    expect(visited).toEqual(stops);
    expect(new Set(visited).size).toBe(visited.length);
    for (const item of blocked) expect(visited).not.toContain(item.specimen);
    await reveal(region(page, 'contexts'));
    const image = await capture(page, info, mode, desktop, 100, 'disabled-error', region(page, 'contexts'));
    await record(info, 'selection-controls-disabled-error.json', { mode, semantics: observed, forced_activations_of_disabled: blocked.length,
      requests: log, keyboard_order: visited, captures: [image] });
  });

  test(`${mode} pending switch keeps the supplied value and focus while repeated requests are suppressed`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    const { preference, setting } = interaction, target = control(page, preference.key), surface = card(page, preference.key);
    await reveal(surface);
    const pendingState = async (checked: boolean) => {
      await expect(target).toHaveAttribute('aria-disabled', 'true'); await expect(target).toHaveAttribute('aria-busy', 'true');
      await expect(target).toHaveAttribute('aria-checked', String(checked));
      await expect(target).toHaveAccessibleName(preference.label); await expect(target).toHaveAccessibleDescription(preference.pendingHelper);
      await expect(surface.locator('.ot-selection-helper')).toBeVisible();
      // Pending is not native disabling: the initiating control keeps focus.
      expect(await target.evaluate(node => (node as HTMLButtonElement).disabled)).toBe(false);
      await expect(target).toBeFocused();
      expect((await visibleArtwork(page, preference.key)).source).toBe(checked ? 'switch-on.svg' : 'switch-off.svg');
    };
    const settled = async (checked: boolean) => {
      await expect(target).not.toHaveAttribute('aria-disabled'); await expect(target).not.toHaveAttribute('aria-busy');
      await expect(target).toHaveAttribute('aria-checked', String(checked));
      await expect(target).toHaveAccessibleDescription(preference.helpers[`${checked}`]);
      await expect(target).toBeFocused();
    };
    await control(page, setting.key).focus(); await page.keyboard.press('Tab');
    await expect(target).toBeFocused();
    await page.keyboard.press('Space');
    const log: Request[] = [{ control: preference.key, next: true }];
    await expectRequests(page, log); await pendingState(false);
    // Keyboard and pointer repeats are ignored while the caller reports pending.
    await page.keyboard.press('Space'); await page.keyboard.press('Enter'); await target.click({ force: true }); await target.click({ force: true });
    await expectRequests(page, log); await pendingState(false);
    const image = await capture(page, info, mode, desktop, 100, 'pending', region(page, 'interaction'));
    await pendingState(false);
    // The generated parent applies the request; nothing here asserts that a provider accepted it.
    await galleryButton(page, interaction.apply).dispatchEvent('click');
    await settled(true); await expectRequests(page, log);
    await page.keyboard.press('Enter');
    log.push({ control: preference.key, next: false }); await expectRequests(page, log); await pendingState(true);
    await page.keyboard.press('Enter'); await page.keyboard.press('Space'); await target.click({ force: true });
    await expectRequests(page, log); await pendingState(true);
    await galleryButton(page, interaction.discard).dispatchEvent('click');
    await settled(true); await expectRequests(page, log);
    // Static pending specimens show either supplied value and never request a change.
    for (const [specimen, checked] of [['switch-pending', false], ['switch-pending-on', true]] as const) {
      const fixed = control(page, specimen);
      await reveal(card(page, specimen));
      await expect(fixed).toHaveAttribute('aria-checked', String(checked)); await expect(fixed).toHaveAttribute('aria-disabled', 'true');
      await expect(fixed).toHaveAccessibleDescription('Saving preference…');
      await fixed.click({ force: true }); await expect(fixed).toBeFocused(); await page.keyboard.press('Space'); await page.keyboard.press('Enter');
      await expect(fixed).toHaveAttribute('aria-checked', String(checked));
      expect((await visibleArtwork(page, specimen)).source).toBe(checked ? 'switch-on.svg' : 'switch-off.svg');
    }
    await expectRequests(page, log);
    expect(await page.evaluate(() => document.querySelectorAll('[role="status"], [role="alert"], [role="log"], [aria-live], output').length)).toBe(0);
    await record(info, 'selection-controls-pending.json', { mode, requests: log, suppressed_activations: 13,
      provider_acceptance: 'NOT_ASSERTED', captures: [image] });
  });

  test(`${mode} real hover and keyboard focus change no value, and the focus ring is complete without moving layout`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    const action = triple(tokens.colors['action-primary']), base = triple(tokens.colors['surface-base']);
    const subtle = triple(tokens.colors['action-subtle']), edge = triple(tokens.colors['border-control']);
    const rest = await measurements(page), captures = [], observations: Record<string, unknown> = {};
    for (const kind of ['checkbox', 'switch'] as const) {
      const specimen = `${kind}-focus`, target = control(page, specimen), surface = card(page, specimen);
      await reveal(surface);
      await keyboardFocus(page, target);
      const focused = await measurements(page), row = focused.find(item => item.specimen === specimen)!;
      // The ring is an outline around the unchanged 360 by 80 card: no box anywhere on the page moves.
      expect(geometry(focused)).toEqual(geometry(rest));
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
      expect([row.card.width, row.card.height]).toEqual([360, 80]);
      expect(row.outline).toEqual(['solid', '2px', color('action-primary'), '2px']);
      expect(row.border).toEqual(['1px', 'solid', color('border-control')]);
      expect(row.background).toBe(color('surface-base'));
      const ring = await ringSamples(page, row.card);
      for (const side of ['top', 'bottom', 'left', 'right'] as const) expect(ring[side]).toEqual([base, base, action, action, base]);
      expect([ring.border, ring.surface]).toEqual([edge, base]);
      captures.push(await capture(page, info, mode, desktop, 100, `${kind}-focus`, page.locator(`[data-specimen="${specimen}"]`)));
      observations[`${kind}_focus`] = { card: row.card, outline: row.outline, ring };
      await expect(target).toBeFocused(); await expect(surface).toBeVisible();
    }
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    for (const kind of ['checkbox', 'switch'] as const) {
      const specimen = `${kind}-hover`, surface = card(page, specimen);
      await reveal(surface);
      await control(page, specimen).hover();
      await expect(surface).toHaveCSS('background-color', color('action-subtle'));
      const hovered = await measurements(page), row = hovered.find(item => item.specimen === specimen)!;
      expect(geometry(hovered)).toEqual(geometry(rest));
      expect(hovered.map(item => item.checked)).toEqual(rest.map(item => item.checked));
      expect([row.outline[0], row.border, row.opacity]).toEqual(['none', ['1px', 'solid', color('border-control')], '1']);
      const seen = await inViewport(page, row.card);
      for (const value of Object.values(seen)) expect(Number.isInteger(value)).toBe(true);
      const [inside, border] = await pixels(page, [{ x: seen.x + 6, y: seen.y + 6 }, { x: seen.x, y: seen.y + 40 }]);
      expect([inside, border]).toEqual([subtle, edge]);
      captures.push(await capture(page, info, mode, desktop, 100, `${kind}-hover`, page.locator(`[data-specimen="${specimen}"]`)));
      observations[`${kind}_hover`] = { card: row.card, background: row.background, pixel: inside };
    }
    // Native product frame 166:10053: each control is hovered in turn; neither accepts terms nor changes a preference.
    const frame = page.locator('[data-context="hover"]');
    await reveal(frame);
    for (const [specimen, state] of [['hover-terms', 'context-hover-checkbox'], ['hover-automation', 'context-hover-switch']] as const) {
      await control(page, specimen).hover();
      await expect(card(page, specimen)).toHaveCSS('background-color', color('action-subtle'));
      await expect(control(page, specimen)).not.toBeChecked();
      captures.push(await capture(page, info, mode, desktop, 100, state, frame));
    }
    // Blocked treatments give no hover affordance.
    for (const specimen of ['checkbox-disabled', 'switch-disabled', 'switch-pending', 'checkbox-error']) {
      await reveal(card(page, specimen)); await control(page, specimen).hover({ force: true });
      await expect(card(page, specimen)).toHaveCSS('background-color', specimen === 'checkbox-error' ? color('failure-background') : color('surface-base'));
    }
    await page.mouse.move(0, 0);
    expect((await measurements(page)).map(item => [item.specimen, item.checked])).toEqual(rest.map(item => [item.specimen, item.checked]));
    await expectRequests(page, []);
    await record(info, 'selection-controls-hover-focus.json', { mode, observations, requests: [], layout_shift: 'none', captures });
  });

  test(`${mode} forced colors keep text, boundaries, state shapes and a system focus ring`, async ({ page, baseURL }, info) => {
    await page.emulateMedia({ forcedColors: 'none' });
    await open(page, baseURL, mode);
    const before = await measurements(page), artBefore = await artwork(page);
    await page.emulateMedia({ forcedColors: 'active' });
    expect(await page.evaluate(() => matchMedia('(forced-colors: active)').matches)).toBe(true);
    const system = await systemColors(page), after = await measurements(page), artAfter = await artwork(page);
    expect(geometry(after)).toEqual(geometry(before));
    // Switch artwork is an unedited image: its off and on shapes, size and source are identical under forced colors.
    expect(artAfter).toEqual(artBefore);
    for (const [index, row] of after.entries()) {
      const item = expectedControls[index], grey = item.state === 'disabled', text = grey ? system.GrayText : system.CanvasText;
      expect([row.labelText, row.helperText, row.checked]).toEqual([item.label, item.helper, item.checked]);
      expect([row.background, row.border, row.opacity]).toEqual([system.Canvas, ['1px', 'solid', text], '1']);
      expect([row.labelFont[4], row.helperFont[4]]).toEqual([text, text]);
      if (item.kind === 'checkbox') {
        // Selection is a filled indicator carrying the same mapped glyph, never colour alone.
        expect(row.artStyle.background).toBe(!item.checked ? system.Canvas : grey ? system.GrayText : system.Highlight);
        expect(row.artStyle.border).toEqual(['1px', 'solid', text]);
        expect(row.glyph !== null).toBe(item.checked);
        if (row.glyph) expect([row.glyph.stroke, row.glyph.strokeWidth, sha256(row.glyph.d!)]).toEqual([grey ? system.Canvas : system.HighlightText, '1.6px', glyphPathSha256]);
      } else expect(row.artStyle.opacity).toBe(grey ? '0.55' : '1');
    }
    const captures = [await capture(page, info, mode, desktop, 100, 'forced-colors')];
    await reveal(card(page, 'switch-focus'));
    await keyboardFocus(page, control(page, 'switch-focus'));
    // System Highlight can change when keyboard focus activates the page.
    const focusedSystem = await systemColors(page);
    const focused = (await measurements(page)).find(item => item.specimen === 'switch-focus')!;
    expect(focused.outline).toEqual(['solid', '2px', focusedSystem.Highlight, '2px']);
    expect(focused.card).toEqual(before.find(item => item.specimen === 'switch-focus')!.card);
    const ring = await ringSamples(page, focused.card), canvas = channels(focusedSystem.Canvas), highlight = composite(focusedSystem.Highlight, focusedSystem.Canvas);
    for (const side of ['top', 'bottom', 'left', 'right'] as const) expect(ring[side]).toEqual([canvas, canvas, highlight, highlight, canvas]);
    expect(ring.border).toEqual(channels(focusedSystem.CanvasText));
    captures.push(await capture(page, info, mode, desktop, 100, 'forced-colors-focus', page.locator('[data-specimen="switch-focus"]')));
    await record(info, 'selection-controls-forced-colors.json', { mode, system, before, after, artwork: artAfter, focus: { system: focusedSystem, outline: focused.outline, ring },
      switch_artwork: 'Unedited image exports keep authored colours; state shape is the knob position.', captures });
  });

  test(`${mode} reduced motion is identical because every state is static`, async ({ page, baseURL }, info) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await open(page, baseURL, mode);
    const { selection, setting } = interaction;
    await reveal(card(page, selection.key));
    await control(page, selection.key).click(); await control(page, setting.key).click();
    await expect(control(page, selection.key)).toBeChecked(); await expect(control(page, setting.key)).toHaveAttribute('aria-checked', 'true');
    const motion = () => page.evaluate(() => ({ animations: document.getAnimations().length,
      moving: [...document.querySelectorAll('.ot-selection, .ot-selection *')].filter(node => {
        const style = getComputedStyle(node);
        return style.animationName !== 'none' || style.transitionDuration.split(',').some(value => parseFloat(value) !== 0);
      }).length }));
    expect(await motion()).toEqual({ animations: 0, moving: 0 });
    await page.mouse.move(0, 0); await page.evaluate(() => scrollTo(0, 0));
    const before = await measurements(page), original = await page.screenshot({ fullPage: true, animations: 'disabled' });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
    expect(await motion()).toEqual({ animations: 0, moving: 0 });
    expect(await measurements(page)).toEqual(before);
    const reduced = await page.screenshot({ fullPage: true, animations: 'disabled' });
    expect(reduced.equals(original)).toBe(true);
    await control(page, selection.key).click(); await control(page, setting.key).click();
    await expect(control(page, selection.key)).not.toBeChecked(); await expect(control(page, setting.key)).toHaveAttribute('aria-checked', 'false');
    expect(await motion()).toEqual({ animations: 0, moving: 0 });
    await page.mouse.move(0, 0); await page.evaluate(() => scrollTo(0, 0));
    const image = await capture(page, info, mode, desktop, 100, 'reduced-motion');
    await record(info, 'selection-controls-reduced-motion.json', { mode, motion: await motion(), identical_to_no_preference: true,
      requests: 4, captures: [image] });
  });
}
