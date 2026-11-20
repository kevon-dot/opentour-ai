import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { ComparatorEvidence } from './comparator-evidence';
import { comparatorFixtures } from './comparator-evidence.fixtures';

for (const [state, props] of Object.entries(comparatorFixtures)) {
  it('renders the frozen caller projection for ' + state, () => {
    const markup = renderToStaticMarkup(<ComparatorEvidence {...props} />);
    for (const value of [props.parity.state, props.parity.reason, props.frozenIdentity, props.selectionPolicy,
      props.tieHandling, props.coverage.detail, props.recommendation.result, props.recommendation.reason, props.recommendation.nextStep]) {
      assert.ok(markup.includes(value));
    }
    assert.doesNotMatch(markup, /<(button|form|input|a)\b/i);
  });
}
it('retains a zero-error tie when cost changes', () => {
  const { tied } = comparatorFixtures;
  const alternatives = tied.alternatives.map((row, index) => index ? row : { ...row, costPerCase: { value: 'USD 0.001' } });
  const markup = renderToStaticMarkup(<ComparatorEvidence {...tied} alternatives={alternatives} />);
  for (const value of ['Quality results are tied', 'Do not train', 'No demonstrated training advantage', 'USD 0.001']) assert.ok(markup.includes(value));
  assert.doesNotMatch(markup, /quality winner|best alternative/i);
});
