import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { Metric, NavItem, TableCell, metricTones, tableCellTones } from './display';
import type { MetricProps, MetricTone, NavItemProps, TableCellProps, TableCellTone } from './display';
import { displayFixtures as fixtures } from './display.fixtures';
import tokens from '../../../styles/product-tokens.json';

type Colour = keyof typeof tokens.colors;
const tokenCss = readFileSync(new URL('../../../styles/product-tokens.css', import.meta.url), 'utf8');
const displayCss = readFileSync(new URL('./display.css', import.meta.url), 'utf8');
const rules = displayCss.split('\n').map(line => line.trim()).filter(line => line.startsWith('.ot-'));
const row = (...cells: React.ReactElement[]) => renderToStaticMarkup(<table><tbody><tr>{cells}</tr></tbody></table>)
  .replace(/^<table><tbody><tr>|<\/tr><\/tbody><\/table>$/g, '');
const typeRule = (selector: string, style: string, before = '') => `${selector} { ${before}font-size: var(--ot-type-${style}-size); ` +
  `font-weight: var(--ot-type-${style}-weight); line-height: var(--ot-type-${style}-line); }`;
// Values a caller could pass when a measurement is absent; none may reach the page as text.
const absent = [0, 1024, NaN, null, undefined, false, '', '   ', '\n'] as unknown as string[];

describe('metric presentation contract', () => {
  it.each(fixtures.metrics)('renders the caller $tone title, value and detail as one description group', ({ tone, title, value, detail }) => {
    const html = renderToStaticMarkup(<Metric tone={tone} title={title} value={value} detail={detail} />);
    // Exact markup: native term and descriptions, no ARIA, no heading, no control and no text of its own.
    expect(html).toBe(`<dl class="ot-metric ot-metric--${tone}"><dt class="ot-metric-title">${title}</dt>` +
      `<dd class="ot-metric-value">${value}</dd><dd class="ot-metric-detail">${detail}</dd></dl>`);
  });
  it('declares each design tone once and carries a generated example for every one', () => {
    expect(metricTones).toEqual(['blue', 'mint', 'lilac', 'amber']);
    expect(fixtures.metrics.map(metric => metric.tone)).toEqual(metricTones);
  });
  it('states an unknown value in words with its reason', () => {
    const unknown = fixtures.metrics[3];
    const html = renderToStaticMarkup(<Metric {...unknown} />);
    expect(html).toContain('<dd class="ot-metric-value">Unknown</dd>');
    expect(html).toContain(`<dd class="ot-metric-detail">${unknown.detail}</dd>`);
    expect(html).not.toMatch(/>\s*0\s*</);
  });
  it.each(absent)('refuses the absent or unformatted text %j instead of rendering zero or a blank', text => {
    const example = fixtures.metrics[0];
    for (const field of ['title', 'value', 'detail'] as const) {
      expect(() => renderToStaticMarkup(<Metric {...example} {...{ [field]: text }} />)).toThrow(`visible foundation component metric ${field}`);
    }
  });
  it('renders the same card for different tones apart from the tone class', () => {
    const [first, ...others] = metricTones.map(tone => renderToStaticMarkup(<Metric {...fixtures.metrics[0]} tone={tone} />));
    for (const [index, html] of others.entries()) {
      expect(html.replace(`ot-metric--${metricTones[index + 1]}`, 'ot-metric--blue')).toBe(first);
    }
  });
  it.each(['default', 'coral', 'success', '', undefined])('rejects the undeclared tone %j rather than rendering an undesigned card', tone => {
    expect(() => renderToStaticMarkup(<Metric {...fixtures.metrics[0]} tone={tone as MetricTone} />)).toThrow('declared metric tone');
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Metric tone="blue" title={'<b>generated</b>'} value={'<script>generated</script>'} detail={'<a href="#">generated</a>'} />);
    expect(html).toContain('&lt;b&gt;generated&lt;/b&gt;');
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(html.match(/<[a-z]+/g)).toEqual(['<dl', '<dt', '<dd', '<dd']);
  });
});

