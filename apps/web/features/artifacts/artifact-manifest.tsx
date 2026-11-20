import { useId } from 'react';
import { LabelledRecordGrid } from '../../components/foundation/data-display/relationship-context';
import { DerivedValue } from '../../components/foundation/forms/derived-value';
import { Notice } from '../../components/foundation/tokens/notice';
import { Link } from '../../components/foundation/tokens/primitives';
import './artifact-manifest.css';

type Digest = { label: string; value: string; copyable: boolean };
type Intervention = 'Model checkpoint' | 'Rule repair' | 'Retrieval' | 'Policy' | 'Adapter';
type Checkpoint = { state: 'available'; reference: string } | { state: 'missing' | 'error' };

/** Undefined fields are withheld, explicit nulls are unavailable measurements. */
export interface AuthorizedManifest {
  intervention: Intervention;
  checkpoint?: Checkpoint | null;
  checksums?: string | null;
  rights?: string | null;
  servingIdentity?: { reference: string; href?: string } | null;
  digests?: readonly Digest[];
}

export interface RouteEvidence {
  compatibility?: string | null;
  integrity?: string | null;
  licensePurpose?: string | null;
  registration?: string | null;
}

export type ArtifactManifestProps =
  | { access: 'authorized'; manifest: AuthorizedManifest; qualification: RouteEvidence }
  | { access: 'restricted' };

function checkpointValue({ checkpoint, intervention }: AuthorizedManifest) {
  if (checkpoint === undefined) return undefined;
  if (checkpoint === null) return intervention === 'Rule repair' ? 'Not required for rule repair' : 'Unavailable';
  if (checkpoint.state === 'available') return checkpoint.reference;
  return checkpoint.state === 'error' ? 'Checkpoint could not be read' : 'Unavailable';
}

export function ArtifactManifest(props: ArtifactManifestProps) {
  const id = useId();
  if (props.access !== 'authorized') return <Notice tone="unknown" title="Access unavailable"
    body="Artifact evidence is unavailable for your current access." />;
  const { manifest, qualification } = props;
  const fields = [
    ['Intervention type', manifest.intervention], ['Checkpoint', checkpointValue(manifest)],
    ['File checksums', manifest.checksums], ['Rights and license', manifest.rights],
    ['Serving identity', manifest.servingIdentity === null ? null : manifest.servingIdentity?.reference],
    ...(manifest.digests ?? []).filter(digest => !digest.copyable).map(digest => [digest.label, digest.value] as const),
  ] as const;
  const checks = [
    ['Runtime profile compatibility', qualification.compatibility], ['Artifact integrity', qualification.integrity],
    ['License / purpose', qualification.licensePurpose], ['Serving registration', qualification.registration],
  ] as const;
  const rows = (values: readonly (readonly [string, string | null | undefined])[]) =>
    values.flatMap(([label, value]) => value === undefined ? [] : [{ key: label, cells: [label, value ?? 'Unavailable'] }]);
  const results = rows(checks);
  return <section className="ot-artifact-manifest" aria-labelledby={id}>
    <h2 id={id}>Artifact manifest</h2>
    <LabelledRecordGrid name={{ 'aria-labelledby': id }} columns={['Field', 'Current value']} rows={rows(fields)} />
    {manifest.servingIdentity?.href && <Link label="View serving identity" href={manifest.servingIdentity.href} size="label" />}
    {manifest.digests?.filter(digest => digest.copyable).map(digest => <DerivedValue key={digest.label}
      label={digest.label} value={digest.value} helpText="Exact digest from the supplied manifest."
      copyLabel={'Copy ' + digest.label.toLowerCase()} copyFeedback={{ success: 'Digest copied.', failure: 'Clipboard unavailable. Select the digest to copy it.' }} />)}
    {results.length > 0 && <>
      <h2 id={id + '-checks'}>Route qualification</h2>
      <LabelledRecordGrid name={{ 'aria-labelledby': id + '-checks' }} columns={['Check', 'Result']} rows={results} />
    </>}
  </section>;
}
