import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { BlockingReasons } from './blocking-reasons';
import { blockerFixtures } from './blocking-reasons.fixtures';

it('projects one, three and thirty reasons without reordering or hiding them', () => {
  for (const props of [blockerFixtures.one, blockerFixtures.three, blockerFixtures.many]) {
    const html = renderToStaticMarkup(<BlockingReasons {...props} />);
    assert.equal((html.match(/<li\b/g) ?? []).length, props.reasons.length);
    let offset = -1;
    for (const row of props.reasons) {
      const next = html.indexOf(row.title, offset + 1);
      assert.ok(next > offset);
      offset = next;
    }
  }
});
it('omits missing or unsafe recovery destinations and their display labels', () => {
  for (const href of ['javascript:alert(1)', '//foreign.example/x', '/\\foreign.example', 'https://foreign.example', '']) {
    const html = renderToStaticMarkup(<BlockingReasons reasons={[{ key: 'test', title: 'Current reason', recovery: { href, label: 'Hidden unsafe recovery' } }]} />);
    assert.doesNotMatch(html, /<a\b|Hidden unsafe recovery|Next scheduled check/);
  }
  const html = renderToStaticMarkup(<BlockingReasons {...blockerFixtures.one} />);
  assert.doesNotMatch(html, /<nav|<a\b|Permitted recovery|Next scheduled check/);
});
it('distinguishes an empty supplied view and rejects out-of-contract counts', () => {
  const html = renderToStaticMarkup(<BlockingReasons {...blockerFixtures.none} />);
  assert.match(html, /No action scheduled/);
  assert.doesNotMatch(html, /<li\b|<nav|Next scheduled check/);
  assert.throws(() => renderToStaticMarkup(<BlockingReasons reasons={[...blockerFixtures.many.reasons, blockerFixtures.many.reasons[0]]} />), RangeError);
});
