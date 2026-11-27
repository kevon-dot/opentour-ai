import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { Breadcrumb, breadcrumbSeparator, Link, linkSizes, LiveRegion, liveRegionPolitenessLevels } from './primitives';
import type { BreadcrumbItem, LinkSize, LiveRegionPoliteness } from './primitives';
import { primitiveFixtures as fixtures } from './primitives.fixtures';
import tokens from '../../../styles/product-tokens.json';

const tags = (html: string) => html.match(/<[a-z]+/g);
const visibleText = (html: string) => html.replace(/<[^>]+>/g, '');

describe('link presentation contract', () => {
  testCases(fixtures.links)('renders the $size link as one native anchor to its destination', ({ size, label, href }) => {
    const html = renderToStaticMarkup(<Link size={size} label={label} href={href} />);
    // Exact markup: no button role, no new-window target and no arrow unless it is requested.
    assert.equal(html, `<a href="${href}" class="ot-link ot-link--${size}">${label}</a>`);
  });
  it('declares each designed text style once and carries a generated example for every one', () => {
    assert.deepEqual(linkSizes, ['small', 'small-strong', 'label']);
    assert.deepEqual(fixtures.links.map(link => link.size), linkSizes);
  });
  it('draws the trailing arrow as decoration that stays out of the accessible name', () => {
    const html = renderToStaticMarkup(<Link size="small-strong" label="Open generated example" href="/generated" trailingArrow />);
    assert.equal(html, '<a href="/generated" class="ot-link ot-link--small-strong">Open generated example' +
      '<span aria-hidden="true"> →</span></a>');
    assert.equal(visibleText(html), 'Open generated example →');
    assert.ok((renderToStaticMarkup(<Link size="small-strong" label="Open generated example" href="/generated"
      trailingArrow={false} />))?.includes('→') === false);
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Link size="label" label={'<script>generated</script>'} href={'/generated?a="b"&c=<d>'} />);
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.ok((html)?.includes('href="/generated?a=&quot;b&quot;&amp;c=&lt;d&gt;"'));
    assert.deepEqual(tags(html), ['<a']);
  });
  testCases(['', '   ', '\n'])('rejects a missing visible label or destination %j', text => {
    assert.throws(() => renderToStaticMarkup(<Link size="small" label={text} href="/generated" />), /visible/);
    assert.throws(() => renderToStaticMarkup(<Link size="small" label="Generated" href={text} />), /destination is required/);
  });
  testCases(['medium', 'body', '', undefined])('rejects the undeclared size %j rather than rendering an undesigned style', size => {
    assert.throws(() => renderToStaticMarkup(<Link size={size as LinkSize} label="Generated" href="/generated" />), /declared link size/);
  });
  testCases(fixtures.unsafeDestinations)('refuses the executable or unknown-scheme destination %j', href => {
    assert.throws(() => renderToStaticMarkup(<Link size="small" label="Generated" href={href} />), /must be a path or an http, https or mailto address/);
  });
  testCases(fixtures.safeDestinations)('keeps the relative or web destination %j unchanged', href => {
    assert.equal(renderToStaticMarkup(<Link size="small" label="Generated" href={href} />), `<a href="${href}" class="ot-link ot-link--small">Generated</a>`);
  });
  it('never calls the press observer while rendering', () => {
    assert.ok((renderToStaticMarkup(<Link size="small" label="Generated" href="/generated"
      onPress={() => { throw new Error('Rendering cannot follow a link'); }} />))?.includes('href="/generated"'));
  });
});

