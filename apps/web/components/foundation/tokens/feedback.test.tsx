import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { DataState, Progress, Toast, dataStates, progressStates, toastStates } from './feedback';
import type { DataStateKind, PersistentAnnouncement, ProgressState, ToastAnnouncement, ToastState } from './feedback';
import { feedbackFixtures as fixtures } from './feedback.fixtures';
import tokens from '../../../styles/product-tokens.json';

const tags = (html: string) => html.match(/<[a-z]+/g);

describe('data state presentation contract', () => {
  it.each(fixtures.dataStates)('renders the caller $state title then body as one polite persistent status', ({ state, title, body }) => {
    const html = renderToStaticMarkup(<DataState state={state} title={title} body={body} />);
    // Exact markup: no alert, no assertive region, no indicator, no number and no state name of its own.
    expect(html).toBe(`<div role="status" class="ot-data-state ot-data-state--${state}">` +
      `<p class="ot-data-state-title">${title}</p><p class="ot-data-state-body">${body}</p></div>`);
  });
  it('declares each design state once in variant order and carries a generated example for every one', () => {
    expect(dataStates).toEqual(['loading', 'empty', 'partial', 'error', 'denied', 'stale', 'offline', 'revoked', 'unknown-outcome']);
    expect(fixtures.dataStates.map(example => example.state)).toEqual(dataStates);
  });
  it('renders plain text with no live region when the caller turns announcement off', () => {
    const html = renderToStaticMarkup(<DataState state="stale" title="Generated" body="Generated" announce="off" />);
    expect(html).toBe('<div class="ot-data-state ot-data-state--stale">' +
      '<p class="ot-data-state-title">Generated</p><p class="ot-data-state-body">Generated</p></div>');
  });
  it.each(['assertive', 'alert', '', null])('rejects the announcement %j because a persistent data state is never an alert', announce => {
    expect(() => renderToStaticMarkup(<DataState state="error" title="Generated" body="Generated"
      announce={announce as PersistentAnnouncement} />)).toThrow('politely or not at all');
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<DataState state="unknown-outcome" title={'<b>generated</b>'} body={'<script>generated</script>'} />);
    expect(html).toContain('&lt;b&gt;generated&lt;/b&gt;');
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(tags(html)).toEqual(['<div', '<p', '<p']);
  });
  it.each(['', '   ', '\n'])('rejects a missing visible title or body %j', text => {
    expect(() => renderToStaticMarkup(<DataState state="loading" title={text} body="Generated" />)).toThrow('visible');
    expect(() => renderToStaticMarkup(<DataState state="loading" title="Generated" body={text} />)).toThrow('visible');
  });
  it.each(['unknown', 'failed', 'complete', 'Loading', '', undefined])('rejects the undeclared state %j rather than rendering an undesigned state', state => {
    expect(() => renderToStaticMarkup(<DataState state={state as DataStateKind} title="Generated" body="Generated" />))
      .toThrow('declared data state');
  });
});

