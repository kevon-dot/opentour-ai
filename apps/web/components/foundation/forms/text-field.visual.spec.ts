import { expect, test, type Locator, type Page } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';

const widths = [320, 768, 1280, 1440];
const helper = 'Used for each generated run.';
const problem = 'Enter a whole number.';
const paint = {
  text: 'rgb(24, 34, 55)', secondary: 'rgb(81, 96, 120)', control: 'rgb(98, 111, 132)', surface: 'rgb(255, 255, 255)',
  failureSurface: 'rgb(255, 232, 227)', failureText: 'rgb(170, 69, 58)', disabledSurface: 'rgb(237, 240, 245)',
  focus: 'rgb(36, 91, 238)',
};
/** Native field frame at 100% text with single-line content: frame height and each line's [x, y] origin. */
const native = {
  rest: { height: 108, lines: [[17, 17], [17, 45], [17, 73]] },
  focus: { height: 110, lines: [[18, 18], [18, 46], [18, 74]] },
  invalid: { height: 134, lines: [[17, 17], [17, 45], [17, 73], [17, 99]] },
  invalidFocus: { height: 136, lines: [[18, 18], [18, 46], [18, 74], [18, 100]] },
};

let script: string;
let styles: string;
test.beforeAll(async () => {
  const output = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/text-field.tsx'), 'esm');
  script = output.script; styles = output.css;
});

async function open(page: Page, width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.route('http://field.test/', route => route.fulfill({ contentType: 'text/html',
    body: '<!doctype html><html lang="en"><title>Text field states</title><main></main></html>' }));
  await page.route('http://field.test/gallery.js', route => route.fulfill({ contentType: 'text/javascript', body: script }));
  await page.goto('http://field.test/');
  await page.addStyleTag({ content: styles + `html{font-size:${scale}%}body{margin:0}` });
  await page.addScriptTag({ type: 'module', url: 'http://field.test/gallery.js' });
  await expect(page.getByRole('heading', { name: 'Text field states' })).toBeVisible();
  await page.evaluate(async () => {
    await Promise.all(['500 14px Geist', '400 14px Geist'].map(font => document.fonts.load(font)));
    await document.fonts.ready;
  });
  expect(await page.evaluate(() => [...document.fonts].some(face => face.family === 'Geist' && face.status === 'loaded'))).toBe(true);
}

function gallery(page: Page) {
  const region = (name: string) => page.getByRole('region', { name, exact: true });
  const values = page.getByRole('status', { name: 'Raw values' });
  return {
    region,
    /** The bordered frame of a labelled field example. */
    frame: (name: string) => region(name).locator('.ot-text-field'),
    /** The native input of a labelled field example. */
    box: (name: string) => region(name).getByRole('textbox'),
    /** A standalone input, found by its accessible name. */
    input: (name: string) => page.getByRole('textbox', { name, exact: true }),
    control: (name: string) => page.getByRole('button', { name, exact: true }),
    /** The exact raw string the gallery last received for an example; no whitespace normalisation. */
    raw: (key: string) => expect.poll(async () =>
      (JSON.parse((await values.textContent()) || '{}') as Record<string, string>)[key]),
    blurs: page.getByRole('status', { name: 'Blur validations' }),
  };
}

/** Geometry and paint read from the rendered element and its direct children. */
const measure = (target: Locator) => target.evaluate(node => {
  const bounds = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  const sides = ['top', 'right', 'bottom', 'left'];
  const type = (computed: CSSStyleDeclaration) => [computed.fontSize, computed.lineHeight, computed.fontWeight, computed.fontFamily];
  return {
    left: bounds.x, width: bounds.width, height: bounds.height,
    border: sides.map(side => ['width', 'style', 'color'].map(part => style.getPropertyValue(`border-${side}-${part}`)).join(' ')),
    padding: sides.map(side => style.getPropertyValue(`padding-${side}`)),
    radius: style.borderTopLeftRadius, background: style.backgroundColor, opacity: style.opacity,
    outline: [style.outlineStyle, style.outlineWidth, style.outlineColor, style.outlineOffset],
    type: type(style), color: style.color,
    parts: [...node.children].map(child => {
      const box = child.getBoundingClientRect();
      const computed = getComputedStyle(child);
      return { tag: child.tagName, x: box.x - bounds.x, y: box.y - bounds.y, width: box.width, height: box.height,
        type: type(computed), color: computed.color };
    }),
  };
});
type Measured = Awaited<ReturnType<typeof measure>>;

