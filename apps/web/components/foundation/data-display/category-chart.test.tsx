import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { CategoryChart, type CategoryChartProps } from './category-chart';
import { categoryFixtures } from './category-chart.fixtures';

type Data = Omit<CategoryChartProps, 'expanded' | 'onExpandedChange'>;
const render = (data: Data, expanded = false) =>
  renderToStaticMarkup(<CategoryChart {...data} expanded={expanded} onExpandedChange={() => {}} />);
const { overflow, tied, incomplete } = categoryFixtures;

it('reserves the final visible slot for Other and names its supplied members', () => {
  const html = render(overflow);
  assert.equal(html.match(/<li>/g)?.length, 3);
  assert.match(html, /Other 20/);
  assert.match(html, /C 10; D 7; E 3/);
  assert.match(html, /Learning · 100 cases/);
});
it('expands to the five disjoint source categories with no summary row in the full table', () => {
  const table = render(overflow, true).split('<table')[1].split('</table>')[0];
  assert.equal(table.match(/scope="row"/g)?.length, 5);
  assert.doesNotMatch(table, /Other/);
  assert.deepEqual([...table.matchAll(/<td[^>]*><div[^>]*><span[^>]*>(\d+)</g)].map(match => Number(match[1])), [50, 30, 10, 7, 3]);
});
it('retains tied order, exact-limit and under-limit categories and known zero', () => {
  for (const visibleCategoryLimit of [3, 4]) {
    const html = render({ ...tied, visibleCategoryLimit });
    assert.ok(html.indexOf('Zulu 4') < html.indexOf('Alpha 4'));
    assert.match(html, /No matching cases 0/);
    assert.doesNotMatch(html, /Other|<button/);
    assert.equal(html.match(/class="ot-category-mark"/g)?.length, 2);
  }
});
it('keeps missing and unknown statuses visible, without plotting missing geometry', () => {
  const html = render(incomplete, true);
  assert.match(html, /Other Unknown · Aggregate unavailable/);
  assert.match(html, /Unavailable · Source unavailable/);
  assert.match(html, /Unknown · Count not supplied/);
  assert.equal(html.match(/class="ot-category-mark"/g)?.length, 1);
});
it('handles the single-slot and empty-population limits', () => {
  const html = render({ ...overflow, visibleCategoryLimit: 1, other: { status: 'observed', count: 100 } });
  assert.match(html, /Other 100/);
  assert.equal(html.match(/<li>/g)?.length, 1);
  const empty = render({ categories: [], totalCases: 0, visibleCategoryLimit: 1 });
  assert.match(empty, /No categories/);
  assert.doesNotMatch(empty, /ot-category-mark|<button|NaN|Infinity/);
});
it('refuses ambiguous limits, duplicate keys and inconsistent aggregate counts', () => {
  for (const data of [{ ...overflow, visibleCategoryLimit: 0 }, { ...overflow, totalCases: 101 },
    { ...overflow, other: { status: 'observed', count: 19 } as const }, { ...overflow, other: undefined },
    { ...tied, other: { status: 'observed', count: 0 } as const },
    { ...tied, categories: [tied.categories[0], tied.categories[0]] }]) assert.throws(() => render(data));
});