describe('progress presentation contract', () => {
  it.each(fixtures.progress)('renders the caller $state title then body as one polite status and never as a progressbar', ({ state, title, body }) => {
    const html = renderToStaticMarkup(<Progress state={state} title={title} body={body} />);
    // Exact markup: no progressbar, no value, no indicator, no percentage and no state name of its own.
    expect(html).toBe(`<div role="status" class="ot-progress ot-progress--${state}">` +
      `<p class="ot-progress-title">${title}</p><p class="ot-progress-body">${body}</p></div>`);
  });
  it('declares each design state once in variant order and carries a generated example for every one', () => {
    expect(progressStates).toEqual(['queued', 'running', 'stopping', 'complete', 'failed']);
    expect(fixtures.progress.map(example => example.state)).toEqual(progressStates);
  });
  it('renders plain text with no live region when the caller turns announcement off', () => {
    const html = renderToStaticMarkup(<Progress state="running" title="Generated" body="Generated" announce="off" />);
    expect(html).toBe('<div class="ot-progress ot-progress--running">' +
      '<p class="ot-progress-title">Generated</p><p class="ot-progress-body">Generated</p></div>');
  });
  it.each(['assertive', 'alert', '', null])('rejects the announcement %j because a persistent progress state never interrupts', announce => {
    expect(() => renderToStaticMarkup(<Progress state="failed" title="Generated" body="Generated"
      announce={announce as PersistentAnnouncement} />)).toThrow('politely or not at all');
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Progress state="running" title={'<b>generated</b>'} body={'<script>generated</script>'} />);
    expect(html).toContain('&lt;b&gt;generated&lt;/b&gt;');
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(tags(html)).toEqual(['<div', '<p', '<p']);
  });
  it.each(['', '   ', '\n'])('rejects a missing visible title or body %j', text => {
    expect(() => renderToStaticMarkup(<Progress state="queued" title={text} body="Generated" />)).toThrow('visible');
    expect(() => renderToStaticMarkup(<Progress state="queued" title="Generated" body={text} />)).toThrow('visible');
  });
  it.each(['error', 'completed', 'accepted', 'Running', '', undefined])('rejects the undeclared state %j rather than rendering an undesigned state', state => {
    expect(() => renderToStaticMarkup(<Progress state={state as ProgressState} title="Generated" body="Generated" />))
      .toThrow('declared progress state');
  });
});

describe('toast presentation contract', () => {
  it.each(fixtures.toasts)('renders the caller $state title then body as a polite status by default', ({ state, title, body }) => {
    const html = renderToStaticMarkup(<Toast state={state} title={title} body={body} />);
    // Exact markup: polite even when failed, with no dismiss control and no state name of its own.
    expect(html).toBe(`<div role="status" class="ot-toast ot-toast--${state}">` +
      `<p class="ot-toast-title">${title}</p><p class="ot-toast-body">${body}</p></div>`);
  });
  it('declares each design state once in variant order and keeps accepted distinct from confirmed', () => {
    expect(toastStates).toEqual(['accepted', 'confirmed', 'failed', 'unknown']);
    expect(fixtures.toasts.map(example => example.state)).toEqual(toastStates);
  });
  it('interrupts only for a failed toast that the caller marks assertive', () => {
    const html = renderToStaticMarkup(<Toast state="failed" title="Generated" body="Generated" announce="assertive" />);
    expect(html).toBe('<div role="alert" class="ot-toast ot-toast--failed">' +
      '<p class="ot-toast-title">Generated</p><p class="ot-toast-body">Generated</p></div>');
  });
  it.each(['accepted', 'confirmed', 'unknown'] as const)('rejects an assertive %s toast', state => {
    expect(() => renderToStaticMarkup(<Toast state={state} title="Generated" body="Generated" announce="assertive" />))
      .toThrow('Only a failed toast');
  });
  it.each(['off', 'alert', '', null])('rejects the announcement %j because a toast is always announced', announce => {
    expect(() => renderToStaticMarkup(<Toast state="failed" title="Generated" body="Generated"
      announce={announce as ToastAnnouncement} />)).toThrow('politely or assertively');
  });
  it('escapes caller text instead of introducing markup, a dismiss control or content-derived actions', () => {
    const html = renderToStaticMarkup(<Toast state="unknown" title={'<b>generated</b>'} body={'<script>generated</script>'} />);
    expect(html).toContain('&lt;b&gt;generated&lt;/b&gt;');
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(tags(html)).toEqual(['<div', '<p', '<p']);
  });
  it.each(['', '   ', '\n'])('rejects a missing visible title or body %j', text => {
    expect(() => renderToStaticMarkup(<Toast state="accepted" title={text} body="Generated" />)).toThrow('visible');
    expect(() => renderToStaticMarkup(<Toast state="accepted" title="Generated" body={text} />)).toThrow('visible');
  });
  it.each(['complete', 'error', 'unknown-outcome', 'success', '', undefined])('rejects the undeclared state %j rather than rendering an undesigned state', state => {
    expect(() => renderToStaticMarkup(<Toast state={state as ToastState} title="Generated" body="Generated" />))
      .toThrow('declared toast state');
  });
});

