import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { SupportNextSteps, type SupportNextStepsProps } from './support-next-steps';
import { supportFixtures } from './support-next-steps.fixtures';

const render = (props: SupportNextStepsProps) => renderToStaticMarkup(<SupportNextSteps {...props} />);

it('shows exactly the supplied resources for each partial projection, including an empty view', () => {
  const keys = ['supportCase', 'incident', 'additionalGrant'] as const;
  for (let mask = 0; mask < 8; mask++) {
    const projection: SupportNextStepsProps = { ...supportFixtures.cleared };
    for (const [index, key] of keys.entries()) {
      if (mask & (1 << index)) projection[key] = supportFixtures.case41[key];
    }
    const html = render(projection);
    for (const [index, key] of keys.entries()) {
      assert.equal(html.includes(supportFixtures.case41[key].resource), Boolean(mask & (1 << index)));
    }
    if (mask === 0) assert.equal(html, '');
  }
});

it('keeps owner, next update and enforcement independently absent or present', () => {
  const withoutOwner = render({ ...supportFixtures.cleared,
    supportCase: { ...supportFixtures.case41.supportCase, nextUpdate: '17 Nov, 18:00 UTC' } });
  assert.match(withoutOwner, /<dt>Next update<\/dt><dd>17 Nov, 18:00 UTC<\/dd>/);
  assert.doesNotMatch(withoutOwner, /<dt>Owner|Additional grant|Related incident/);
  const closed = render(supportFixtures.closed);
  for (const value of ['Closed', 'Investigating', 'Expired', 'Access removal pending', 'Generated support owner']) {
    assert.ok(closed.includes(value));
  }
  assert.doesNotMatch(closed, /<dt>Next update|<button|href=/);
  const enforcement = render({ ...supportFixtures.cleared, enforcement: 'Draining' });
  assert.match(enforcement, /<dt>Access enforcement<\/dt><dd>Draining<\/dd>/);
  assert.doesNotMatch(enforcement, /<table|Additional grant/);
});