describe('breadcrumb presentation contract', () => {
  it('renders a named navigation landmark with an ordered trail and one current page', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={fixtures.breadcrumb} />);
    assert.equal(html, '<nav class="ot-breadcrumb" aria-label="Breadcrumb"><ol class="ot-breadcrumb-list" role="list">' +
      '<li class="ot-breadcrumb-item"><a class="ot-breadcrumb-link" href="/generated">Generated tenant A</a></li>' +
      '<li class="ot-breadcrumb-item"><span class="ot-breadcrumb-separator" aria-hidden="true">  /  </span>' +
      '<span aria-current="page">Generated workflow</span></li></ol></nav>');
  });
  it('reads as the designed single line: labels joined by a solidus with two spaces on each side', () => {
    assert.equal(breadcrumbSeparator, `${' '.repeat(2)}/${' '.repeat(2)}`);
    for (const items of [fixtures.breadcrumb, fixtures.breadcrumbDeep, fixtures.breadcrumbLong]) {
      const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={items} />);
      assert.equal(visibleText(html), items.map(item => item.label).join(`${' '.repeat(2)}/${' '.repeat(2)}`));
      assert.equal((html.match(/class="ot-breadcrumb-separator" aria-hidden="true"/g))?.length, items.length - 1);
      assert.equal((html.match(/<li /g))?.length, items.length);
    }
  });
  it('marks only the last item current, links only ancestors with a destination and leaves the rest as text', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={fixtures.breadcrumbDeep} />);
    assert.equal((html.match(/aria-current="page"/g))?.length, 1);
    assert.ok((html)?.includes('<span aria-current="page">Generated details</span></li></ol></nav>'));
    assert.equal((html.match(/<a /g))?.length, 2);
    assert.ok((html)?.includes('<span>Generated group</span>'));
    assert.ok((html)?.includes('role="button"') === false);
    assert.ok((html)?.includes('<button') === false);
  });
  it('renders a single current page without a separator or a link', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={[{ label: 'Generated workflow' }]} />);
    assert.deepEqual(tags(html), ['<nav', '<ol', '<li', '<span']);
    assert.equal(visibleText(html), 'Generated workflow');
  });
  it('escapes caller text instead of introducing markup', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={'<b>generated</b>'}
      items={[{ label: '<i>generated</i>', href: '/generated' }, { label: '<script>generated</script>' }]} />);
    assert.ok((html)?.includes('aria-label="&lt;b&gt;generated&lt;/b&gt;"'));
    assert.ok((html)?.includes('&lt;i&gt;generated&lt;/i&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(tags(html), ['<nav', '<ol', '<li', '<a', '<li', '<span', '<span']);
  });
  testCases(['', '   ', '\n'])('rejects a missing landmark name or item label %j', text => {
    assert.throws(() => renderToStaticMarkup(<Breadcrumb label={text} items={fixtures.breadcrumb} />), /visible/);
    assert.throws(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: text, href: '/generated' }, { label: 'Generated workflow' }]} />), /visible/);
    assert.throws(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: 'Generated tenant A', href: '/generated' }, { label: text }]} />), /visible/);
  });
  it('rejects an empty or absent trail', () => {
    for (const items of [[], undefined, null]) {
      assert.throws(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
        items={items as unknown as BreadcrumbItem[]} />), /at least one item/);
    }
  });
  it('rejects a destination on the current page rather than silently dropping it', () => {
    assert.throws(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: 'Generated tenant A', href: '/generated' }, { label: 'Generated workflow', href: '/generated/workflow' }]} />), /current breadcrumb item is not a link/);
  });
  testCases(['', ' ', ...fixtures.unsafeDestinations])('refuses the ancestor destination %j', href => {
    assert.throws(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: 'Generated tenant A', href }, { label: 'Generated workflow' }]} />), /destination/);
  });
  it('never calls the press observer while rendering', () => {
    assert.ok((renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={fixtures.breadcrumb}
      onItemPress={() => { throw new Error('Rendering cannot follow a link'); }} />))?.includes('href="/generated"'));
  });
});

