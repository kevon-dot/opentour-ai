import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import { describe, it, mock } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { Checkbox, Switch, checkboxStates, requestChange, switchStates } from './selection-controls';
import { Checkbox as ExportedCheckbox, Switch as ExportedSwitch } from './index';
import type { CheckboxProps, CheckboxState, SwitchProps, SwitchState } from './selection-controls.contracts';
import { selectionControlCaptureInventory, selectionControlFixtures as fixtures } from './selection-controls.fixtures';

const noop = () => undefined;
const read = (path: string) => readFileSync(new URL(path, import.meta.url));
const sha256 = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
const text = (html: string) => html.replace(/<[^>]+>/g, '');
const attribute = (html: string, name: string) => html.match(new RegExp(` ${name}="([^"]*)"`))?.[1];
const checkbox = (props: Partial<CheckboxProps> = {}) => renderToStaticMarkup(
  <Checkbox checked={false} label="Generated selection" helper="Not selected" onCheckedChange={noop} {...props} />);
const toggle = (props: Partial<SwitchProps> = {}) => renderToStaticMarkup(
  <Switch checked={false} label="Generated preference" helper="Disabled by preference" onCheckedChange={noop} {...props} />);

const checkboxSpecimens = [...fixtures.checkbox, ...fixtures.suppliedCheckboxes,
  ...fixtures.contexts.flatMap(context => context.checkboxes), fixtures.long.checkbox];
const switchSpecimens = [...fixtures.switch, ...fixtures.suppliedSwitches,
  ...fixtures.contexts.flatMap(context => context.switches), fixtures.long.switch];

describe('labelled checkbox presentation contract', () => {
  testCases(checkboxSpecimens)('$key renders the exact supplied label and helper', ({ key: _key, ...props }) => {
    const html = renderToStaticMarkup(<Checkbox {...props} onCheckedChange={noop} />);
    assert.equal(text(html), props.label + props.helper);
    assert.ok((html)?.includes(`>${props.label}</label>`));
    assert.ok((html)?.includes(`>${props.helper}</span></div>`));
  });
  it('is one native checkbox named by its label and described by its helper', () => {
    const html = checkbox();
    const id = attribute(html, 'id');
    assert.equal((html.match(/<input /g))?.length, 1);
    assert.ok((html)?.includes('<input class="ot-selection-control" type="checkbox"'));
    assert.ok((html)?.includes(`<label class="ot-selection-label" for="${id}">Generated selection</label>`));
    assert.ok((html)?.includes(`<span class="ot-selection-helper" id="${attribute(html, 'aria-describedby')}">Not selected</span>`));
    assert.notEqual(attribute(html, 'aria-describedby'), id);
    assert.doesNotMatch(html, /role=|aria-label=|aria-labelledby=|aria-live|aria-checked|tabindex|<button|<output|<a\b|<title/);
  });
  testCases([true, false])('reflects only the supplied checked value %j in the control and native indicator', checked => {
    const html = checkbox({ checked });
    assert.equal(html.includes(' checked=""'), checked);
    assert.equal(html.includes('ot-checkbox-indicator--checked'), checked);
    assert.equal((html.match(/<svg /g) ?? [])?.length, checked ? 1 : 0);
  });
  testCases(checkboxStates)('applies the %s treatment without changing the name, helper or supplied value', state => {
    for (const checked of [true, false]) {
      const html = checkbox({ state, checked });
      assert.ok((html)?.includes(`<div class="ot-selection ot-selection--checkbox ot-selection--${state}">`));
      assert.equal(html.includes(' disabled=""'), state === 'disabled');
      assert.equal(html.includes(' aria-invalid="true"'), state === 'error');
      assert.equal(html.includes(' checked=""'), checked);
      assert.equal(text(html), 'Generated selectionNot selected');
    }
  });
  it('keeps the mapped CheckCircle path and stroke, hidden from assistive technology and focus', () => {
    const html = checkbox({ checked: true });
    assert.ok((html)?.includes('<svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" class="ot-checkbox-glyph" aria-hidden="true" focusable="false">'));
    assert.equal(sha256(attribute(html, 'd')!), '51e06728c6c9fa2ab427a8596ec6bbd2deb5325f29a12e2ed0085db61ee8132f');
    assert.ok((html)?.includes('stroke="var(--svg-stroke-color)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'));
    assert.ok((html)?.includes('<rect width="16" height="16" fill="white"></rect>'));
    assert.match(html, /<span class="ot-selection-art ot-checkbox-indicator ot-checkbox-indicator--checked" aria-hidden="true"><svg /);
  });
});

