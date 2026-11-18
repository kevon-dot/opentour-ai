import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { describe, expect, it, vi } from 'vitest';
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
  it.each(checkboxSpecimens)('$key renders the exact supplied label and helper', ({ key: _key, ...props }) => {
    const html = renderToStaticMarkup(<Checkbox {...props} onCheckedChange={noop} />);
    expect(text(html)).toBe(props.label + props.helper);
    expect(html).toContain(`>${props.label}</label>`);
    expect(html).toContain(`>${props.helper}</span></div>`);
  });
  it('is one native checkbox named by its label and described by its helper', () => {
    const html = checkbox();
    const id = attribute(html, 'id');
    expect(html.match(/<input /g)).toHaveLength(1);
    expect(html).toContain('<input class="ot-selection-control" type="checkbox"');
    expect(html).toContain(`<label class="ot-selection-label" for="${id}">Generated selection</label>`);
    expect(html).toContain(`<span class="ot-selection-helper" id="${attribute(html, 'aria-describedby')}">Not selected</span>`);
    expect(attribute(html, 'aria-describedby')).not.toBe(id);
    expect(html).not.toMatch(/role=|aria-label=|aria-labelledby=|aria-live|aria-checked|tabindex|<button|<output|<a\b|<title/);
  });
  it.each([true, false])('reflects only the supplied checked value %j in the control and native indicator', checked => {
    const html = checkbox({ checked });
    expect(html.includes(' checked=""')).toBe(checked);
    expect(html.includes('ot-checkbox-indicator--checked')).toBe(checked);
    expect(html.match(/<svg /g) ?? []).toHaveLength(checked ? 1 : 0);
  });
  it.each(checkboxStates)('applies the %s treatment without changing the name, helper or supplied value', state => {
    for (const checked of [true, false]) {
      const html = checkbox({ state, checked });
      expect(html).toContain(`<div class="ot-selection ot-selection--checkbox ot-selection--${state}">`);
      expect(html.includes(' disabled=""')).toBe(state === 'disabled');
      expect(html.includes(' aria-invalid="true"')).toBe(state === 'error');
      expect(html.includes(' checked=""')).toBe(checked);
      expect(text(html)).toBe('Generated selectionNot selected');
    }
  });
  it('keeps the mapped CheckCircle path and stroke, hidden from assistive technology and focus', () => {
    const html = checkbox({ checked: true });
    expect(html).toContain('<svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" class="ot-checkbox-glyph" aria-hidden="true" focusable="false">');
    expect(sha256(attribute(html, 'd')!)).toBe('51e06728c6c9fa2ab427a8596ec6bbd2deb5325f29a12e2ed0085db61ee8132f');
    expect(html).toContain('stroke="var(--svg-stroke-color)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"');
    expect(html).toContain('<rect width="16" height="16" fill="white"></rect>');
    expect(html).toMatch(/<span class="ot-selection-art ot-checkbox-indicator ot-checkbox-indicator--checked" aria-hidden="true"><svg /);
  });
});