describe('table cell presentation contract', () => {
  const frame = (tone: TableCellTone, value: string) =>
    `<div class="ot-table-cell-frame ot-table-cell-frame--${tone}"><span class="ot-table-cell-value">${value}</span></div>`;
  it.each(tableCellTones)('renders a native data cell around the %s frame', tone => {
    expect(row(<TableCell key="cell" tone={tone} value="Generated value" />)).toBe(`<td class="ot-table-cell">${frame(tone, 'Generated value')}</td>`);
  });
  it('uses the default frame when no tone is given', () => {
    expect(row(<TableCell key="cell" value="Generated value" />)).toBe(`<td class="ot-table-cell">${frame('default', 'Generated value')}</td>`);
  });
  it.each([['column', 'col'], ['row', 'row']] as const)('renders a %s header as a scoped header cell with the same frame', (header, scope) => {
    expect(row(<TableCell key="cell" header={header} tone="blue" value="Generated header" />))
      .toBe(`<th scope="${scope}" class="ot-table-cell">${frame('blue', 'Generated header')}</th>`);
  });
  it('declares each design tone once and shows every one in the generated table', () => {
    expect(tableCellTones).toEqual(['default', 'blue', 'mint', 'lilac', 'amber', 'coral']);
    const used = new Set([...fixtures.tableColumns, ...fixtures.tableRows.flat()].map(cell => cell.tone ?? 'default'));
    expect([...used].sort()).toEqual([...tableCellTones].sort());
  });
  it('composes a native table whose every cell is a child of a row and is associated with its headers', () => {
    const html = renderToStaticMarkup(<table>
      <caption>{fixtures.tableCaption}</caption>
      <thead><tr>{fixtures.tableColumns.map(cell => <TableCell key={cell.value} {...cell} />)}</tr></thead>
      <tbody>{fixtures.tableRows.map(cells => <tr key={cells[0].value}>{cells.map(cell => <TableCell key={cell.value} {...cell} />)}</tr>)}</tbody>
    </table>);
    expect(html.match(/<th scope="col" class="ot-table-cell">/g)).toHaveLength(3);
    expect(html.match(/<th scope="row" class="ot-table-cell">/g)).toHaveLength(fixtures.tableRows.length);
    expect(html.match(/<td class="ot-table-cell">/g)).toHaveLength(fixtures.tableRows.length * 2);
    // Every row holds exactly three cell elements and nothing else; no role replaces the native ones.
    const rows = [...html.matchAll(/<tr>(.*?)<\/tr>/g)].map(match => match[1]);
    expect(rows).toHaveLength(fixtures.tableRows.length + 1);
    for (const cells of rows) expect(cells).toMatch(/^(?:<(td|th)[ >].*?<\/\1>){3}$/);
    expect(html).not.toMatch(/role=|aria-|tabindex|<td[^>]*scope|<th(?=[ >])(?![^>]*scope)/);
  });
  it('keeps an unknown value and a counted zero as different visible text', () => {
    const [unknown, zero] = [fixtures.tableRows[2][1], fixtures.tableRows[4][1]];
    expect(row(<TableCell key="cell" {...unknown} />)).toContain('>Unknown</span>');
    expect(row(<TableCell key="cell" {...zero} />)).toContain('>0 of 16 generated examples</span>');
  });
  it.each(absent)('refuses the absent or unformatted value %j instead of rendering zero or an empty cell', value => {
    expect(() => row(<TableCell key="cell" value={value} />)).toThrow('visible foundation component table cell value');
  });
  it.each(['success', 'Blue', '', null])('rejects the undeclared tone %j rather than rendering an undesigned cell', tone => {
    expect(() => row(<TableCell key="cell" tone={tone as TableCellTone} value="Generated value" />)).toThrow('declared table cell tone');
  });
  it.each(['col', 'true', '', true, null])('rejects the header %j rather than guessing a scope', header => {
    expect(() => row(<TableCell key="cell" header={header as TableCellProps['header']} value="Generated value" />)).toThrow('`column`, `row` or omitted');
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = row(<TableCell key="cell" value={'<script>generated</script>'} />);
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(html.match(/<[a-z]+/g)).toEqual(['<td', '<div', '<span']);
  });
});