describe('labelled switch presentation contract', () => {
  testCases(switchSpecimens)('$key renders the exact supplied label and helper', ({ key: _key, ...props }) => {
    const html = renderToStaticMarkup(<Switch {...props} onCheckedChange={noop} />);
    assert.equal(text(html), props.label + props.helper);
    assert.ok((html)?.includes(`>${props.label}</span></span>`));
    assert.ok((html)?.includes(`>${props.helper}</span></div>`));
  });
  it('is one native non-submit button exposed as a switch, named by its label and described by its helper', () => {
    const html = toggle();
    assert.equal((html.match(/<button /g))?.length, 1);
    assert.ok((html)?.includes('<button class="ot-selection-control" type="button" role="switch" aria-checked="false"'));
    assert.ok((html)?.includes(`<span class="ot-selection-label" id="${attribute(html, 'aria-labelledby')}">Generated preference</span>`));
    assert.ok((html)?.includes(`<span class="ot-selection-helper" id="${attribute(html, 'aria-describedby')}">Disabled by preference</span>`));
    assert.notEqual(attribute(html, 'aria-labelledby'), attribute(html, 'aria-describedby'));
    assert.match(html, /<button [^>]*><\/button>/);
    assert.doesNotMatch(html, /aria-label=|aria-live|tabindex|<input|<output|<a\b|<title|<svg/);
  });
  testCases([true, false])('reflects only the supplied checked value %j in state and visible native artwork', checked => {
    const html = toggle({ checked });
    assert.equal(attribute(html, 'aria-checked'), String(checked));
    const images = [...html.matchAll(/<img [^>]*>/g)].map(match => match[0]);
    assert.equal((images)?.length, 2);
    for (const image of images) assert.match(image, /^<img src="[^"]+" width="38" height="20" alt=""/);
    // The bundler may serve each export by path or embed it; either way the off knob is at 10 and the on knob at 28.
    const knob = (image: string) => {
      const src = attribute(image, 'src')!;
      if (!src.startsWith('data:')) return /switch-off[^/]*\.svg/.test(src) ? '10' : /switch-on[^/]*\.svg/.test(src) ? '28' : null;
      const body = src.slice(src.indexOf(',') + 1).replaceAll('&#x27;', "'").replaceAll('&quot;', '"');
      const svg = src.includes(';base64,') ? Buffer.from(body, 'base64').toString('utf8') : decodeURIComponent(body);
      assert.match(svg, /^<svg [^>]*width=['"]38['"] height=['"]20['"] viewBox=['"]0 0 38 20['"]/);
      return svg.match(/<circle [^>]*cx=['"](\d+)['"]/)?.[1] ?? null;
    };
    assert.deepEqual(images.map(knob), ['10', '28']);
    assert.deepEqual(images.map(image => image.includes(' hidden=""')), [checked, !checked]);
    assert.ok((html)?.includes('<span class="ot-selection-art ot-switch-art" aria-hidden="true">'));
  });
  testCases(switchStates)('applies the %s treatment without changing the name, helper or supplied value', state => {
    for (const checked of [true, false]) {
      const html = toggle({ state, checked });
      assert.ok((html)?.includes(`<div class="ot-selection ot-selection--switch ot-selection--${state}">`));
      // Pending must stay focusable: it is never expressed with the native disabled attribute.
      assert.equal(html.includes(' disabled=""'), state === 'disabled');
      assert.equal(html.includes(' aria-disabled="true"'), state === 'pending');
      assert.equal(html.includes(' aria-busy="true"'), state === 'pending');
      assert.equal(attribute(html, 'aria-checked'), String(checked));
      assert.equal(text(html), 'Generated preferenceDisabled by preference');
    }
  });
});

describe('closed controlled selection API', () => {
  const controls = [['Checkbox', checkbox], ['Switch', toggle]] as const;
  testCases(controls)('%s escapes exact caller text instead of making markup', (_name, render) => {
    const html = render({ label: '<script>Generated & unconfirmed</script>', helper: '<img src=x onerror=alert(1)> "quoted"' });
    assert.ok((html)?.includes('&lt;script&gt;Generated &amp; unconfirmed&lt;/script&gt;'));
    assert.ok((html)?.includes('&lt;img src=x onerror=alert(1)&gt; &quot;quoted&quot;'));
    assert.ok((html)?.includes('<script>') === false);
    assert.equal((html.match(/<img /g) ?? [])?.length, render === toggle ? 2 : 0);
  });
  testCases(controls)('%s rejects a missing visible label or helper', (_name, render) => {
    for (const value of ['', ' ', '\n', undefined, null, 0, {}]) {
      assert.throws(() => render({ label: value as string }), /visible foundation component label/);
      assert.throws(() => render({ helper: value as string }), /visible foundation component helper/);
    }
  });
  testCases(controls)('%s rejects a checked value that is not a boolean', (_name, render) => {
    for (const value of ['true', 'false', 'mixed', 'indeterminate', 0, 1, null, undefined, {}]) {
      assert.throws(() => render({ checked: value as boolean }), /boolean checked value/);
    }
  });
  testCases(controls)('%s rejects a missing change request handler', (_name, render) => {
    for (const value of [undefined, null, 'request', true, {}]) {
      assert.throws(() => render({ onCheckedChange: value as unknown as () => void }), /change request handler/);
    }
  });
  it('rejects undeclared states, including the other control’s treatment and tri-state names', () => {
    for (const state of ['pending', 'indeterminate', 'mixed', 'focus', 'hover', 'checked', '', null, 0, true, {}]) {
      assert.throws(() => checkbox({ state: state as CheckboxState }), /declared selection control state/);
    }
    for (const state of ['error', 'indeterminate', 'mixed', 'focus', 'hover', 'on', '', null, 0, true, {}]) {
      assert.throws(() => toggle({ state: state as SwitchState }), /declared selection control state/);
    }
  });
  testCases(controls)('%s ignores unauthorized passthrough, visual-state and semantic override props', (_name, render) => {
    const overrides = { className: 'override', style: { display: 'none' }, id: 'override', role: 'alert', tabIndex: 0,
      'aria-label': 'Replaced', 'aria-live': 'assertive', 'aria-checked': 'mixed', title: 'Replaced', children: 'Replaced',
      name: 'preference', value: 'on', defaultChecked: true, indeterminate: true, disabled: true, pending: true, error: true,
      focused: true, hover: true, type: 'submit', onClick: noop, onChange: noop, dangerouslySetInnerHTML: { __html: '<b>Replaced</b>' } };
    assert.equal(render(overrides as never), render());
  });
  it('requests the opposite of the supplied value exactly once, and nothing when disabled or pending', () => {
    for (const state of ['enabled', 'error'] as const) for (const checked of [true, false]) {
      const request = mock.fn();
      requestChange(checked, state, request);
      assert.deepEqual(request.mock.calls.map(call => call.arguments), [[!checked]]);
    }
    for (const state of ['disabled', 'pending'] as const) for (const checked of [true, false]) {
      const request = mock.fn();
      requestChange(checked, state, request);
      assert.equal(request.mock.callCount(), 0);
    }
  });
  it('never invokes the change request while rendering', () => {
    const request = mock.fn();
    renderToStaticMarkup(<><Checkbox checked label="Generated" helper="Generated helper" onCheckedChange={request} />
      <Switch checked label="Generated" helper="Generated helper" state="pending" onCheckedChange={request} /></>);
    assert.equal(request.mock.callCount(), 0);
  });
  it('gives every control distinct, resolvable IDs that are stable across identical renders', () => {
    const tree = <>{fixtures.checkbox.map(({ key, ...props }) => <Checkbox key={key} {...props} checked onCheckedChange={noop} />)}
      {fixtures.switch.map(({ key, ...props }) => <Switch key={key} {...props} onCheckedChange={noop} />)}</>;
    const html = renderToStaticMarkup(tree);
    const ids = [...html.matchAll(/ id="([^"]+)"/g)].map(match => match[1]);
    // Six checkboxes own control, helper and clip IDs; six switches own label and helper IDs.
    assert.equal((ids)?.length, 6 * 3 + 6 * 2);
    assert.equal(new Set(ids).size, ids.length);
    const references = [...html.matchAll(/ (?:for|aria-labelledby|aria-describedby)="([^"]+)"/g)].map(match => match[1]);
    assert.equal((references)?.length, 6 * 2 + 6 * 2);
    for (const reference of references) assert.ok((ids)?.includes(reference));
    for (const clip of html.matchAll(/clip-path="url\(#([^)]+)\)"/g)) assert.ok((ids)?.includes(clip[1]));
    assert.equal((html.match(/clip-path="url\(#/g))?.length, 6);
    assert.equal(renderToStaticMarkup(tree), html);
  });
  it('exports the same closed components from the foundation index', () => {
    assert.equal(ExportedCheckbox, Checkbox);
    assert.equal(ExportedSwitch, Switch);
  });
});

describe('native artwork, provenance and static presentation', () => {
  const source = JSON.parse(read('./selection-controls-assets/source.json').toString('utf8'));
  it('retains the exact downloaded switch exports at their native root size', () => {
    assert.equal(sha256(read('./selection-controls-assets/switch-off.svg')), 'bc457ac885d6b71d357451f057d515a463460bd2b57a6ac957ad86279f9cb7b7');
    assert.equal(sha256(read('./selection-controls-assets/switch-on.svg')), '960e63d9a772ca28795b26f2cc72f709e7aa106400b21ffa08a5695d972af3f6');
    for (const name of ['switch-off', 'switch-on']) {
      const svg = read(`./selection-controls-assets/${name}.svg`).toString('utf8');
      assert.match(svg, /^<svg [^>]*width="38" height="20" viewBox="0 0 38 20" /);
      assert.doesNotMatch(svg, /<script|<foreignObject|href=|\son[a-z]+=|url\(/i);
    }
  });
  it('binds every recorded source file to its current bytes and keeps the upstream MIT attribution', () => {
    assert.deepEqual(Object.keys(source.files).sort(), ['selection-controls-assets/switch-off.svg', 'selection-controls-assets/switch-on.svg',
      'selection-controls-glyphs/Icon.tsx', 'selection-controls-glyphs/IconCheckCircle.tsx', 'selection-controls-glyphs/LICENSE']);
    for (const [path, digest] of Object.entries(source.files)) assert.equal(sha256(read(`./${path}`)), digest);
    assert.deepEqual(read('./selection-controls-glyphs/LICENSE'), read('./status-icon-glyphs/LICENSE'));
    assert.ok((read('./selection-controls-glyphs/LICENSE').toString('utf8'))?.includes('MIT License'));
    assert.equal(source.sources.check_circle.upstream, `https://github.com/figma/sds/blob/${source.sds_commit}/src/ui/icons/IconCheckCircle.tsx`);
    assert.equal(source.sources.check_circle.git_blob, '9805fb3c1e23edee4354e1038bafae5f87d265eb');
  });
  it('keeps the focus ring an outline, with no motion, shadow or runtime remote source', () => {
    const css = read('./selection-controls.css').toString('utf8');
    assert.ok((css)?.includes('.ot-selection-control:focus-visible { outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }'));
    assert.doesNotMatch(css, /transition|animation|@keyframes|box-shadow|https?:|@import/);
    assert.deepEqual(css.match(/#[0-9a-f]{3,8}\b/gi), ['#1e1e1e']);
    for (const name of ['selection-controls.tsx', 'selection-controls.css', 'selection-controls.fixtures.ts', 'testing/selection-controls-gallery.tsx']) {
      assert.doesNotMatch(read(`./${name}`).toString('utf8'), /figma\.com|localStorage|sessionStorage|fetch\(|setTimeout|setInterval|useState\(.*checked/);
    }
    assert.doesNotMatch(read('./selection-controls.tsx').toString('utf8'), /useState|useEffect|useRef|import\.meta/);
  });
  it('declares a unique capture for every registered mode, width, text scale and state', () => {
    const names = selectionControlCaptureInventory();
    assert.equal((names)?.length, 74);
    assert.equal(new Set(names).size, 74);
    for (const name of names) assert.match(name, /^selection-controls-(customer-controlled|opentour-managed)-(320|768|1280|1440)-(100|200)-[a-z-]+\.png$/);
    for (const mode of fixtures.modes) for (const { width } of fixtures.viewports) for (const scale of fixtures.scales) {
      assert.ok((names)?.includes(`selection-controls-${mode}-${width}-${scale}-gallery.png`));
    }
  });
});
