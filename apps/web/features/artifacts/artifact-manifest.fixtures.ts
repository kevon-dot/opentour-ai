import type { ArtifactManifestProps, AuthorizedManifest, RouteEvidence } from './artifact-manifest';

export const fixtureDigest = 'sha256:' + '0123456789abcdef'.repeat(4);
const model: AuthorizedManifest = {
  intervention: 'Model checkpoint', checkpoint: { state: 'available', reference: 'fixture-checkpoint-02' },
  checksums: 'fixture-checksum-manifest-02', rights: 'fixture-rights-commercial-02 / evaluation',
  servingIdentity: { reference: 'fixture-serving-incumbent-01', href: '/serving/fixture-serving-incumbent-01' },
  digests: [
    { label: 'Runtime digest', value: fixtureDigest, copyable: true },
    { label: 'Tokenizer digest', value: 'sha256:' + 'a'.repeat(64), copyable: false },
  ],
};
const checks: RouteEvidence = {
  compatibility: 'Qualified', integrity: 'Passed', licensePurpose: 'Current', registration: 'Not performed',
};
export type ManifestFixture = 'model' | 'rule' | 'missing' | 'error' | 'restricted';
export function manifestFixture(state: ManifestFixture): ArtifactManifestProps {
  if (state === 'restricted') return { access: 'restricted' };
  if (state === 'rule') return { access: 'authorized', manifest: {
    intervention: 'Rule repair', checkpoint: null, rights: 'fixture-rights-rule-01 / repair',
    checksums: 'fixture-rule-checksum-01', servingIdentity: null,
  }, qualification: checks };
  if (state === 'missing' || state === 'error') return { access: 'authorized',
    manifest: { intervention: 'Model checkpoint', checkpoint: { state }, rights: null },
    qualification: { compatibility: null, integrity: 'Pending', registration: 'Not performed' } };
  return { access: 'authorized', manifest: model, qualification: checks };
}
