import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { PairedOutcomes, type PairedOutcomesProps } from './paired-outcomes';
import { pairedFixtures } from './paired-outcomes.fixtures';

const render = (data: PairedOutcomesProps) => renderToStaticMarkup(<PairedOutcomes {...data} />);
const { complete, partial, mixed } = pairedFixtures;

it('renders the four supplied counts, complete denominator and separate population rows', () => {
  const html = render(complete);
  for (const value of ['Both correct · 130', 'Fixed · 40', 'Regressed · 10', 'Both incorrect · 20',
    'Learning · 200 complete pairs', 'Excluded', 'Unknown', 'Missing candidate results', 'Expected pairs']) {
    assert.ok(html.includes(value), value);
  }
  assert.equal(html.match(/class="ot-paired-mark"/g)?.length, 4);
});

it('preserves the partial population without assigning dispositions to missing candidates', () => {
  const html = render(partial);
  assert.match(html, /180 complete pairs/);
  assert.match(html, /Missing candidate results<[^<]+<\/div><\/th><td[^>]*><div[^>]*><span[^>]*>20</);
  assert.match(html, /Expected pairs<[^<]+<\/div><\/th><td[^>]*><div[^>]*><span[^>]*>200</);
  assert.ok(!html.includes('class="ot-paired-mark"'));
  assert.ok(!/passed|promotion|release gate/i.test(html));
});

it('draws only observed positive counts and labels zero, missing and unknown explicitly', () => {
  const html = render(mixed);
  assert.equal(html.match(/class="ot-paired-mark"/g)?.length, 1);
  for (const text of ['Fixed · 0', 'Regressed · Unknown · Count not supplied', 'Both incorrect · Missing · Result unavailable']) {
    assert.ok(html.includes(text), text);
  }
});

it('accepts a genuinely empty population with no fabricated mark or undefined geometry', () => {
  const zero = { status: 'observed', count: 0 } as const;
  const html = render({ ...complete, expectedPairs: 0, completePairs: 0,
    outcomes: { bothCorrect: zero, fixed: zero, regressed: zero, bothIncorrect: zero } });
  assert.match(html, /0 complete pairs/);
  assert.doesNotMatch(html, /ot-paired-mark|NaN|Infinity/);
});

it('rejects invalid counts and outcomes inconsistent with the supplied complete denominator', () => {
  for (const value of [-1, 0.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => render({ ...partial, excluded: { status: 'observed', count: value } }));
  }
  assert.throws(() => render({ ...complete, completePairs: 199 }));
  assert.throws(() => render({ ...complete, completePairs: 201, expectedPairs: 210 }));
  assert.throws(() => render({ ...partial, expectedPairs: 190 }));
  assert.throws(() => render({ ...partial, completePairs: 201 }));
  assert.throws(() => render({ ...partial, unknown: { status: 'unknown', reason: ' ' } }));
});
