import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { ActivityHistory } from './activity-history';
import { activityFixture, referenceEvents } from './activity-history.fixtures';

it('names each disclosure for its event and preserves the reference row order', () => {
  const html = renderToStaticMarkup(<ActivityHistory snapshot={activityFixture('reference')} onLoadOlder={() => {}} />);
  let position = -1;
  for (const event of referenceEvents) {
    const next = html.indexOf('Show details for ' + event.event);
    assert.ok(next > position);
    position = next;
  }
  assert.equal((html.match(/aria-expanded="false"/g) ?? []).length, 4);
  assert.doesNotMatch(html, /EXACT_JOIN_PROGRESS|<a /);
});

it('suppresses overlap between pages and presents the exhausted snapshot explicitly', () => {
  const html = renderToStaticMarkup(<ActivityHistory snapshot={activityFixture('exhausted')} onLoadOlder={() => {}} />);
  assert.equal((html.match(/scope="row"/g) ?? []).length, 6);
  assert.match(html, /You have reached the beginning\./);
  assert.match(html, /aria-disabled="true"/);
});

it('keeps read-only review available and omits withheld actor, receipt, detail and operation controls', () => {
  const html = renderToStaticMarkup(<ActivityHistory snapshot={activityFixture('read-only')} onLoadOlder={() => {}} />);
  assert.match(html, /Read-only access/);
  assert.match(html, /Show details for Published activity/);
  assert.doesNotMatch(html, /System|labels-015|<a |Pause automation|Stop current cycle/);
});
