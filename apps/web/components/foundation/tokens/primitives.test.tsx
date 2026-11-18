import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { Breadcrumb, breadcrumbSeparator, Link, linkSizes, LiveRegion, liveRegionPolitenessLevels } from './primitives';
import type { BreadcrumbItem, LinkSize, LiveRegionPoliteness } from './primitives';
import { primitiveFixtures as fixtures } from './primitives.fixtures';
import tokens from '../../../styles/product-tokens.json';

const tags = (html: string) => html.match(/<[a-z]+/g);
const visibleText = (html: string) => html.replace(/<[^>]+>/g, '');

describe('link presentation contract', () => {
  it.each(fixtures.links)('renders the $size link as one native anchor to its destination', ({ size, label, href }) => {
    const html = renderToStaticMarkup(<Link size={size} label={label} href={href} />);
    // Exact markup: no button role, no new-window target and no arrow unless it is requested.
    expect(html).toBe(`<a href="${href}" class="ot-link ot-link--${size}">${label}</a>`);
  });
  it('declares each designed text style once and carries a generated example for every one', () => {
    expect(linkSizes).toEqual(['small', 'small-strong', 'label']);
    expect(fixtures.links.map(link => link.size)).toEqual(linkSizes);
  });
  it('draws the trailing arrow as decoration that stays out of the accessible name', () => {
    const html = renderToStaticMarkup(<Link size="small-strong" label="Open generated example" href="/generated" trailingArrow />);
    expect(html).toBe('<a href="/generated" class="ot-link ot-link--small-strong">Open generated example' +
      '<span aria-hidden="true"> →</span></a>');
    expect(visibleText(html)).toBe('Open generated example →');
    expect(renderToStaticMarkup(<Link size="small-strong" label="Open generated example" href="/generated"
      trailingArrow={false} />)).not.toContain('→');
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Link size="label" label={'<script>generated</script>'} href={'/generated?a="b"&c=<d>'} />);
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(html).toContain('href="/generated?a=&quot;b&quot;&amp;c=&lt;d&gt;"');
    expect(tags(html)).toEqual(['<a']);
  });
  it.each(['', '   ', '\n'])('rejects a missing visible label or destination %j', text => {
    expect(() => renderToStaticMarkup(<Link size="small" label={text} href="/generated" />)).toThrow('visible');
    expect(() => renderToStaticMarkup(<Link size="small" label="Generated" href={text} />)).toThrow('destination is required');
  });
  it.each(['medium', 'body', '', undefined])('rejects the undeclared size %j rather than rendering an undesigned style', size => {
    expect(() => renderToStaticMarkup(<Link size={size as LinkSize} label="Generated" href="/generated" />))
      .toThrow('declared link size');
  });
  it.each(fixtures.unsafeDestinations)('refuses the executable or unknown-scheme destination %j', href => {
    expect(() => renderToStaticMarkup(<Link size="small" label="Generated" href={href} />))
      .toThrow('must be a path or an http, https or mailto address');
  });
  it.each(fixtures.safeDestinations)('keeps the relative or web destination %j unchanged', href => {
    expect(renderToStaticMarkup(<Link size="small" label="Generated" href={href} />))
      .toBe(`<a href="${href}" class="ot-link ot-link--small">Generated</a>`);
  });
  it('never calls the press observer while rendering', () => {
    expect(renderToStaticMarkup(<Link size="small" label="Generated" href="/generated"
      onPress={() => { throw new Error('Rendering cannot follow a link'); }} />)).toContain('href="/generated"');
  });
});

