import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
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
  testCases(fixtures.metrics)('renders the caller $tone title, value and detail as one description group', ({ tone, title, value, detail }) => {
    const html = renderToStaticMarkup(<Metric tone={tone} title={title} value={value} detail={detail} />);
    // Exact markup: native term and descriptions, no ARIA, no heading, no control and no text of its own.
    assert.equal(html, `<dl class="ot-metric ot-metric--${tone}"><dt class="ot-metric-title">${title}</dt>` +
      `<dd class="ot-metric-value">${value}</dd><dd class="ot-metric-detail">${detail}</dd></dl>`);
  });
  it('declares each design tone once and carries a generated example for every one', () => {
    assert.deepEqual(metricTones, ['blue', 'mint', 'lilac', 'amber']);
    assert.deepEqual(fixtures.metrics.map(metric => metric.tone), metricTones);
  });
  it('states an unknown value in words with its reason', () => {
    const unknown = fixtures.metrics[3];
    const html = renderToStaticMarkup(<Metric {...unknown} />);
    assert.ok((html)?.includes('<dd class="ot-metric-value">Unknown</dd>'));
    assert.ok((html)?.includes(`<dd class="ot-metric-detail">${unknown.detail}</dd>`));
    assert.doesNotMatch(html, />\s*0\s*</);
  });
  testCases(absent)('refuses the absent or unformatted text %j instead of rendering zero or a blank', text => {
    const example = fixtures.metrics[0];
    for (const field of ['title', 'value', 'detail'] as const) {
      assert.throws(() => renderToStaticMarkup(<Metric {...example} {...{ [field]: text }} />), `visible foundation component metric ${field}`);
    }
  });
  it('renders the same card for different tones apart from the tone class', () => {
    const [first, ...others] = metricTones.map(tone => renderToStaticMarkup(<Metric {...fixtures.metrics[0]} tone={tone} />));
    for (const [index, html] of others.entries()) {
      assert.equal(html.replace(`ot-metric--${metricTones[index + 1]}`, 'ot-metric--blue'), first);
    }
  });
  testCases(['default', 'coral', 'success', '', undefined])('rejects the undeclared tone %j rather than rendering an undesigned card', tone => {
    assert.throws(() => renderToStaticMarkup(<Metric {...fixtures.metrics[0]} tone={tone as MetricTone} />), /declared metric tone/);
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = renderToStaticMarkup(<Metric tone="blue" title={'<b>generated</b>'} value={'<script>generated</script>'} detail={'<a href="#">generated</a>'} />);
    assert.ok((html)?.includes('&lt;b&gt;generated&lt;/b&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(html.match(/<[a-z]+/g), ['<dl', '<dt', '<dd', '<dd']);
  });
});