const semantics = (target: Locator) => target.evaluate((node: HTMLInputElement) => ({
  tag: node.tagName, type: node.type, id: node.id,
  labels: [...(node.labels ?? [])].map(label => [label.htmlFor, label.textContent]),
  value: node.value, required: node.required, disabled: node.disabled,
  invalid: node.getAttribute('aria-invalid'), describedBy: node.getAttribute('aria-describedby'),
}));
const fieldSemantics = (key: string, state: Record<string, unknown> = {}) => ({
  tag: 'INPUT', type: 'text', id: 'attempts-' + key, labels: [['attempts-' + key, 'Maximum attempts · Required']],
  value: '3', required: true, disabled: false, invalid: null, describedBy: `attempts-${key}-helper`, ...state,
});

/** Frame: a real border (1px control at rest, 2px action blue on keyboard focus, also when invalid) around the full
 *  16px padding, with 8px gaps and no outline. Label and raw value in label type, helper and error in small type;
 *  an invalid frame paints every line in the failure text colour. */
function expectField(field: Measured, s: number, invalid: boolean, focused = false) {
  const stroke = focused ? 2 : 1;
  const inset = stroke + 16;
  expect(field.parts.map(part => part.tag)).toEqual(invalid ? ['LABEL', 'INPUT', 'P', 'P'] : ['LABEL', 'INPUT', 'P']);
  expect(field.border).toEqual(Array(4).fill(`${stroke}px solid ${focused ? paint.focus : paint.control}`));
  expect(field.padding).toEqual(Array(4).fill('16px'));
  expect(field.outline[0]).toBe('none');
  expect(field.radius).toBe('8px');
  expect(field.background).toBe(invalid ? paint.failureSurface : paint.surface);
  let top = inset;
  field.parts.forEach((part, index) => {
    const small = index > 1;
    const line = (small ? 18 : 20) * s;
    expect([part.x, part.y, part.width]).toEqual([inset, top, field.width - 2 * inset]);
    expect(part.type).toEqual([`${(small ? 12 : 14) * s}px`, `${line}px`, small ? '400' : '500', 'Geist, sans-serif']);
    expect(part.color).toBe(invalid ? paint.failureText : small ? paint.secondary : paint.text);
    expect(part.height).toBeGreaterThanOrEqual(line);
    expect(part.height % line).toBe(0);
    top += part.height + 8;
  });
  expect(field.parts[1].height).toBe(20 * s);
  expect(field.height).toBe(top - 8 + inset);
}

/** The exact native frame height and line origins; only meaningful at 100% text, where every line is single. */
function expectNative(field: Measured, state: keyof typeof native) {
  expect(field.height).toBe(native[state].height);
  expect(field.parts.map(part => [part.x, part.y])).toEqual(native[state].lines);
}

/** Standalone input: 280px wide, one body line inside the full 12px padding and a real border with no outline.
 *  At rest 1px control (13px content inset): 48px tall, 70px at 200% text. Focused 2px action blue (14px content
 *  inset): 50px and 72px. An invalid input keeps the ordinary border and surface. */