describe('labelled switch presentation contract', () => {
  it.each(switchSpecimens)('$key renders the exact supplied label and helper', ({ key: _key, ...props }) => {
    const html = renderToStaticMarkup(<Switch {...props} onCheckedChange={noop} />);
    expect(text(html)).toBe(props.label + props.helper);
    expect(html).toContain(`>${props.label}</span></span>`);
    expect(html).toContain(`>${props.helper}</span></div>`);
  });
  it('is one native non-submit button exposed as a switch, named by its label and described by its helper', () => {
    const html = toggle();
    expect(html.match(/<button /g)).toHaveLength(1);
    expect(html).toContain('<button class="ot-selection-control" type="button" role="switch" aria-checked="false"');
    expect(html).toContain(`<span class="ot-selection-label" id="${attribute(html, 'aria-labelledby')}">Generated preference</span>`);
    expect(html).toContain(`<span class="ot-selection-helper" id="${attribute(html, 'aria-describedby')}">Disabled by preference</span>`);
    expect(attribute(html, 'aria-labelledby')).not.toBe(attribute(html, 'aria-describedby'));
    expect(html).toMatch(/<button [^>]*><\/button>/);
    expect(html).not.toMatch(/aria-label=|aria-live|tabindex|<input|<output|<a\b|<title|<svg/);
  });
  it.each([true, false])('reflects only the supplied checked value %j in state and visible native artwork', checked => {
    const html = toggle({ checked });
    expect(attribute(html, 'aria-checked')).toBe(String(checked));
    const images = [...html.matchAll(/<img [^>]*>/g)].map(match => match[0]);
    expect(images).toHaveLength(2);
    for (const image of images) expect(image).toMatch(/^<img src="[^"]+" width="38" height="20" alt=""/);
    // The bundler may serve each export by path or embed it; either way the off knob is at 10 and the on knob at 28.
    const knob = (image: string) => {
      const src = attribute(image, 'src')!;
      if (!src.startsWith('data:')) return /switch-off[^/]*\.svg/.test(src) ? '10' : /switch-on[^/]*\.svg/.test(src) ? '28' : null;
      const body = src.slice(src.indexOf(',') + 1).replaceAll('&#x27;', "'").replaceAll('&quot;', '"');
      const svg = src.includes(';base64,') ? Buffer.from(body, 'base64').toString('utf8') : decodeURIComponent(body);
      expect(svg).toMatch(/^<svg [^>]*width=['"]38['"] height=['"]20['"] viewBox=['"]0 0 38 20['"]/);
      return svg.match(/<circle [^>]*cx=['"](\d+)['"]/)?.[1] ?? null;
    };
    expect(images.map(knob)).toEqual(['10', '28']);
    expect(images.map(image => image.includes(' hidden=""'))).toEqual([checked, !checked]);
    expect(html).toContain('<span class="ot-selection-art ot-switch-art" aria-hidden="true">');
  });
  it.each(switchStates)('applies the %s treatment without changing the name, helper or supplied value', state => {
    for (const checked of [true, false]) {
      const html = toggle({ state, checked });
      expect(html).toContain(`<div class="ot-selection ot-selection--switch ot-selection--${state}">`);
      // Pending must stay focusable: it is never expressed with the native disabled attribute.
      expect(html.includes(' disabled=""')).toBe(state === 'disabled');
      expect(html.includes(' aria-disabled="true"')).toBe(state === 'pending');
      expect(html.includes(' aria-busy="true"')).toBe(state === 'pending');
      expect(attribute(html, 'aria-checked')).toBe(String(checked));
      expect(text(html)).toBe('Generated preferenceDisabled by preference');
    }
  });
});

describe('closed controlled selection API', () => {
  const controls = [['Checkbox', checkbox], ['Switch', toggle]] as const;
  it.each(controls)('%s escapes exact caller text instead of making markup', (_name, render) => {
    const html = render({ label: '<script>Generated & unconfirmed</script>', helper: '<img src=x onerror=alert(1)> "quoted"' });
    expect(html).toContain('&lt;script&gt;Generated &amp; unconfirmed&lt;/script&gt;');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt; &quot;quoted&quot;');
    expect(html).not.toContain('<script>');
    expect(html.match(/<img /g) ?? []).toHaveLength(render === toggle ? 2 : 0);
  });
  it.each(controls)('%s rejects a missing visible label or helper', (_name, render) => {
    for (const value of ['', ' ', '\n', undefined, null, 0, {}]) {
      expect(() => render({ label: value as string })).toThrow('visible foundation component label');
      expect(() => render({ helper: value as string })).toThrow('visible foundation component helper');
    }
  });
  it.each(controls)('%s rejects a checked value that is not a boolean', (_name, render) => {
    for (const value of ['true', 'false', 'mixed', 'indeterminate', 0, 1, null, undefined, {}]) {
      expect(() => render({ checked: value as boolean })).toThrow('boolean checked value');
    }
  });
  it.each(controls)('%s rejects a missing change request handler', (_name, render) => {
    for (const value of [undefined, null, 'request', true, {}]) {
      expect(() => render({ onCheckedChange: value as unknown as () => void })).toThrow('change request handler');
    }
  });
  it('rejects undeclared states, including the other control’s treatment and tri-state names', () => {
    for (const state of ['pending', 'indeterminate', 'mixed', 'focus', 'hover', 'checked', '', null, 0, true, {}]) {
      expect(() => checkbox({ state: state as CheckboxState })).toThrow('declared selection control state');
    }
    for (const state of ['error', 'indeterminate', 'mixed', 'focus', 'hover', 'on', '', null, 0, true, {}]) {
      expect(() => toggle({ state: state as SwitchState })).toThrow('declared selection control state');
    }
  });
  it.each(controls)('%s ignores unauthorized passthrough, visual-state and semantic override props', (_name, render) => {
    const overrides = { className: 'override', style: { display: 'none' }, id: 'override', role: 'alert', tabIndex: 0,
      'aria-label': 'Replaced', 'aria-live': 'assertive', 'aria-checked': 'mixed', title: 'Replaced', children: 'Replaced',
      name: 'preference', value: 'on', defaultChecked: true, indeterminate: true, disabled: true, pending: true, error: true,
      focused: true, hover: true, type: 'submit', onClick: noop, onChange: noop, dangerouslySetInnerHTML: { __html: '<b>Replaced</b>' } };
    expect(render(overrides as never)).toBe(render());
  });
  it('requests the opposite of the supplied value exactly once, and nothing when disabled or pending', () => {
    for (const state of ['enabled', 'error'] as const) for (const checked of [true, false]) {
      const request = vi.fn();
      requestChange(checked, state, request);
      expect(request.mock.calls).toEqual([[!checked]]);
    }
    for (const state of ['disabled', 'pending'] as const) for (const checked of [true, false]) {
      const request = vi.fn();
      requestChange(checked, state, request);
      expect(request).not.toHaveBeenCalled();
    }
  });
  it('never invokes the change request while rendering', () => {
    const request = vi.fn();
    renderToStaticMarkup(<><Checkbox checked label="Generated" helper="Generated helper" onCheckedChange={request} />
      <Switch checked label="Generated" helper="Generated helper" state="pending" onCheckedChange={request} /></>);
    expect(request).not.toHaveBeenCalled();
  });
  it('gives every control distinct, resolvable IDs that are stable across identical renders', () => {
    const tree = <>{fixtures.checkbox.map(({ key, ...props }) => <Checkbox key={key} {...props} checked onCheckedChange={noop} />)}
      {fixtures.switch.map(({ key, ...props }) => <Switch key={key} {...props} onCheckedChange={noop} />)}</>;
    const html = renderToStaticMarkup(tree);
    const ids = [...html.matchAll(/ id="([^"]+)"/g)].map(match => match[1]);
    // Six checkboxes own control, helper and clip IDs; six switches own label and helper IDs.
    expect(ids).toHaveLength(6 * 3 + 6 * 2);
    expect(new Set(ids).size).toBe(ids.length);
    const references = [...html.matchAll(/ (?:for|aria-labelledby|aria-describedby)="([^"]+)"/g)].map(match => match[1]);
    expect(references).toHaveLength(6 * 2 + 6 * 2);
    for (const reference of references) expect(ids).toContain(reference);
    for (const clip of html.matchAll(/clip-path="url\(#([^)]+)\)"/g)) expect(ids).toContain(clip[1]);
    expect(html.match(/clip-path="url\(#/g)).toHaveLength(6);
    expect(renderToStaticMarkup(tree)).toBe(html);
  });
  it('exports the same closed components from the foundation index', () => {
    expect(ExportedCheckbox).toBe(Checkbox);
    expect(ExportedSwitch).toBe(Switch);
  });
});

describe('native artwork, provenance and static presentation', () => {
  const source = JSON.parse(read('./selection-controls-assets/source.json').toString('utf8'));
  it('retains the exact downloaded switch exports at their native root size', () => {
    expect(sha256(read('./selection-controls-assets/switch-off.svg'))).toBe('bc457ac885d6b71d357451f057d515a463460bd2b57a6ac957ad86279f9cb7b7');
    expect(sha256(read('./selection-controls-assets/switch-on.svg'))).toBe('960e63d9a772ca28795b26f2cc72f709e7aa106400b21ffa08a5695d972af3f6');
    for (const name of ['switch-off', 'switch-on']) {
      const svg = read(`./selection-controls-assets/${name}.svg`).toString('utf8');
      expect(svg).toMatch(/^<svg [^>]*width="38" height="20" viewBox="0 0 38 20" /);
      expect(svg).not.toMatch(/<script|<foreignObject|href=|\son[a-z]+=|url\(/i);
    }
  });
  it('binds every recorded source file to its current bytes and keeps the upstream MIT attribution', () => {
    expect(Object.keys(source.files).sort()).toEqual(['selection-controls-assets/switch-off.svg', 'selection-controls-assets/switch-on.svg',
      'selection-controls-glyphs/Icon.tsx', 'selection-controls-glyphs/IconCheckCircle.tsx', 'selection-controls-glyphs/LICENSE']);
    for (const [path, digest] of Object.entries(source.files)) expect(sha256(read(`./${path}`))).toBe(digest);
    expect(read('./selection-controls-glyphs/LICENSE')).toEqual(read('./status-icon-glyphs/LICENSE'));
    expect(read('./selection-controls-glyphs/LICENSE').toString('utf8')).toContain('MIT License');
    expect(source.sources.check_circle.upstream).toBe(`https://github.com/figma/sds/blob/${source.sds_commit}/src/ui/icons/IconCheckCircle.tsx`);
    expect(source.sources.check_circle.git_blob).toBe('9805fb3c1e23edee4354e1038bafae5f87d265eb');
  });
  it('keeps the focus ring an outline, with no motion, shadow or runtime remote source', () => {
    const css = read('./selection-controls.css').toString('utf8');
    expect(css).toContain('.ot-selection-control:focus-visible { outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }');
    expect(css).not.toMatch(/transition|animation|@keyframes|box-shadow|https?:|@import/);
    expect(css.match(/#[0-9a-f]{3,8}\b/gi)).toEqual(['#1e1e1e']);
    for (const name of ['selection-controls.tsx', 'selection-controls.css', 'selection-controls.fixtures.ts', 'testing/selection-controls-gallery.tsx']) {
      expect(read(`./${name}`).toString('utf8')).not.toMatch(/figma\.com|localStorage|sessionStorage|fetch\(|setTimeout|setInterval|useState\(.*checked/);
    }
    expect(read('./selection-controls.tsx').toString('utf8')).not.toMatch(/useState|useEffect|useRef|import\.meta/);
  });
  it('declares a unique capture for every registered mode, width, text scale and state', () => {
    const names = selectionControlCaptureInventory();
    expect(names).toHaveLength(74);
    expect(new Set(names).size).toBe(74);
    for (const name of names) expect(name).toMatch(/^selection-controls-(customer-controlled|opentour-managed)-(320|768|1280|1440)-(100|200)-[a-z-]+\.png$/);
    for (const mode of fixtures.modes) for (const { width } of fixtures.viewports) for (const scale of fixtures.scales) {
      expect(names).toContain(`selection-controls-${mode}-${width}-${scale}-gallery.png`);
    }
  });
});