describe('live region contract', () => {
  it('announces politely by default through the status role alone', () => {
    const html = renderToStaticMarkup(<LiveRegion message={fixtures.announcements[0].message} />);
    assert.equal(html, '<div role="status" class="ot-live-region">Generated status changed.</div>');
  });
  it('uses the alert role only when assertive is requested', () => {
    const html = renderToStaticMarkup(<LiveRegion {...fixtures.announcements[1]} />);
    assert.equal(html, '<div role="alert" class="ot-live-region">Generated status needs attention.</div>');
    assert.deepEqual(liveRegionPolitenessLevels, ['polite', 'assertive']);
  });
  testCases(liveRegionPolitenessLevels)('does not repeat the %s role as live attributes that some readers speak twice', politeness => {
    const html = renderToStaticMarkup(<LiveRegion message="Generated" politeness={politeness} />);
    assert.doesNotMatch(html, /aria-live|aria-atomic|aria-relevant|aria-busy/);
    assert.equal((html.match(/role=/g))?.length, 1);
  });
  it('stays mounted and silent for an empty message so later text is a change', () => {
    assert.equal(renderToStaticMarkup(<LiveRegion message="" />), '<div role="status" class="ot-live-region"></div>');
  });
  it('renders identical text identically, leaving nothing for a re-render to change or re-announce', () => {
    const render = () => renderToStaticMarkup(<LiveRegion message="Generated status changed." />);
    const first = render();
    for (let poll = 0; poll < 5; poll += 1) assert.equal(render(), first);
    // No per-render nonce, counter, timestamp or padding character is added to force a repeat.
    assert.equal(first, '<div role="status" class="ot-live-region">Generated status changed.</div>');
    assert.deepEqual(tags(first), ['<div']);
  });
  it('escapes caller text instead of introducing markup', () => {
    const html = renderToStaticMarkup(<LiveRegion message={'<script>generated</script>'} />);
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(tags(html), ['<div']);
  });
  testCases([undefined, null, 0, { text: 'Generated' }])('rejects the non-text message %j', message => {
    assert.throws(() => renderToStaticMarkup(<LiveRegion message={message as unknown as string} />), /must be text/);
  });
  testCases(['off', 'rude', '', null])('rejects the undeclared politeness %j', politeness => {
    assert.throws(() => renderToStaticMarkup(<LiveRegion message="Generated" politeness={politeness as LiveRegionPoliteness} />), /declared live region politeness/);
  });
});