describe('table cell presentation contract', () => {
  const frame = (tone: TableCellTone, value: string) =>
    `<div class="ot-table-cell-frame ot-table-cell-frame--${tone}"><span class="ot-table-cell-value">${value}</span></div>`;
  testCases(tableCellTones)('renders a native data cell around the %s frame', tone => {
    assert.equal(row(<TableCell key="cell" tone={tone} value="Generated value" />), `<td class="ot-table-cell">${frame(tone, 'Generated value')}</td>`);
  });
  it('uses the default frame when no tone is given', () => {
    assert.equal(row(<TableCell key="cell" value="Generated value" />), `<td class="ot-table-cell">${frame('default', 'Generated value')}</td>`);
  });
  testCases([['column', 'col'], ['row', 'row']] as const)('renders a %s header as a scoped header cell with the same frame', (header, scope) => {
    assert.equal(row(<TableCell key="cell" header={header} tone="blue" value="Generated header" />), `<th scope="${scope}" class="ot-table-cell">${frame('blue', 'Generated header')}</th>`);
  });
  it('declares each design tone once and shows every one in the generated table', () => {
    assert.deepEqual(tableCellTones, ['default', 'blue', 'mint', 'lilac', 'amber', 'coral']);
    const used = new Set([...fixtures.tableColumns, ...fixtures.tableRows.flat()].map(cell => cell.tone ?? 'default'));
    assert.deepEqual([...used].sort(), [...tableCellTones].sort());
  });
  it('composes a native table whose every cell is a child of a row and is associated with its headers', () => {
    const html = renderToStaticMarkup(<table>
      <caption>{fixtures.tableCaption}</caption>
      <thead><tr>{fixtures.tableColumns.map(cell => <TableCell key={cell.value} {...cell} />)}</tr></thead>
      <tbody>{fixtures.tableRows.map(cells => <tr key={cells[0].value}>{cells.map(cell => <TableCell key={cell.value} {...cell} />)}</tr>)}</tbody>
    </table>);
    assert.equal((html.match(/<th scope="col" class="ot-table-cell">/g))?.length, 3);
    assert.equal((html.match(/<th scope="row" class="ot-table-cell">/g))?.length, fixtures.tableRows.length);
    assert.equal((html.match(/<td class="ot-table-cell">/g))?.length, fixtures.tableRows.length * 2);
    // Every row holds exactly three cell elements and nothing else; no role replaces the native ones.
    const rows = [...html.matchAll(/<tr>(.*?)<\/tr>/g)].map(match => match[1]);
    assert.equal((rows)?.length, fixtures.tableRows.length + 1);
    for (const cells of rows) assert.match(cells, /^(?:<(td|th)[ >].*?<\/\1>){3}$/);
    assert.doesNotMatch(html, /role=|aria-|tabindex|<td[^>]*scope|<th(?=[ >])(?![^>]*scope)/);
  });
  it('keeps an unknown value and a counted zero as different visible text', () => {
    const [unknown, zero] = [fixtures.tableRows[2][1], fixtures.tableRows[4][1]];
    assert.ok((row(<TableCell key="cell" {...unknown} />))?.includes('>Unknown</span>'));
    assert.ok((row(<TableCell key="cell" {...zero} />))?.includes('>0 of 16 generated examples</span>'));
  });
  testCases(absent)('refuses the absent or unformatted value %j instead of rendering zero or an empty cell', value => {
    assert.throws(() => row(<TableCell key="cell" value={value} />), /visible foundation component table cell value/);
  });
  testCases(['success', 'Blue', '', null])('rejects the undeclared tone %j rather than rendering an undesigned cell', tone => {
    assert.throws(() => row(<TableCell key="cell" tone={tone as TableCellTone} value="Generated value" />), /declared table cell tone/);
  });
  testCases(['col', 'true', '', true, null])('rejects the header %j rather than guessing a scope', header => {
    assert.throws(() => row(<TableCell key="cell" header={header as TableCellProps['header']} value="Generated value" />), /`column`, `row` or omitted/);
  });
  it('escapes caller text instead of introducing markup or content-derived actions', () => {
    const html = row(<TableCell key="cell" value={'<script>generated</script>'} />);
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(html.match(/<[a-z]+/g), ['<td', '<div', '<span']);
  });
});

describe('navigation item presentation contract', () => {
  it('renders a plain link to the caller destination with the caller label', () => {
    assert.equal(renderToStaticMarkup(<NavItem label="Generated overview" href="#generated-overview" />), '<a class="ot-nav-item" href="#generated-overview">Generated overview</a>');
  });
  it('marks only the current destination, with aria-current page', () => {
    assert.equal(renderToStaticMarkup(<NavItem label="Generated records" href="#generated-records" current />), '<a class="ot-nav-item" href="#generated-records" aria-current="page">Generated records</a>');
    for (const current of [false, undefined, 'page', 'false', 1, null]) {
      assert.ok((renderToStaticMarkup(<NavItem label="Generated records" href="#generated-records" current={current as NavItemProps['current']} />))?.includes('aria-current') === false);
    }
  });
  it('composes a labelled navigation landmark with one current link and no label of its own', () => {
    const html = renderToStaticMarkup(<nav aria-label={fixtures.navigationLabel}>
      <ul>{fixtures.navigation.map(item => <li key={item.href}><NavItem {...item} /></li>)}</ul>
    </nav>);
    assert.equal((html.match(/<a class="ot-nav-item" href="#generated-[a-z]+"/g))?.length, fixtures.navigation.length);
    assert.equal((html.match(/aria-current="page"/g))?.length, 1);
    assert.equal(html.replace(/<[^>]+>/g, ''), fixtures.navigation.map(item => item.label).join(''));
    assert.doesNotMatch(html, /role=|tabindex|<button|onclick/);
  });
  testCases(absent)('refuses the absent label or destination %j', text => {
    assert.throws(() => renderToStaticMarkup(<NavItem label={text} href="#generated" />), /visible foundation component navigation label/);
    assert.throws(() => renderToStaticMarkup(<NavItem label="Generated label" href={text} />), /navigation item destination/);
  });
  it('escapes caller text and never emits a caller script destination', () => {
    const html = renderToStaticMarkup(<NavItem label={'<script>generated</script>'} href={'javascript:generated()'} />);
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.ok((html)?.includes('generated()') === false);
    assert.deepEqual(html.match(/<[a-z]+/g), ['<a']);
  });
});

