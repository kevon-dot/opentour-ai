import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { Notice, noticeTones } from './index';
import type { NoticeTone } from './index';
import { presentationFixtures as fixtures } from './fixtures';
import tokens from '../../../styles/product-tokens.json';

describe('notice presentation contract', () => {
  testCases(fixtures.notices)('renders the caller $tone title then body as one polite persistent status', ({ tone, title, body }) => {
    const html = renderToStaticMarkup(<Notice tone={tone} title={title} body={body} />);
    // Exact markup: no alert, no assertive region, no dismiss control and no state name of its own.
    assert.equal(html, `<div role="status" class="ot-notice ot-notice--${tone}">` +
      `<p class="ot-notice-title">${title}</p><p class="ot-notice-body">${body}</p></div>`);
  });
  it('declares each design state once and carries a generated example for every one', () => {
    assert.deepEqual(noticeTones, ['info', 'success', 'hold', 'failure', 'unknown']);
    assert.deepEqual(fixtures.notices.map(notice => notice.tone), noticeTones);
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Notice tone="unknown" title={'<b>generated</b>'} body={'<script>generated</script>'} />);
    assert.ok((html)?.includes('&lt;b&gt;generated&lt;/b&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.ok((html)?.includes('<script>') === false);
    assert.deepEqual(html.match(/<[a-z]+/g), ['<div', '<p', '<p']);
  });
  testCases(['', '   ', '\n'])('rejects a missing visible title or body %j', text => {
    assert.throws(() => renderToStaticMarkup(<Notice tone="info" title={text} body={fixtures.notices[0].body} />), /visible/);
    assert.throws(() => renderToStaticMarkup(<Notice tone="info" title={fixtures.notices[0].title} body={text} />), /visible/);
  });
  testCases(['learning', 'failed', '', undefined])('rejects the undeclared tone %j rather than rendering an undesigned state', tone => {
    assert.throws(() => renderToStaticMarkup(<Notice tone={tone as NoticeTone} title={fixtures.notices[0].title}
      body={fixtures.notices[0].body} />), /declared notice tone/);
  });
});

describe('notice design token binding', () => {
  const css = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');
  const rules = css.split('\n').filter(line => line.startsWith('.ot-notice'));
  const pairs: Record<NoticeTone, { fill: keyof typeof tokens.colors; text: (keyof typeof tokens.colors)[] }> = {
    info: { fill: 'surface-base', text: ['text-primary', 'text-secondary'] },
    success: { fill: 'success-background', text: ['success-text'] },
    hold: { fill: 'hold-background', text: ['hold-text'] },
    failure: { fill: 'failure-background', text: ['failure-text'] },
    unknown: { fill: 'unknown-background', text: ['unknown-text'] },
  };
  it('binds every tone to the fill and text variables of its design variant', () => {
    for (const tone of noticeTones) {
      assert.ok((rules)?.includes(`.ot-notice--${tone} { background: var(--ot-${pairs[tone].fill}); color: var(--ot-${pairs[tone].text[0]}); }`));
    }
    assert.ok((rules)?.includes('.ot-notice--info > .ot-notice-body { color: var(--ot-text-secondary); }'));
    assert.equal((rules.filter(rule => rule.includes('color:')))?.length, noticeTones.length + 1);
  });
  it('takes colour, spacing, radius and type from recorded tokens with the 1px border as the only literal length', () => {
    const text = rules.join('\n');
    assert.ok(rules.length >= 9);
    assert.equal(text.match(/#[0-9a-f]{3,8}\b/gi), null);
    assert.deepEqual(text.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em)\b/g), ['1px']);
    for (const [, name] of text.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)) assert.ok((css)?.includes(`  ${name}: `));
    assert.ok((text)?.includes('gap: var(--ot-space-8); '));
    assert.ok((text)?.includes('padding: var(--ot-space-16); '));
    assert.ok((text)?.includes('border: 1px solid var(--ot-border-default); border-radius: var(--ot-radius-8); '));
    for (const [name, style] of [['title', 'label'], ['body', 'small']]) {
      assert.ok((rules)?.includes(`.ot-notice-${name} { font-size: var(--ot-type-${style}-size); ` +
        `font-weight: var(--ot-type-${style}-weight); line-height: var(--ot-type-${style}-line); }`));
    }
  });
  it('fills its container and declares nothing that could clip or stop text wrapping', () => {
    const text = rules.join('\n');
    assert.ok((text)?.includes('inline-size: 100%; '));
    assert.ok((text)?.includes('overflow-wrap: anywhere; '));
    assert.doesNotMatch(text, /overflow(?:-[xy]|-inline|-block)?:|text-overflow|white-space|line-clamp|(?<!-)block-size|(?<![\w-])height/);
  });
  function luminance(hex: string) {
    const channels = hex.slice(1).match(/../g)!.map(channel => parseInt(channel, 16) / 255)
      .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  it('measures normal-text contrast for every notice text and fill pair', () => {
    for (const tone of noticeTones) for (const text of pairs[tone].text) {
      const values = [luminance(tokens.colors[text]), luminance(tokens.colors[pairs[tone].fill])].sort((a, b) => b - a);
      assert.ok((values[0] + 0.05) / (values[1] + 0.05) >= 4.5);
    }
  });
});