describe('navigation item presentation contract', () => {
  it('renders a plain link to the caller destination with the caller label', () => {
    expect(renderToStaticMarkup(<NavItem label="Generated overview" href="#generated-overview" />))
      .toBe('<a class="ot-nav-item" href="#generated-overview">Generated overview</a>');
  });
  it('marks only the current destination, with aria-current page', () => {
    expect(renderToStaticMarkup(<NavItem label="Generated records" href="#generated-records" current />))
      .toBe('<a class="ot-nav-item" href="#generated-records" aria-current="page">Generated records</a>');
    for (const current of [false, undefined, 'page', 'false', 1, null]) {
      expect(renderToStaticMarkup(<NavItem label="Generated records" href="#generated-records" current={current as NavItemProps['current']} />))
        .not.toContain('aria-current');
    }
  });
  it('composes a labelled navigation landmark with one current link and no label of its own', () => {
    const html = renderToStaticMarkup(<nav aria-label={fixtures.navigationLabel}>
      <ul>{fixtures.navigation.map(item => <li key={item.href}><NavItem {...item} /></li>)}</ul>
    </nav>);
    expect(html.match(/<a class="ot-nav-item" href="#generated-[a-z]+"/g)).toHaveLength(fixtures.navigation.length);
    expect(html.match(/aria-current="page"/g)).toHaveLength(1);
    expect(html.replace(/<[^>]+>/g, '')).toBe(fixtures.navigation.map(item => item.label).join(''));
    expect(html).not.toMatch(/role=|tabindex|<button|onclick/);
  });
  it.each(absent)('refuses the absent label or destination %j', text => {
    expect(() => renderToStaticMarkup(<NavItem label={text} href="#generated" />)).toThrow('visible foundation component navigation label');
    expect(() => renderToStaticMarkup(<NavItem label="Generated label" href={text} />)).toThrow('navigation item destination');
  });
  it('escapes caller text and never emits a caller script destination', () => {
    const html = renderToStaticMarkup(<NavItem label={'<script>generated</script>'} href={'javascript:generated()'} />);
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(html).not.toContain('generated()');
    expect(html.match(/<[a-z]+/g)).toEqual(['<a']);
  });
});