function expectInput(input: Measured, s: number, disabled: boolean, focused = false) {
  const stroke = focused ? 2 : 1;
  expect([input.width, input.height]).toEqual([280, (s === 1 ? 48 : 70) + (focused ? 2 : 0)]);
  expect(input.border).toEqual(Array(4).fill(`${stroke}px solid ${focused ? paint.focus : paint.control}`));
  expect(input.padding).toEqual(Array(4).fill('12px'));
  expect(input.outline[0]).toBe('none');
  expect(input.radius).toBe('6px');
  expect(input.background).toBe(disabled ? paint.disabledSurface : paint.surface);
  expect(input.type).toEqual([`${14 * s}px`, `${22 * s}px`, '400', 'Geist, sans-serif']);
  expect(input.color).toBe(paint.control);
  expect(input.opacity).toBe('1');
}

for (const width of widths) for (const scale of [100, 200]) {
  test(`text field at ${width}px and ${scale}% text`, async ({ page }, info) => {
    const s = scale / 100;
    const pad = width < 768 ? 16 : 32;
    const exact = s === 1 && width >= 768; // The native 360px measure with single-line content.
    await open(page, width, scale);
    const { region, frame, box, input, control, raw, blurs } = gallery(page);
    const capture = (state: string, target: Locator) => target.screenshot({
      path: info.outputPath(`field-${width}-${scale}-${state}.png`), animations: 'disabled', caret: 'hide' });

    // Native semantics before any interaction.
    expect(await semantics(box('Default'))).toEqual(fieldSemantics('default'));
    expect(await semantics(box('Keyboard focus'))).toEqual(fieldSemantics('focus'));
    expect(await semantics(box('Error'))).toEqual(fieldSemantics('invalid', { value: '2.5', invalid: 'true',
      describedBy: 'attempts-invalid-helper attempts-invalid-error' }));
    expect(await semantics(box('Disabled'))).toEqual(fieldSemantics('disabled', { required: false, disabled: true,
      labels: [['attempts-disabled', 'Maximum attempts · Optional']] }));
    await expect(region('Default').getByText('Maximum attempts · Required', { exact: true })).toBeVisible();
    await expect(box('Default')).toHaveAccessibleName('Maximum attempts · Required');
    await expect(box('Default')).toHaveAccessibleDescription(helper);
    await expect(box('Error')).toHaveAccessibleDescription(helper + ' ' + problem);
    await expect(box('Disabled')).toHaveAccessibleName('Maximum attempts · Optional');
    await expect(box('Disabled')).toBeDisabled();
    const reference = input('Generated reference');
    const count = input('Generated count');
    const unavailable = input('Unavailable generated reference');
    expect(await semantics(reference)).toEqual({ tag: 'INPUT', type: 'text', id: 'reference', labels: [],
      value: 'Generated value', required: false, disabled: false, invalid: null, describedBy: null });
    expect(await semantics(count)).toMatchObject({ id: 'count', labels: [], value: '2.5', invalid: 'true', describedBy: 'count-problem' });
    await expect(count).toHaveAccessibleDescription(problem);
    await expect(unavailable).toBeDisabled();

    // Geometry and paint from the rendered DOM at this breakpoint and text scale.
    const resting: Record<string, Measured> = {};
    for (const [name, invalid] of [['Default', false], ['Keyboard focus', false], ['Error', true], ['Disabled', false],
      ['Form measure', false]] as const) {
      const field = resting[name] = await measure(frame(name));
      expectField(field, s, invalid);
      expect([field.left, field.width]).toEqual([pad, Math.min(name === 'Form measure' ? 688 : 360, width - 2 * pad)]);
      expect(field.opacity).toBe(name === 'Disabled' ? '0.55' : '1');
      if (exact) expectNative(field, invalid ? 'invalid' : 'rest');
    }
    // The invalid standalone input keeps the ordinary border and surface; only the disabled surface differs.
    for (const [target, disabled] of [[reference, false], [count, false], [unavailable, true]] as const) {
      const standalone = await measure(target);
      expectInput(standalone, s, disabled);
      expect(standalone.left).toBe(pad);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    expect(await page.locator('.field-gallery').evaluate(node => [...node.querySelectorAll('*')].every(element =>
      element instanceof HTMLInputElement || getComputedStyle(element).display === 'inline' ||
      element.scrollWidth <= element.clientWidth + 1))).toBe(true);

    // Resting captures; the error example has not been focused.
    await capture('default', frame('Default'));
    await capture('error', frame('Error'));
    await capture('disabled', frame('Disabled'));
    await capture('input', region('Standalone input').locator('.field-gallery-inputs'));

    // Actual keyboard focus: the real border becomes 2px action blue, so the frame grows 2px taller and its lines
    // move 1px in, as in the native design. The measure holds and no outline is drawn on the frame or the control.
    await page.keyboard.press('Tab');
    await expect(box('Default')).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(box('Keyboard focus')).toBeFocused();
    await expect(blurs).toHaveText('default');
    await page.keyboard.press('ArrowRight'); // Collapse the selection made by tabbing in; focus does not move.
    expect(await box('Keyboard focus').evaluate(node => node.matches(':focus-visible'))).toBe(true);
    const focused = await measure(frame('Keyboard focus'));
    expectField(focused, s, false, true);
    expect([focused.left, focused.width]).toEqual([pad, resting['Keyboard focus'].width]);
    if (exact) expectNative(focused, 'focus');
    await expect(box('Keyboard focus')).toHaveCSS('outline-style', 'none');
    await capture('focus', frame('Keyboard focus'));
    await expect(box('Keyboard focus')).toBeFocused();

    // An invalid field takes the same blue focus border over its failure surface and text, and the caller's blur
    // validation clears the error.
    await page.keyboard.press('Tab');
    await expect(box('Error')).toBeFocused();
    const invalidFocus = await measure(frame('Error'));
    expectField(invalidFocus, s, true, true);
    expect(invalidFocus.width).toBe(resting.Error.width);
    if (exact) expectNative(invalidFocus, 'invalidFocus');
    await expect(box('Error')).toHaveCSS('outline-style', 'none');
    const helperNode = (await page.locator('#attempts-invalid-helper').elementHandle())!;
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.type('4');
    await raw('invalid').toBe('4');
    await expect(box('Error')).toHaveAttribute('aria-invalid', 'true'); // Not revalidated until blur.
    await page.keyboard.press('Tab');
    await expect(box('Form measure')).toBeFocused(); // The disabled field is not a keyboard stop.
    await expect(region('Error').getByText(problem, { exact: true })).toHaveCount(0);
    expect(await semantics(box('Error'))).toEqual(fieldSemantics('invalid', { value: '4' }));
    expect(await helperNode.evaluate(node => node.isConnected && node.id)).toBe('attempts-invalid-helper');
    expectField(await measure(frame('Error')), s, false);
    expectField(await measure(frame('Form measure')), s, false, true);

    // Standalone input: actual focus state (2px blue border, 2px taller, same width) and raw generic text.
    await page.keyboard.press('Tab');
    await expect(reference).toBeFocused();
    expect(await reference.evaluate(node => node.matches(':focus-visible'))).toBe(true);
    expectInput(await measure(reference), s, false, true);
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.type('  Any generic text ');
    await raw('reference').toBe('  Any generic text ');
    expect((await semantics(reference)).value).toBe('  Any generic text ');
    await page.keyboard.press('Tab');
    await expect(count).toBeFocused();
    expectInput(await measure(count), s, false, true); // Invalid and focused: the same blue border.
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.type('7');
    await page.keyboard.press('Tab');
    await expect(control('Supply "3" to the default example')).toBeFocused(); // The disabled input is skipped.
    await expect(page.locator('#count-problem')).toHaveCount(0);
    expect(await semantics(count)).toMatchObject({ value: '7', invalid: null, describedBy: null });
    await expect(blurs).toHaveText('default, focus, invalid, measure, count');

    // Controlled raw input: typed text, a natural blur into the error state, then caller-supplied values.
    await region('Default').getByText('Maximum attempts · Required', { exact: true }).click();
    await expect(box('Default')).toBeFocused();
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.type(' 2.5 ');
    await raw('default').toBe(' 2.5 ');
    expect((await semantics(box('Default'))).value).toBe(' 2.5 ');
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.type('2.5');
    await raw('default').toBe('2.5');
    expect(await semantics(box('Default'))).toEqual(fieldSemantics('default', { value: '2.5' }));
    await page.keyboard.press('Tab');
    await expect(box('Keyboard focus')).toBeFocused();
    await expect(region('Default').getByText(problem, { exact: true })).toBeVisible();
    await expect(region('Default').getByText(helper, { exact: true })).toBeVisible();
    expect(await semantics(box('Default'))).toEqual(fieldSemantics('default', { value: '2.5', invalid: 'true',
      describedBy: 'attempts-default-helper attempts-default-error' }));
    await expect(box('Default')).toHaveAccessibleDescription(helper + ' ' + problem);
    const afterBlur = await measure(frame('Default'));
    expectField(afterBlur, s, true);
    expect(afterBlur.width).toBe(resting.Default.width);
    expect(afterBlur.height).toBe(resting.Default.height + 8 + afterBlur.parts[3].height);
    await control('Supply "" to the default example').click();
    await raw('default').toBe('');
    expect(await semantics(box('Default'))).toMatchObject({ value: '', invalid: 'true', required: true });
    await control('Supply "2.5" to the default example').click();
    await raw('default').toBe('2.5');
    expect(await semantics(box('Default'))).toMatchObject({ value: '2.5', invalid: 'true' });
    await control('Supply "3" to the default example').click();
    await raw('default').toBe('3');
    await expect(region('Default').getByText(problem, { exact: true })).toHaveCount(0);
    expect(await semantics(box('Default'))).toEqual(fieldSemantics('default'));
    expect(await measure(frame('Default'))).toEqual(resting.Default);
    await box('Default').click();
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.press('Backspace');
    await raw('default').toBe('');
    expect(await semantics(box('Default'))).toEqual(fieldSemantics('default', { value: '' }));

    // Long raw text scrolls inside the native input; the frame and the page do not grow.
    const long = 'unparsed'.repeat(18);
    await box('Default').fill(long);
    await raw('default').toBe(long);
    expect(await box('Default').evaluate((node: HTMLInputElement) => [node.value, node.scrollWidth > node.clientWidth]))
      .toEqual([long, true]);
    expect((await measure(frame('Default'))).width).toBe(resting.Default.width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);

    // Native disabled state follows the caller across rerenders and keeps the raw value.
    const toggle = control('Disable the disabled example');
    await expect(toggle).toHaveAttribute('aria-pressed', 'true');
    await box('Disabled').click({ force: true });
    await expect(box('Disabled')).not.toBeFocused();
    await raw('disabled').toBe('3');
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await expect(box('Disabled')).toBeEnabled();
    await expect(frame('Disabled')).toHaveCSS('opacity', '1');
    await box('Disabled').click();
    await expect(box('Disabled')).toBeFocused();
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.type('2.5');
    await raw('disabled').toBe('2.5');
    // Blur validation can grow the field. Activate the toggle after the layout settles.
    await toggle.focus();
    await page.keyboard.press('Space');
    await expect(box('Disabled')).toBeDisabled();
    expect(await semantics(box('Disabled'))).toMatchObject({ value: '2.5', disabled: true, required: false, invalid: 'true',
      describedBy: 'attempts-disabled-helper attempts-disabled-error' });
    const disabledInvalid = await measure(frame('Disabled'));
    expectField(disabledInvalid, s, true);
    expect(disabledInvalid.opacity).toBe('0.55');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
  });
}

test('larger text grows the type inside the same measure at every width', async ({ page }) => {
  await open(page, 1440, 100);
  const { frame, input } = gallery(page);
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => innerWidth)).toBe(width);
    const sample = async (scale: number) => {
      await page.evaluate(percent => { document.documentElement.style.fontSize = `${percent}%`; }, scale);
      return { field: await measure(frame('Default')), form: await measure(frame('Form measure')),
        input: await measure(input('Generated reference')),
        page: await page.evaluate(() => document.documentElement.scrollWidth) };
    };
    const normal = await sample(100);
    const large = await sample(200);
    expect([normal.page, large.page]).toEqual([width, width]);
    expect([normal.field.width, large.field.width]).toEqual(Array(2).fill(Math.min(360, width - (width < 768 ? 32 : 64))));
    expect([normal.form.width, large.form.width]).toEqual(Array(2).fill(Math.min(688, width - (width < 768 ? 32 : 64))));
    expect(large.input.width).toBe(normal.input.width);
    expect(normal.field.parts.map(part => part.type.slice(0, 2))).toEqual([['14px', '20px'], ['14px', '20px'], ['12px', '18px']]);
    expect(large.field.parts.map(part => part.type.slice(0, 2))).toEqual([['28px', '40px'], ['28px', '40px'], ['24px', '36px']]);
    expect([normal.field.parts[1].height, large.field.parts[1].height]).toEqual([20, 40]);
    large.field.parts.forEach((part, index) =>
      expect(part.height).toBeGreaterThanOrEqual(2 * normal.field.parts[index].height));
    expect(large.field.height).toBeGreaterThanOrEqual(normal.field.height + 58);
    expect([normal.input.type[0], large.input.type[0]]).toEqual(['14px', '28px']);
    expect([normal.input.height, large.input.height]).toEqual([48, 70]);
    if (width >= 768) expectNative(normal.field, 'rest');
    expectField(large.field, 2, false);
    expectInput(large.input, 2, false);
  }
});

