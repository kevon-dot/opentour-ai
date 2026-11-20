import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { VerifierQualification } from './verifier-qualification';
import { verifierFixtures } from './verifier-qualification.fixtures';

it('uses only supplied qualification and historical impact results', () => {
  const markup = renderToStaticMarkup(<VerifierQualification {...verifierFixtures.exact} />);
  for (const row of verifierFixtures.exact.slices) assert.ok(markup.includes(row.result) && markup.includes(row.reason));
  for (const row of verifierFixtures.exact.impacts) assert.ok(markup.includes(row.state) && markup.includes(row.nextStep));
  assert.doesNotMatch(markup, /Comparison evidence|<button|<a\b/);
});
it('suppresses comparison details on withheld slices even if extra fields arrive', () => {
  const comparison = { disclosure: 'withheld', samples: 4917, agreement: 'sentinel comparison', identity: 'sentinel reviewer' } as const;
  const slices = [{ ...verifierFixtures.exact.slices[0], comparison }];
  const markup = renderToStaticMarkup(<VerifierQualification {...verifierFixtures.exact} slices={slices} />);
  assert.doesNotMatch(markup, /4917|sentinel|Comparison evidence/);
});
it('preserves the reported failure even with unanimous agreement, and zero samples', () => {
  const markup = renderToStaticMarkup(<VerifierQualification {...verifierFixtures.measured} />);
  for (const value of ['Failed', '731 / 731', '3 / 200', '>0<']) assert.ok(markup.includes(value));
});