describe('display design token binding', () => {
  const metricFill: Record<MetricTone, Colour> = { blue: 'action-subtle', mint: 'success-background', lilac: 'learning-background', amber: 'hold-background' };
  const cellFill: Record<TableCellTone, Colour> = { default: 'surface-base', ...metricFill, coral: 'failure-background' };
  it('binds every metric and table cell tone to the fill variable of its design variant', () => {
    for (const tone of metricTones) assert.ok((rules)?.includes(`.ot-metric--${tone} { background: var(--ot-${metricFill[tone]}); }`));
    for (const tone of tableCellTones) assert.ok((rules)?.includes(`.ot-table-cell-frame--${tone} { background: var(--ot-${cellFill[tone]}); }`));
    // A tone changes the fill and nothing else.
    assert.equal((rules.filter(rule => /^\.ot-(metric|table-cell-frame)--/.test(rule)))?.length, metricTones.length + tableCellTones.length);
  });
  it('selects the active navigation appearance from aria-current, never from a separate class', () => {
    assert.ok((rules)?.includes(`.ot-nav-item[aria-current='page'] { background: var(--ot-action-subtle); color: var(--ot-text-link); }`));
    assert.ok((rules.find(rule => rule.startsWith('.ot-nav-item {')))?.includes(' background: var(--ot-surface-nav); color: var(--ot-text-secondary); '));
    assert.doesNotMatch(displayCss, /ot-nav-item--|:hover|:active|:visited/);
  });
  it('takes each text layer from its recorded type style and text colour', () => {
    assert.ok((rules)?.includes(typeRule('.ot-metric-title', 'small-strong')));
    assert.ok((rules)?.includes(typeRule('.ot-metric-value', 'metric')));
    assert.ok((rules)?.includes(typeRule('.ot-metric-detail', 'small', 'color: var(--ot-text-secondary); ')));
    assert.ok((rules)?.includes(typeRule('.ot-table-cell-value', 'body', 'flex: 1 1 0; min-inline-size: 0; ')));
    assert.ok((rules.find(rule => rule.startsWith('.ot-nav-item {')))?.includes(typeRule('', 'label').slice(3, -2)));
    assert.ok((rules.find(rule => rule.startsWith('.ot-metric {')))?.includes(' color: var(--ot-text-primary); '));
    assert.ok((rules.find(rule => rule.startsWith('.ot-table-cell-frame {')))?.includes(' color: var(--ot-text-primary); '));
    assert.equal(rules[0], '.ot-metric, .ot-table-cell, .ot-nav-item { font-family: Geist, sans-serif; overflow-wrap: anywhere; }');
  });
  it('takes spacing and radius from recorded tokens', () => {
    assert.ok((rules.find(rule => rule.startsWith('.ot-metric {')))?.includes(
      ' gap: var(--ot-space-8); box-sizing: border-box; inline-size: 100%; min-inline-size: 0; margin: 0; padding: var(--ot-space-16) var(--ot-space-20); border-radius: var(--ot-radius-12); '));
    assert.ok((rules)?.includes('.ot-table-cell { padding: 0; vertical-align: top; text-align: start; }'));
    assert.ok((rules.find(rule => rule.startsWith('.ot-table-cell-frame {')))?.includes(' padding: var(--ot-space-12); '));
    assert.ok((rules.find(rule => rule.startsWith('.ot-nav-item {')))?.includes(' padding: var(--ot-space-8) var(--ot-space-12); border-radius: var(--ot-radius-6); '));
    // The table cell has square corners in the design; no radius is declared for it.
    assert.equal((rules.filter(rule => rule.includes('border-radius')))?.length, 2);
  });
  it('uses only defined variables, no raw colour, and literal lengths only for focus and forced colours', () => {
    assert.equal(displayCss.match(/#[0-9a-f]{3,8}\b/gi), null);
    assert.doesNotMatch(displayCss, /\b(?:rgb|hsl|oklch|color)a?\(/);
    const used = [...displayCss.matchAll(/var\((--ot-[a-z0-9-]+)\)/g)].map(match => match[1]);
    assert.ok(used.length > 30);
    for (const name of used) assert.ok((tokenCss)?.includes(`  ${name}: `));
    assert.equal((displayCss.match(/var\(/g))?.length, used.length);
    assert.deepEqual(displayCss.match(/(?<![\w-])\d*\.?\d+(?:px|rem|em|%)(?![\w-])/g), ['100%', '100%', '2px', '2px', '1px', '3px']);
  });
  it('keeps the approved keyboard focus treatment of the shipped button, in both colour modes', () => {
    const shipped = tokenCss.split('\n').map(line => line.trim());
    const focus = shipped.find(line => line.startsWith('.ot-button:focus-visible,'))!;
    const forced = shipped.find(line => line.startsWith('.ot-button:focus-visible,') && line.includes('Highlight'))!;
    assert.ok((rules)?.includes(`.ot-nav-item:focus-visible ${focus.slice(focus.indexOf('{'))}`));
    assert.ok((rules)?.includes(`.ot-nav-item:focus-visible ${forced.slice(forced.indexOf('{'))}`));
    assert.equal((rules.filter(rule => rule.includes('outline')))?.length, 2);
  });
  it('keeps a card edge and the current link visible when forced colours remove the fills', () => {
    const forced = displayCss.slice(displayCss.indexOf('@media (forced-colors: active)'));
    assert.ok((forced)?.includes('.ot-metric { border: 1px solid CanvasText; }'));
    assert.ok((forced)?.includes(`.ot-nav-item[aria-current='page'] { border-inline-start: 3px solid Highlight; }`));
    // Outside that block nothing draws a border, so the designed sizes are untouched.
    assert.doesNotMatch(displayCss.slice(0, displayCss.indexOf('@media')), /border(?!-radius|-box)/);
    assert.equal((displayCss.match(/@media/g))?.length, 1);
  });
  it('declares nothing that could clip text, stop it wrapping, move or cast a shadow', () => {
    assert.doesNotMatch(displayCss, /overflow(?:-[xy]|-inline|-block)?:|text-overflow|white-space|line-clamp|(?<!-)block-size|(?<![\w-])height|(?<![\w-])width/);
    assert.doesNotMatch(displayCss, /transition|animation|transform|shadow|filter|opacity|cursor/);
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
    assert.equal((pairs)?.length, 16);
    for (const [foreground, background] of pairs) {
      const values = [luminance(tokens.colors[foreground]), luminance(tokens.colors[background])].sort((a, b) => b - a);
      assert.ok((values[0] + 0.05) / (values[1] + 0.05) >= 4.5);
    }
  });
});

describe('display fixtures', () => {
  it('are accepted unchanged by the components, including the long wrapping examples', () => {
    const metrics: readonly MetricProps[] = [...fixtures.metrics, fixtures.metricLong];
    const cells: readonly TableCellProps[] = [...fixtures.tableColumns, ...fixtures.tableRows.flat(), { value: fixtures.tableCellLong }];
    const links: readonly NavItemProps[] = [...fixtures.navigation, { label: fixtures.navItemLong, href: '#generated-long' }];
    for (const metric of metrics) assert.ok((renderToStaticMarkup(<Metric {...metric} />))?.includes(`>${metric.value}</dd>`));
    for (const cell of cells) assert.ok((row(<TableCell key="cell" {...cell} />))?.includes(`>${cell.value}</span>`));
    for (const link of links) assert.ok((renderToStaticMarkup(<NavItem {...link} />))?.includes(`>${link.label}</a>`));
    assert.equal(fixtures.tableRows.every(cells => cells.length === fixtures.tableColumns.length && cells[0].header === 'row'), true);
    assert.equal((fixtures.navigation.filter(link => link.current))?.length, 1);
  });
});