describe('display design token binding', () => {
  const metricFill: Record<MetricTone, Colour> = { blue: 'action-subtle', mint: 'success-background', lilac: 'learning-background', amber: 'hold-background' };
  const cellFill: Record<TableCellTone, Colour> = { default: 'surface-base', ...metricFill, coral: 'failure-background' };
  it('binds every metric and table cell tone to the fill variable of its design variant', () => {
    for (const tone of metricTones) expect(rules).toContain(`.ot-metric--${tone} { background: var(--ot-${metricFill[tone]}); }`);
    for (const tone of tableCellTones) expect(rules).toContain(`.ot-table-cell-frame--${tone} { background: var(--ot-${cellFill[tone]}); }`);
    // A tone changes the fill and nothing else.
    expect(rules.filter(rule => /^\.ot-(metric|table-cell-frame)--/.test(rule))).toHaveLength(metricTones.length + tableCellTones.length);
  });
  it('selects the active navigation appearance from aria-current, never from a separate class', () => {
    expect(rules).toContain(`.ot-nav-item[aria-current='page'] { background: var(--ot-action-subtle); color: var(--ot-text-link); }`);
    expect(rules.find(rule => rule.startsWith('.ot-nav-item {'))).toContain(' background: var(--ot-surface-nav); color: var(--ot-text-secondary); ');
    expect(displayCss).not.toMatch(/ot-nav-item--|:hover|:active|:visited/);
  });
  it('takes each text layer from its recorded type style and text colour', () => {
    expect(rules).toContain(typeRule('.ot-metric-title', 'small-strong'));
    expect(rules).toContain(typeRule('.ot-metric-value', 'metric'));
    expect(rules).toContain(typeRule('.ot-metric-detail', 'small', 'color: var(--ot-text-secondary); '));
    expect(rules).toContain(typeRule('.ot-table-cell-value', 'body', 'flex: 1 1 0; min-inline-size: 0; '));
    expect(rules.find(rule => rule.startsWith('.ot-nav-item {'))).toContain(typeRule('', 'label').slice(3, -2));
    expect(rules.find(rule => rule.startsWith('.ot-metric {'))).toContain(' color: var(--ot-text-primary); ');
    expect(rules.find(rule => rule.startsWith('.ot-table-cell-frame {'))).toContain(' color: var(--ot-text-primary); ');
    expect(rules[0]).toBe('.ot-metric, .ot-table-cell, .ot-nav-item { font-family: Geist, sans-serif; overflow-wrap: anywhere; }');
  });
  it('takes spacing and radius from recorded tokens', () => {
    expect(rules.find(rule => rule.startsWith('.ot-metric {'))).toContain(
      ' gap: var(--ot-space-8); box-sizing: border-box; inline-size: 100%; min-inline-size: 0; margin: 0; padding: var(--ot-space-16) var(--ot-space-20); border-radius: var(--ot-radius-12); ');
    expect(rules).toContain('.ot-table-cell { padding: 0; vertical-align: top; text-align: start; }');
    expect(rules.find(rule => rule.startsWith('.ot-table-cell-frame {'))).toContain(' padding: var(--ot-space-12); ');
    expect(rules.find(rule => rule.startsWith('.ot-nav-item {'))).toContain(' padding: var(--ot-space-8) var(--ot-space-12); border-radius: var(--ot-radius-6); ');
    // The table cell has square corners in the design; no radius is declared for it.
    expect(rules.filter(rule => rule.includes('border-radius'))).toHaveLength(2);
  });
  it('uses only defined variables, no raw colour, and literal lengths only for focus and forced colours', () => {
    expect(displayCss.match(/#[0-9a-f]{3,8}\b/gi)).toBeNull();
    expect(displayCss).not.toMatch(/\b(?:rgb|hsl|oklch|color)a?\(/);
    const used = [...displayCss.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)].map(match => match[1]);
    expect(used.length).toBeGreaterThan(30);
    for (const name of used) expect(tokenCss).toContain(`  ${name}: `);
    expect(displayCss.match(/var\(/g)).toHaveLength(used.length);
    expect(displayCss.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em|%)(?![\w-])/g)).toEqual(['100%', '100%', '2px', '2px', '1px', '3px']);
  });
  it('keeps the approved keyboard focus treatment of the shipped button, in both colour modes', () => {
    const shipped = tokenCss.split('\n').map(line => line.trim());
    const focus = shipped.find(line => line.startsWith('.ot-button:focus-visible,'))!;
    const forced = shipped.find(line => line.startsWith('.ot-button:focus-visible,') && line.includes('Highlight'))!;
    expect(rules).toContain(`.ot-nav-item:focus-visible ${focus.slice(focus.indexOf('{'))}`);
    expect(rules).toContain(`.ot-nav-item:focus-visible ${forced.slice(forced.indexOf('{'))}`);
    expect(rules.filter(rule => rule.includes('outline'))).toHaveLength(2);
  });
  it('keeps a card edge and the current link visible when forced colours remove the fills', () => {
    const forced = displayCss.slice(displayCss.indexOf('@media (forced-colors: active)'));
    expect(forced).toContain('.ot-metric { border: 1px solid CanvasText; }');
    expect(forced).toContain(`.ot-nav-item[aria-current='page'] { border-inline-start: 3px solid Highlight; }`);
    // Outside that block nothing draws a border, so the designed sizes are untouched.
    expect(displayCss.slice(0, displayCss.indexOf('@media'))).not.toMatch(/border(?!-radius|-box)/);
    expect(displayCss.match(/@media/g)).toHaveLength(1);
  });
  it('declares nothing that could clip text, stop it wrapping, move or cast a shadow', () => {
    expect(displayCss).not.toMatch(/overflow(?:-[xy]|-inline|-block)?:|text-overflow|white-space|line-clamp|(?<!-)block-size|(?<![\w-])height|(?<![\w-])width/);
    expect(displayCss).not.toMatch(/transition|animation|transform|shadow|filter|opacity|cursor/);
  });
  function luminance(hex: string) {
    const channels = hex.slice(1).match(/../g)!.map(channel => parseInt(channel, 16) / 255)
      .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  it('measures normal-text contrast for every text and fill pair of the three components', () => {
    const pairs: [Colour, Colour][] = [
      ...metricTones.flatMap(tone => [['text-primary', metricFill[tone]], ['text-secondary', metricFill[tone]]] as [Colour, Colour][]),
      ...tableCellTones.map(tone => ['text-primary', cellFill[tone]] as [Colour, Colour]),
      ['text-secondary', 'surface-nav'], ['text-link', 'action-subtle'],
    ];
    expect(pairs).toHaveLength(16);
    for (const [foreground, background] of pairs) {
      const values = [luminance(tokens.colors[foreground]), luminance(tokens.colors[background])].sort((a, b) => b - a);
      expect((values[0] + 0.05) / (values[1] + 0.05)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe('display fixtures', () => {
  it('are accepted unchanged by the components, including the long wrapping examples', () => {
    const metrics: readonly MetricProps[] = [...fixtures.metrics, fixtures.metricLong];
    const cells: readonly TableCellProps[] = [...fixtures.tableColumns, ...fixtures.tableRows.flat(), { value: fixtures.tableCellLong }];
    const links: readonly NavItemProps[] = [...fixtures.navigation, { label: fixtures.navItemLong, href: '#generated-long' }];
    for (const metric of metrics) expect(renderToStaticMarkup(<Metric {...metric} />)).toContain(`>${metric.value}</dd>`);
    for (const cell of cells) expect(row(<TableCell key="cell" {...cell} />)).toContain(`>${cell.value}</span>`);
    for (const link of links) expect(renderToStaticMarkup(<NavItem {...link} />)).toContain(`>${link.label}</a>`);
    expect(fixtures.tableRows.every(cells => cells.length === fixtures.tableColumns.length && cells[0].header === 'row')).toBe(true);
    expect(fixtures.navigation.filter(link => link.current)).toHaveLength(1);
  });
});