describe('primitive design token binding', () => {
  const css = readFileSync(new URL('./primitives.css', import.meta.url), 'utf8');
  const tokenCss = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');
  const rules = css.split('\n').map(line => line.trim()).filter(line => line.startsWith('.ot-'));
  const visual = rules.filter(rule => !rule.startsWith('.ot-live-region'));
  const type = (style: string) => `font-size: var(--ot-type-${style}-size); font-weight: var(--ot-type-${style}-weight); ` +
    `line-height: var(--ot-type-${style}-line);`;

  it('binds every link size to the type tokens of its design text style and the link colour', () => {
    for (const size of linkSizes) assert.ok((rules)?.includes(`.ot-link--${size} { ${type(size)} }`));
    assert.equal((rules.filter(rule => rule.startsWith('.ot-link--')))?.length, linkSizes.length);
    const link = rules.find(rule => rule.startsWith('.ot-link {'))!;
    assert.ok((link)?.includes('color: var(--ot-text-link); '));
    assert.ok((link)?.includes('text-decoration: none; '));
  });
  it('draws the breadcrumb as one secondary small trail whose links keep the trail colour', () => {
    const trail = rules.find(rule => rule.startsWith('.ot-breadcrumb {'))!;
    assert.ok((trail)?.includes('color: var(--ot-text-secondary); '));
    assert.ok((trail)?.includes(`${type('small')} `));
    assert.ok((rules)?.includes('.ot-breadcrumb-link { color: inherit; text-decoration: none; }'));
    assert.ok((rules)?.includes('.ot-breadcrumb-item { display: inline; }'));
    assert.ok((rules)?.includes('.ot-breadcrumb-separator { white-space: pre-wrap; }'));
    // The current page has no rule of its own: the design does not distinguish it.
    assert.doesNotMatch(css, /aria-current|ot-breadcrumb-current/);
  });
  it('adds no appearance the design does not show: no hover, visited, pressed, underline, motion or shadow', () => {
    assert.doesNotMatch(css, /:hover|:visited|:active|:link\b|underline|transition|animation|box-shadow|text-shadow|opacity|cursor/);
    assert.deepEqual(visual.filter(rule => /(?<![\w-])color:/.test(rule)).map(rule => rule.split(' {')[0]),
      ['.ot-link', '.ot-breadcrumb', '.ot-breadcrumb-link']);
    assert.doesNotMatch(css, /background|border-radius/);
  });
  it('uses the approved focus outline for both anchors, with Highlight in forced colours', () => {
    assert.ok((rules)?.includes('.ot-link:focus-visible, .ot-breadcrumb-link:focus-visible ' +
      '{ outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }'));
    assert.ok((tokenCss)?.includes('outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }'));
    assert.ok((css)?.includes('@media (forced-colors: active) {\n' +
      '  .ot-link:focus-visible, .ot-breadcrumb-link:focus-visible { outline-color: Highlight; }\n}'));
    // An outline never takes layout space, so focus cannot shift neighbouring content.
    assert.doesNotMatch(css, /:focus(?!-visible)|:focus-visible[^{]*\{[^}]*(?:border|padding|margin)/);
  });
  it('takes colour and type from recorded tokens with the focus outline as the only visible literal length', () => {
    const text = visual.join('\n');
    assert.equal(css.match(/#[0-9a-f]{3,8}\b/gi), null);
    assert.deepEqual(text.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em)\b/g), ['2px', '2px']);
    const names = [...css.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)].map(match => match[1]);
    assert.ok(names.length >= 14);
    for (const name of names) assert.ok((tokenCss)?.includes(`  ${name}: `));
    assert.deepEqual(css.match(/font-family: [^;]+;/g), ['font-family: Geist, sans-serif;']);
  });
  it('declares nothing on visible text that could clip it or stop it wrapping', () => {
    const text = visual.join('\n');
    assert.ok((rules.find(rule => rule.startsWith('.ot-link {')))?.includes('max-inline-size: 100%; '));
    assert.equal((text.match(/overflow-wrap: anywhere;/g))?.length, 2);
    assert.doesNotMatch(text, /overflow(?:-[xy]|-inline|-block)?:|text-overflow|nowrap|line-clamp|(?<![\w-])(?:block-size|height|width)\s*:/);
    // Preserved separator spaces still wrap; `pre` or `nowrap` would force one line at 320px.
    assert.deepEqual(text.match(/white-space: [a-z-]+;/g), ['white-space: pre-wrap;']);
  });
  it('hides the live region with the clip pattern while keeping it in the accessibility tree', () => {
    assert.ok((rules)?.includes('.ot-live-region { position: absolute; inline-size: 1px; block-size: 1px; margin: -1px; ' +
      'padding: 0; border: 0; overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }'));
    assert.equal((rules.filter(rule => rule.startsWith('.ot-live-region')))?.length, 1);
    assert.doesNotMatch(css, /display:\s*none|visibility:\s*hidden|\[hidden\]/);
  });
  function luminance(hex: string) {
    const channels = hex.slice(1).match(/../g)!.map(channel => parseInt(channel, 16) / 255)
      .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  it('measures normal-text contrast for link and breadcrumb text on every surface the design places them on', () => {
    const surfaces = ['surface-base', 'surface-canvas', 'surface-nav', 'action-subtle'] as const;
    for (const text of ['text-link', 'text-secondary'] as const) for (const surface of surfaces) {
      const values = [luminance(tokens.colors[text]), luminance(tokens.colors[surface])].sort((a, b) => b - a);
      assert.ok((values[0] + 0.05) / (values[1] + 0.05) >= 4.5);
    }
  });
});