test('forced colors keep the boundary, the separate error and a system focus indicator', async ({ page }) => {
  await open(page, 1280, 100);
  await page.emulateMedia({ forcedColors: 'active' });
  const { region, frame, box, input } = gallery(page);
  const [highlight, canvasText] = await page.evaluate(() => {
    const swatch = document.createElement('span');
    swatch.style.forcedColorAdjust = 'none';
    document.body.append(swatch);
    const colors = ['Highlight', 'CanvasText'].map(color => {
      swatch.style.color = color;
      return getComputedStyle(swatch).color;
    });
    swatch.remove();
    return colors;
  });
  // At rest the boundary is the 1px system text colour, on the invalid frame too.
  for (const name of ['Default', 'Error']) {
    expect((await measure(frame(name))).border).toEqual(Array(4).fill('1px solid ' + canvasText));
  }
  expect((await measure(input('Generated reference'))).border).toEqual(Array(4).fill('1px solid ' + canvasText));
  // Focus is the 2px border in the system highlight, with no outline on the frame or the control to double it.
  for (let stop = 0; stop < 3; stop += 1) await page.keyboard.press('Tab');
  await expect(box('Error')).toBeFocused();
  const invalid = await measure(frame('Error'));
  expect(invalid.border).toEqual(Array(4).fill('2px solid ' + highlight));
  expect(invalid.outline[0]).toBe('none');
  await expect(box('Error')).toHaveCSS('outline-style', 'none');
  await expect(box('Error')).toHaveAttribute('aria-invalid', 'true');
  await expect(region('Error').getByText(problem, { exact: true })).toBeVisible();
  await expect(region('Error').getByText(helper, { exact: true })).toBeVisible();
  expect((await measure(frame('Disabled'))).opacity).toBe('1');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await expect(input('Generated reference')).toBeFocused();
  const standalone = await measure(input('Generated reference'));
  expect(standalone.border).toEqual(Array(4).fill('2px solid ' + highlight));
  expect(standalone.outline[0]).toBe('none');
});