describe('breadcrumb presentation contract', () => {
  it('renders a named navigation landmark with an ordered trail and one current page', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={fixtures.breadcrumb} />);
    expect(html).toBe('<nav class="ot-breadcrumb" aria-label="Breadcrumb"><ol class="ot-breadcrumb-list" role="list">' +
      '<li class="ot-breadcrumb-item"><a class="ot-breadcrumb-link" href="/generated">Generated tenant A</a></li>' +
      '<li class="ot-breadcrumb-item"><span class="ot-breadcrumb-separator" aria-hidden="true">  /  </span>' +
      '<span aria-current="page">Generated workflow</span></li></ol></nav>');
  });
  it('reads as the designed single line: labels joined by a solidus with two spaces on each side', () => {
    expect(breadcrumbSeparator).toBe(`${' '.repeat(2)}/${' '.repeat(2)}`);
    for (const items of [fixtures.breadcrumb, fixtures.breadcrumbDeep, fixtures.breadcrumbLong]) {
      const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={items} />);
      expect(visibleText(html)).toBe(items.map(item => item.label).join(`${' '.repeat(2)}/${' '.repeat(2)}`));
      expect(html.match(/class="ot-breadcrumb-separator" aria-hidden="true"/g)).toHaveLength(items.length - 1);
      expect(html.match(/<li /g)).toHaveLength(items.length);
    }
  });
  it('marks only the last item current, links only ancestors with a destination and leaves the rest as text', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={fixtures.breadcrumbDeep} />);
    expect(html.match(/aria-current="page"/g)).toHaveLength(1);
    expect(html).toContain('<span aria-current="page">Generated details</span></li></ol></nav>');
    expect(html.match(/<a /g)).toHaveLength(2);
    expect(html).toContain('<span>Generated group</span>');
    expect(html).not.toContain('role="button"');
    expect(html).not.toContain('<button');
  });
  it('renders a single current page without a separator or a link', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={[{ label: 'Generated workflow' }]} />);
    expect(tags(html)).toEqual(['<nav', '<ol', '<li', '<span']);
    expect(visibleText(html)).toBe('Generated workflow');
  });
  it('escapes caller text instead of introducing markup', () => {
    const html = renderToStaticMarkup(<Breadcrumb label={'<b>generated</b>'}
      items={[{ label: '<i>generated</i>', href: '/generated' }, { label: '<script>generated</script>' }]} />);
    expect(html).toContain('aria-label="&lt;b&gt;generated&lt;/b&gt;"');
    expect(html).toContain('&lt;i&gt;generated&lt;/i&gt;');
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(tags(html)).toEqual(['<nav', '<ol', '<li', '<a', '<li', '<span', '<span']);
  });
  it.each(['', '   ', '\n'])('rejects a missing landmark name or item label %j', text => {
    expect(() => renderToStaticMarkup(<Breadcrumb label={text} items={fixtures.breadcrumb} />)).toThrow('visible');
    expect(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: text, href: '/generated' }, { label: 'Generated workflow' }]} />)).toThrow('visible');
    expect(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: 'Generated tenant A', href: '/generated' }, { label: text }]} />)).toThrow('visible');
  });
  it('rejects an empty or absent trail', () => {
    for (const items of [[], undefined, null]) {
      expect(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
        items={items as unknown as BreadcrumbItem[]} />)).toThrow('at least one item');
    }
  });
  it('rejects a destination on the current page rather than silently dropping it', () => {
    expect(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: 'Generated tenant A', href: '/generated' }, { label: 'Generated workflow', href: '/generated/workflow' }]} />))
      .toThrow('current breadcrumb item is not a link');
  });
  it.each(['', ' ', ...fixtures.unsafeDestinations])('refuses the ancestor destination %j', href => {
    expect(() => renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel}
      items={[{ label: 'Generated tenant A', href }, { label: 'Generated workflow' }]} />)).toThrow('destination');
  });
  it('never calls the press observer while rendering', () => {
    expect(renderToStaticMarkup(<Breadcrumb label={fixtures.breadcrumbLabel} items={fixtures.breadcrumb}
      onItemPress={() => { throw new Error('Rendering cannot follow a link'); }} />)).toContain('href="/generated"');
  });
});

