import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { DataState, Progress, Toast, dataStates, progressStates, toastStates } from './feedback';
import type { DataStateKind, PersistentAnnouncement, ProgressState, ToastAnnouncement, ToastState } from './feedback';
import { feedbackFixtures as fixtures } from './feedback.fixtures';
import tokens from '../../../styles/product-tokens.json';

const tags = (html: string) => html.match(/<[a-z]+/g);

describe('data state presentation contract', () => {
  testCases(fixtures.dataStates)('renders the caller $state title then body as one polite persistent status', ({ state, title, body }) => {
    const html = renderToStaticMarkup(<DataState state={state} title={title} body={body} />);
    // Exact markup: no alert, no assertive region, no indicator, no number and no state name of its own.
    assert.equal(html, `<div role="status" class="ot-data-state ot-data-state--${state}">` +
      `<p class="ot-data-state-title">${title}</p><p class="ot-data-state-body">${body}</p></div>`);
  });
  it('declares each design state once in variant order and carries a generated example for every one', () => {
    assert.deepEqual(dataStates, ['loading', 'empty', 'partial', 'error', 'denied', 'stale', 'offline', 'revoked', 'unknown-outcome']);
    assert.deepEqual(fixtures.dataStates.map(example => example.state), dataStates);
  });
  it('renders plain text with no live region when the caller turns announcement off', () => {
    const html = renderToStaticMarkup(<DataState state="stale" title="Generated" body="Generated" announce="off" />);
    assert.equal(html, '<div class="ot-data-state ot-data-state--stale">' +
      '<p class="ot-data-state-title">Generated</p><p class="ot-data-state-body">Generated</p></div>');
  });
  testCases(['assertive', 'alert', '', null])('rejects the announcement %j because a persistent data state is never an alert', announce => {
    assert.throws(() => renderToStaticMarkup(<DataState state="error" title="Generated" body="Generated"
      announce={announce as PersistentAnnouncement} />), /politely or not at all/);
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<DataState state="unknown-outcome" title={'<b>generated</b>'} body={'<script>generated</script>'} />);
    assert.ok((html)?.includes('&lt;b&gt;generated&lt;/b&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(tags(html), ['<div', '<p', '<p']);
  });
  testCases(['', '   ', '\n'])('rejects a missing visible title or body %j', text => {
    assert.throws(() => renderToStaticMarkup(<DataState state="loading" title={text} body="Generated" />), /visible/);
    assert.throws(() => renderToStaticMarkup(<DataState state="loading" title="Generated" body={text} />), /visible/);
  });
  testCases(['unknown', 'failed', 'complete', 'Loading', '', undefined])('rejects the undeclared state %j rather than rendering an undesigned state', state => {
    assert.throws(() => renderToStaticMarkup(<DataState state={state as DataStateKind} title="Generated" body="Generated" />), /declared data state/);
  });
});

describe('progress presentation contract', () => {
  testCases(fixtures.progress)('renders the caller $state title then body as one polite status and never as a progressbar', ({ state, title, body }) => {
    const html = renderToStaticMarkup(<Progress state={state} title={title} body={body} />);
    // Exact markup: no progressbar, no value, no indicator, no percentage and no state name of its own.
    assert.equal(html, `<div role="status" class="ot-progress ot-progress--${state}">` +
      `<p class="ot-progress-title">${title}</p><p class="ot-progress-body">${body}</p></div>`);
  });
  it('declares each design state once in variant order and carries a generated example for every one', () => {
    assert.deepEqual(progressStates, ['queued', 'running', 'stopping', 'complete', 'failed']);
    assert.deepEqual(fixtures.progress.map(example => example.state), progressStates);
  });
  it('renders plain text with no live region when the caller turns announcement off', () => {
    const html = renderToStaticMarkup(<Progress state="running" title="Generated" body="Generated" announce="off" />);
    assert.equal(html, '<div class="ot-progress ot-progress--running">' +
      '<p class="ot-progress-title">Generated</p><p class="ot-progress-body">Generated</p></div>');
  });
  testCases(['assertive', 'alert', '', null])('rejects the announcement %j because a persistent progress state never interrupts', announce => {
    assert.throws(() => renderToStaticMarkup(<Progress state="failed" title="Generated" body="Generated"
      announce={announce as PersistentAnnouncement} />), /politely or not at all/);
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Progress state="running" title={'<b>generated</b>'} body={'<script>generated</script>'} />);
    assert.ok((html)?.includes('&lt;b&gt;generated&lt;/b&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(tags(html), ['<div', '<p', '<p']);
  });
  testCases(['', '   ', '\n'])('rejects a missing visible title or body %j', text => {
    assert.throws(() => renderToStaticMarkup(<Progress state="queued" title={text} body="Generated" />), /visible/);
    assert.throws(() => renderToStaticMarkup(<Progress state="queued" title="Generated" body={text} />), /visible/);
  });
  testCases(['error', 'completed', 'accepted', 'Running', '', undefined])('rejects the undeclared state %j rather than rendering an undesigned state', state => {
    assert.throws(() => renderToStaticMarkup(<Progress state={state as ProgressState} title="Generated" body="Generated" />), /declared progress state/);
  });
});

describe('toast presentation contract', () => {
  testCases(fixtures.toasts)('renders the caller $state title then body as a polite status by default', ({ state, title, body }) => {
    const html = renderToStaticMarkup(<Toast state={state} title={title} body={body} />);
    // Exact markup: polite even when failed, with no dismiss control and no state name of its own.
    assert.equal(html, `<div role="status" class="ot-toast ot-toast--${state}">` +
      `<p class="ot-toast-title">${title}</p><p class="ot-toast-body">${body}</p></div>`);
  });
  it('declares each design state once in variant order and keeps accepted distinct from confirmed', () => {
    assert.deepEqual(toastStates, ['accepted', 'confirmed', 'failed', 'unknown']);
    assert.deepEqual(fixtures.toasts.map(example => example.state), toastStates);
  });
  it('interrupts only for a failed toast that the caller marks assertive', () => {
    const html = renderToStaticMarkup(<Toast state="failed" title="Generated" body="Generated" announce="assertive" />);
    assert.equal(html, '<div role="alert" class="ot-toast ot-toast--failed">' +
      '<p class="ot-toast-title">Generated</p><p class="ot-toast-body">Generated</p></div>');
  });
  testCases(['accepted', 'confirmed', 'unknown'] as const)('rejects an assertive %s toast', state => {
    assert.throws(() => renderToStaticMarkup(<Toast state={state} title="Generated" body="Generated" announce="assertive" />), /Only a failed toast/);
  });
  testCases(['off', 'alert', '', null])('rejects the announcement %j because a toast is always announced', announce => {
    assert.throws(() => renderToStaticMarkup(<Toast state="failed" title="Generated" body="Generated"
      announce={announce as ToastAnnouncement} />), /politely or assertively/);
  });
  it('escapes caller text instead of introducing markup, a dismiss control or content-derived actions', () => {
    const html = renderToStaticMarkup(<Toast state="unknown" title={'<b>generated</b>'} body={'<script>generated</script>'} />);
    assert.ok((html)?.includes('&lt;b&gt;generated&lt;/b&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(tags(html), ['<div', '<p', '<p']);
  });
  testCases(['', '   ', '\n'])('rejects a missing visible title or body %j', text => {
    assert.throws(() => renderToStaticMarkup(<Toast state="accepted" title={text} body="Generated" />), /visible/);
    assert.throws(() => renderToStaticMarkup(<Toast state="accepted" title="Generated" body={text} />), /visible/);
  });
  testCases(['complete', 'error', 'unknown-outcome', 'success', '', undefined])('rejects the undeclared state %j rather than rendering an undesigned state', state => {
    assert.throws(() => renderToStaticMarkup(<Toast state={state as ToastState} title="Generated" body="Generated" />), /declared toast state/);
  });
});

describe('feedback components only render what they are told', () => {
  const source = readFileSync(new URL('./feedback.tsx', import.meta.url), 'utf8');
  it('holds no state, effect, timer, focus, storage or network access that could change or dismiss a state', () => {
    assert.doesNotMatch(source, /\buse[A-Z]\w*\s*\(|['"]use client['"]/);
    assert.doesNotMatch(source, /setTimeout|setInterval|requestAnimationFrame|requestIdleCallback|\bDate\b|performance\./);
    assert.doesNotMatch(source, /\.focus\(|autoFocus|tabIndex|scrollIntoView/);
    assert.doesNotMatch(source, /\bfetch\b|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|indexedDB|cookie/);
    assert.doesNotMatch(source, /\bdocument\b|\bwindow\b|\bglobalThis\b|\bnavigator\b/);
  });
  it('adds no focus stop, explicit live attribute, busy flag or control to any state', () => {
    const html = [
      ...fixtures.dataStates.map(example => renderToStaticMarkup(<DataState {...example} />)),
      ...fixtures.progress.map(example => renderToStaticMarkup(<Progress {...example} />)),
      ...fixtures.toasts.map(example => renderToStaticMarkup(<Toast {...example} />)),
      renderToStaticMarkup(<Toast state="failed" title="Generated" body="Generated" announce="assertive" />),
    ];
    assert.equal((html)?.length, dataStates.length + progressStates.length + toastStates.length + 1);
    for (const markup of html) {
      assert.doesNotMatch(markup, /tabindex|autofocus|aria-live|aria-atomic|aria-busy|aria-valuenow|<button|<a |<input|<progress|<img|<svg|style=/);
      assert.deepEqual(tags(markup), ['<div', '<p', '<p']);
    }
  });
  it('renders the same markup for the same props, so a repeated identical update leaves nothing to announce', () => {
    for (const example of fixtures.progress) {
      assert.equal(renderToStaticMarkup(<Progress {...example} />), renderToStaticMarkup(<Progress {...example} />));
    }
    for (const example of fixtures.toasts) {
      assert.equal(renderToStaticMarkup(<Toast {...example} />), renderToStaticMarkup(<Toast {...example} />));
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
    assert.deepEqual(Object.keys(design['data-state']), dataStates);
    assert.deepEqual(Object.keys(design.progress), progressStates);
    assert.deepEqual(Object.keys(design.toast), toastStates);
  });
  it('binds every state to the fill and text variables of its design variant', () => {
    for (const { block, state, pair } of variants) {
      assert.ok((rules)?.includes(`.ot-${block}--${state} { background: var(--ot-${pair.fill}); color: var(--ot-${pair.text[0]}); }`));
      const body = `.ot-${block}--${state} > .ot-${block}-body { color: var(--ot-${pair.text[1]}); }`;
      if (pair.text.length === 2) assert.ok((rules)?.includes(body));
      else assert.deepEqual(rules.filter(rule => rule.startsWith(`.ot-${block}--${state} >`)), []);
    }
    // One fill and one text colour per variant, one more text colour where the body differs, and no others.
    assert.equal((css.match(/(?<![\w-])background(?:-color)?:/g))?.length, variants.length);
    assert.equal((css.match(/(?<![\w-])color:/g))?.length, variants.length + variants.filter(({ pair }) => pair.text.length === 2).length);
  });
  it('takes colour, spacing, radius and type from recorded tokens with the 1px border as the only literal length', () => {
    assert.equal(css.match(/#[0-9a-f]{3,8}\b/gi), null);
    assert.equal(css.match(/\b(?:rgb|hsl|oklch|color-mix)\(/g), null);
    assert.deepEqual(css.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em|%)(?![\w-])/g), ['100%', '1px']);
    const names = [...css.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)].map(match => match[1]);
    assert.ok(names.length > 0);
    for (const name of names) assert.ok((tokenCss)?.includes(`  ${name}: `));
    assert.equal((css.match(/var\(/g))?.length, names.length);
    assert.equal(rules[0], '.ot-data-state, .ot-progress, .ot-toast { display: flex; flex-direction: column; gap: var(--ot-space-8); ' +
      'box-sizing: border-box; inline-size: 100%; min-inline-size: 0; padding: var(--ot-space-16); ' +
      'border: 1px solid var(--ot-border-default); border-radius: var(--ot-radius-8); font-family: Geist, sans-serif; overflow-wrap: anywhere; }');
    for (const [name, style] of [['title', 'label'], ['body', 'small']]) {
      assert.ok((rules)?.includes(`.ot-data-state-${name}, .ot-progress-${name}, .ot-toast-${name} { font-size: var(--ot-type-${style}-size); ` +
        `font-weight: var(--ot-type-${style}-weight); line-height: var(--ot-type-${style}-line); }`));
    }
  });
  it('declares nothing that could clip text, stop it wrapping, move, fade or cast a shadow', () => {
    assert.doesNotMatch(css, /overflow(?:-[xy]|-inline|-block)?:|text-overflow|white-space|line-clamp|(?<!-)block-size|(?<![\w-])height/);
    assert.doesNotMatch(css, /animation|transition|transform|@keyframes|box-shadow|text-shadow|filter|opacity/);
    assert.doesNotMatch(css, /position:|z-index|(?<![\w-])(?:top|right|bottom|left|inset)[\w-]*:|max-inline-size|(?<![\w-])width/);
  });
  it('keeps the card boundary on a system colour in forced colours, where the title text alone carries the state', () => {
    assert.ok((css)?.includes('@media (forced-colors: active) {\n  .ot-data-state, .ot-progress, .ot-toast { border-color: CanvasText; }\n}'));
    assert.ok((css)?.includes('forced-color-adjust') === false);
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
      assert.ok((values[0] + 0.05) / (values[1] + 0.05) >= 4.5);
      measured += 1;
    }
    assert.equal(measured, 24);
  });
});
