import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { ArtifactManifest } from './artifact-manifest';
import { fixtureDigest, manifestFixture } from './artifact-manifest.fixtures';

it('separates the model checkpoint, rights, exact digest and independent route results', () => {
  const html = renderToStaticMarkup(<ArtifactManifest {...manifestFixture('model')} />);
  for (const text of ['fixture-checkpoint-02', 'fixture-rights-commercial-02 / evaluation', fixtureDigest,
    'Runtime profile compatibility', 'Qualified', 'Artifact integrity', 'Passed', 'License / purpose',
    'Current', 'Serving registration', 'Not performed']) assert.ok(html.includes(text));
  assert.match(html, /scope="col">Current value/);
  assert.match(html, /scope="row">Serving registration/);
  assert.equal((html.match(/<button/g) ?? []).length, 1);
  assert.doesNotMatch(html, /Release authorized|Traffic active/);
});

it('renders allowed null rule checkpoint, missing model and read error distinctly', () => {
  for (const [state, expected] of [['rule', 'Not required for rule repair'], ['missing', 'Unavailable'],
    ['error', 'Checkpoint could not be read']] as const) {
    const html = renderToStaticMarkup(<ArtifactManifest {...manifestFixture(state)} />);
    assert.ok(html.includes(expected));
    assert.doesNotMatch(html, /Copy runtime digest/);
  }
});

it('omits withheld fields and refuses stale state after a restriction', () => {
  const limited = renderToStaticMarkup(<ArtifactManifest access="authorized" manifest={{ intervention: 'Policy' }} qualification={{}} />);
  assert.doesNotMatch(limited, /Rights and license|Serving identity|Route qualification|Checkpoint/);
  const stale = { ...manifestFixture('model'), access: 'restricted' as const };
  const html = renderToStaticMarkup(<ArtifactManifest {...stale} />);
  assert.match(html, /Access unavailable/);
  assert.doesNotMatch(html, /fixture-|sha256|Copy|Qualified|<table|href=/);
});
