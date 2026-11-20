import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { describeProjectDraft, ProjectDescription } from './project-description';

const base = { id: 'description', value: '', onChange: () => assert.fail('Unexpected draft change') };
for (const count of [0, 499, 500, 501]) {
  it(`preserves the complete ${count}-character draft and its exact count`, () => {
    const value = 'a'.repeat(count);
    const html = renderToStaticMarkup(<ProjectDescription {...base} value={value} />);
    assert.deepEqual(describeProjectDraft(value), { count, error: count > 500 ? 'Use 500 characters or fewer.' : undefined });
    assert.ok(html.includes('>' + value + '</textarea>'));
    assert.ok(html.includes(`${count} / 500 characters`));
    assert.doesNotMatch(html, /maxlength/i);
    assert.equal(html.includes('aria-invalid="true"'), count > 500);
    assert.equal(html.includes('id="description-error"'), count > 500);
  });
}

it('counts Unicode code points, including whitespace and newlines', () => {
  assert.deepEqual(describeProjectDraft(' 💬\n '), { count: 4, error: undefined });
});

it('preserves the native label, helper, safe caller error, and unavailable reason', () => {
  const html = renderToStaticMarkup(<ProjectDescription {...base} disabledReason="Unavailable" error="A & B" />);
  assert.match(html, /for="description">Description · Optional/);
  assert.match(html, /id="description"[^>]*disabled=""/);
  assert.match(html, /aria-describedby="description-helper description-error description-unavailable"/);
  assert.match(html, /id="description-error">A &amp; B/);
  assert.match(html, /id="description-unavailable">Unavailable/);
});

it('renders native read-only semantics and accepts a count-only helper', () => {
  const html = renderToStaticMarkup(<ProjectDescription {...base} readOnly helper="" />);
  assert.match(html, /readOnly=""/i);
  assert.match(html, /0 \/ 500 characters<\/p>/);
  assert.doesNotMatch(html, /disabled=""|aria-invalid=/);
});

it('rejects invalid native target IDs and blank supplied disabled reasons', () => {
  assert.throws(() => renderToStaticMarkup(<ProjectDescription {...base} id="bad id" />));
  assert.throws(() => renderToStaticMarkup(<ProjectDescription {...base} disabledReason=" " />));
});