describe('feedback components only render what they are told', () => {
  const source = readFileSync(new URL('./feedback.tsx', import.meta.url), 'utf8');
  it('holds no state, effect, timer, focus, storage or network access that could change or dismiss a state', () => {
    expect(source).not.toMatch(/\buse[A-Z]\w*\s*\(|['"]use client['"]/);
    expect(source).not.toMatch(/setTimeout|setInterval|requestAnimationFrame|requestIdleCallback|\bDate\b|performance\./);
    expect(source).not.toMatch(/\.focus\(|autoFocus|tabIndex|scrollIntoView/);
    expect(source).not.toMatch(/\bfetch\b|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|indexedDB|cookie/);
    expect(source).not.toMatch(/\bdocument\b|\bwindow\b|\bglobalThis\b|\bnavigator\b/);
  });
  it('adds no focus stop, explicit live attribute, busy flag or control to any state', () => {
    const html = [
      ...fixtures.dataStates.map(example => renderToStaticMarkup(<DataState {...example} />)),
      ...fixtures.progress.map(example => renderToStaticMarkup(<Progress {...example} />)),
      ...fixtures.toasts.map(example => renderToStaticMarkup(<Toast {...example} />)),
      renderToStaticMarkup(<Toast state="failed" title="Generated" body="Generated" announce="assertive" />),
    ];
    expect(html).toHaveLength(dataStates.length + progressStates.length + toastStates.length + 1);
    for (const markup of html) {
      expect(markup).not.toMatch(/tabindex|autofocus|aria-live|aria-atomic|aria-busy|aria-valuenow|<button|<a |<input|<progress|<img|<svg|style=/);
      expect(tags(markup)).toEqual(['<div', '<p', '<p']);
    }
  });
  it('renders the same markup for the same props, so a repeated identical update leaves nothing to announce', () => {
    for (const example of fixtures.progress) {
      expect(renderToStaticMarkup(<Progress {...example} />)).toBe(renderToStaticMarkup(<Progress {...example} />));
    }
    for (const example of fixtures.toasts) {
      expect(renderToStaticMarkup(<Toast {...example} />)).toBe(renderToStaticMarkup(<Toast {...example} />));
    }
  });
});

describe('feedback design token binding', () => {
  const tokenCss = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');
  const css = readFileSync(new URL('./feedback.css', import.meta.url), 'utf8');
  const rules = css.split('\n').filter(line => line.startsWith('.ot-'));
  type Color = keyof typeof tokens.colors;
  type Pair = { fill: Color; text: Color[] };
  const neutral: Pair = { fill: 'surface-base', text: ['text-primary', 'text-secondary'] };
  const tone = (name: 'success' | 'hold' | 'failure' | 'unknown'): Pair => ({ fill: `${name}-background`, text: [`${name}-text`] });
  // Fill and text variables of every variant of Figma component sets 12:109, 12:125 and 12:138.
  const design: Record<string, Record<string, Pair>> = {
    'data-state': { loading: neutral, empty: neutral, partial: neutral, error: tone('failure'), denied: tone('unknown'),
      stale: tone('unknown'), offline: tone('unknown'), revoked: tone('unknown'), 'unknown-outcome': tone('unknown') },
    progress: { queued: neutral, running: neutral, stopping: tone('hold'), complete: tone('success'), failed: tone('failure') },
    toast: { accepted: neutral, confirmed: tone('success'), failed: tone('failure'), unknown: tone('unknown') },
  };
  const variants = Object.entries(design).flatMap(([block, states]) => Object.entries(states).map(([state, pair]) => ({ block, state, pair })));

  it('covers exactly the declared states of the three components', () => {
    expect(Object.keys(design['data-state'])).toEqual(dataStates);
    expect(Object.keys(design.progress)).toEqual(progressStates);
    expect(Object.keys(design.toast)).toEqual(toastStates);
  });
  it('binds every state to the fill and text variables of its design variant', () => {
    for (const { block, state, pair } of variants) {
      expect(rules).toContain(`.ot-${block}--${state} { background: var(--ot-${pair.fill}); color: var(--ot-${pair.text[0]}); }`);
      const body = `.ot-${block}--${state} > .ot-${block}-body { color: var(--ot-${pair.text[1]}); }`;
      if (pair.text.length === 2) expect(rules).toContain(body);
      else expect(rules.filter(rule => rule.startsWith(`.ot-${block}--${state} >`))).toEqual([]);
    }
    // One fill and one text colour per variant, one more text colour where the body differs, and no others.
    expect(css.match(/(?<![\w-])background(?:-color)?:/g)).toHaveLength(variants.length);
    expect(css.match(/(?<![\w-])color:/g)).toHaveLength(variants.length + variants.filter(({ pair }) => pair.text.length === 2).length);
  });
  it('takes colour, spacing, radius and type from recorded tokens with the 1px border as the only literal length', () => {
    expect(css.match(/#[0-9a-f]{3,8}\b/gi)).toBeNull();
    expect(css.match(/\b(?:rgb|hsl|oklch|color-mix)\(/g)).toBeNull();
    expect(css.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em|%)(?![\w-])/g)).toEqual(['100%', '1px']);
    const names = [...css.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)].map(match => match[1]);
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) expect(tokenCss).toContain(`  ${name}: `);
    expect(css.match(/var\(/g)).toHaveLength(names.length);
    expect(rules[0]).toBe('.ot-data-state, .ot-progress, .ot-toast { display: flex; flex-direction: column; gap: var(--ot-space-8); ' +
      'box-sizing: border-box; inline-size: 100%; min-inline-size: 0; padding: var(--ot-space-16); ' +
      'border: 1px solid var(--ot-border-default); border-radius: var(--ot-radius-8); font-family: Geist, sans-serif; overflow-wrap: anywhere; }');
    for (const [name, style] of [['title', 'label'], ['body', 'small']]) {
      expect(rules).toContain(`.ot-data-state-${name}, .ot-progress-${name}, .ot-toast-${name} { font-size: var(--ot-type-${style}-size); ` +
        `font-weight: var(--ot-type-${style}-weight); line-height: var(--ot-type-${style}-line); }`);
    }
  });
  it('declares nothing that could clip text, stop it wrapping, move, fade or cast a shadow', () => {
    expect(css).not.toMatch(/overflow(?:-[xy]|-inline|-block)?:|text-overflow|white-space|line-clamp|(?<!-)block-size|(?<![\w-])height/);
    expect(css).not.toMatch(/animation|transition|transform|@keyframes|box-shadow|text-shadow|filter|opacity/);
    expect(css).not.toMatch(/position:|z-index|(?<![\w-])(?:top|right|bottom|left|inset)[\w-]*:|max-inline-size|(?<![\w-])width/);
  });
  it('keeps the card boundary on a system colour in forced colours, where the title text alone carries the state', () => {
    expect(css).toContain('@media (forced-colors: active) {\n  .ot-data-state, .ot-progress, .ot-toast { border-color: CanvasText; }\n}');
    expect(css).not.toContain('forced-color-adjust');
  });
  function luminance(hex: string) {
    const channels = hex.slice(1).match(/../g)!.map(channel => parseInt(channel, 16) / 255)
      .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  it('measures normal-text contrast for every state text and fill pair', () => {
    let measured = 0;
    for (const { pair } of variants) for (const text of pair.text) {
      const values = [luminance(tokens.colors[text]), luminance(tokens.colors[pair.fill])].sort((a, b) => b - a);
      expect((values[0] + 0.05) / (values[1] + 0.05)).toBeGreaterThanOrEqual(4.5);
      measured += 1;
    }
    expect(measured).toBe(24);
  });
});