describe('live region contract', () => {
  it('announces politely by default through the status role alone', () => {
    const html = renderToStaticMarkup(<LiveRegion message={fixtures.announcements[0].message} />);
    expect(html).toBe('<div role="status" class="ot-live-region">Generated status changed.</div>');
  });
  it('uses the alert role only when assertive is requested', () => {
    const html = renderToStaticMarkup(<LiveRegion {...fixtures.announcements[1]} />);
    expect(html).toBe('<div role="alert" class="ot-live-region">Generated status needs attention.</div>');
    expect(liveRegionPolitenessLevels).toEqual(['polite', 'assertive']);
  });
  it.each(liveRegionPolitenessLevels)('does not repeat the %s role as live attributes that some readers speak twice', politeness => {
    const html = renderToStaticMarkup(<LiveRegion message="Generated" politeness={politeness} />);
    expect(html).not.toMatch(/aria-live|aria-atomic|aria-relevant|aria-busy/);
    expect(html.match(/role=/g)).toHaveLength(1);
  });
  it('stays mounted and silent for an empty message so later text is a change', () => {
    expect(renderToStaticMarkup(<LiveRegion message="" />)).toBe('<div role="status" class="ot-live-region"></div>');
  });
  it('renders identical text identically, leaving nothing for a re-render to change or re-announce', () => {
    const render = () => renderToStaticMarkup(<LiveRegion message="Generated status changed." />);
    const first = render();
    for (let poll = 0; poll < 5; poll += 1) expect(render()).toBe(first);
    // No per-render nonce, counter, timestamp or padding character is added to force a repeat.
    expect(first).toBe('<div role="status" class="ot-live-region">Generated status changed.</div>');
    expect(tags(first)).toEqual(['<div']);
  });
  it('escapes caller text instead of introducing markup', () => {
    const html = renderToStaticMarkup(<LiveRegion message={'<script>generated</script>'} />);
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(tags(html)).toEqual(['<div']);
  });
  it.each([undefined, null, 0, { text: 'Generated' }])('rejects the non-text message %j', message => {
    expect(() => renderToStaticMarkup(<LiveRegion message={message as unknown as string} />)).toThrow('must be text');
  });
  it.each(['off', 'rude', '', null])('rejects the undeclared politeness %j', politeness => {
    expect(() => renderToStaticMarkup(<LiveRegion message="Generated" politeness={politeness as LiveRegionPoliteness} />))
      .toThrow('declared live region politeness');
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
    for (const size of linkSizes) expect(rules).toContain(`.ot-link--${size} { ${type(size)} }`);
    expect(rules.filter(rule => rule.startsWith('.ot-link--'))).toHaveLength(linkSizes.length);
    const link = rules.find(rule => rule.startsWith('.ot-link {'))!;
    expect(link).toContain('color: var(--ot-text-link); ');
    expect(link).toContain('text-decoration: none; ');
  });
  it('draws the breadcrumb as one secondary small trail whose links keep the trail colour', () => {
    const trail = rules.find(rule => rule.startsWith('.ot-breadcrumb {'))!;
    expect(trail).toContain('color: var(--ot-text-secondary); ');
    expect(trail).toContain(`${type('small')} `);
    expect(rules).toContain('.ot-breadcrumb-link { color: inherit; text-decoration: none; }');
    expect(rules).toContain('.ot-breadcrumb-item { display: inline; }');
    expect(rules).toContain('.ot-breadcrumb-separator { white-space: pre-wrap; }');
    // The current page has no rule of its own: the design does not distinguish it.
    expect(css).not.toMatch(/aria-current|ot-breadcrumb-current/);
  });
  it('adds no appearance the design does not show: no hover, visited, pressed, underline, motion or shadow', () => {
    expect(css).not.toMatch(/:hover|:visited|:active|:link\b|underline|transition|animation|box-shadow|text-shadow|opacity|cursor/);
    expect(visual.filter(rule => /(?<![\w-])color:/.test(rule)).map(rule => rule.split(' {')[0]))
      .toEqual(['.ot-link', '.ot-breadcrumb', '.ot-breadcrumb-link']);
    expect(css).not.toMatch(/background|border-radius/);
  });
  it('uses the approved focus outline for both anchors, with Highlight in forced colours', () => {
    expect(rules).toContain('.ot-link:focus-visible, .ot-breadcrumb-link:focus-visible ' +
      '{ outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }');
    expect(tokenCss).toContain('outline: 2px solid var(--ot-action-primary); outline-offset: 2px; }');
    expect(css).toContain('@media (forced-colors: active) {\n' +
      '  .ot-link:focus-visible, .ot-breadcrumb-link:focus-visible { outline-color: Highlight; }\n}');
    // An outline never takes layout space, so focus cannot shift neighbouring content.
    expect(css).not.toMatch(/:focus(?!-visible)|:focus-visible[^{]*\{[^}]*(?:border|padding|margin)/);
  });
  it('takes colour and type from recorded tokens with the focus outline as the only visible literal length', () => {
    const text = visual.join('\n');
    expect(css.match(/#[0-9a-f]{3,8}\b/gi)).toBeNull();
    expect(text.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em)\b/g)).toEqual(['2px', '2px']);
    const names = [...css.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)].map(match => match[1]);
    expect(names.length).toBeGreaterThanOrEqual(14);
    for (const name of names) expect(tokenCss).toContain(`  ${name}: `);
    expect(css.match(/font-family: [^;]+;/g)).toEqual(['font-family: Geist, sans-serif;']);
  });
  it('declares nothing on visible text that could clip it or stop it wrapping', () => {
    const text = visual.join('\n');
    expect(rules.find(rule => rule.startsWith('.ot-link {'))).toContain('max-inline-size: 100%; ');
    expect(text.match(/overflow-wrap: anywhere;/g)).toHaveLength(2);
    expect(text).not.toMatch(/overflow(?:-[xy]|-inline|-block)?:|text-overflow|nowrap|line-clamp|(?<![\w-])(?:block-size|height|width)\s*:/);
    // Preserved separator spaces still wrap; `pre` or `nowrap` would force one line at 320px.
    expect(text.match(/white-space: [a-z-]+;/g)).toEqual(['white-space: pre-wrap;']);
  });
  it('hides the live region with the clip pattern while keeping it in the accessibility tree', () => {
    expect(rules).toContain('.ot-live-region { position: absolute; inline-size: 1px; block-size: 1px; margin: -1px; ' +
      'padding: 0; border: 0; overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }');
    expect(rules.filter(rule => rule.startsWith('.ot-live-region'))).toHaveLength(1);
    expect(css).not.toMatch(/display:\s*none|visibility:\s*hidden|\[hidden\]/);
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
      expect((values[0] + 0.05) / (values[1] + 0.05)).toBeGreaterThanOrEqual(4.5);
    }
  });
});
