'use client';

import { useId, useLayoutEffect, useRef } from 'react';
import { Notice } from '../../components/foundation/tokens/notice';
import '../../styles/product-tokens.css';
import './effort-ledger.css';

export type EffortCategory = 'Setup' | 'Change' | 'Review' | 'Failed work' | 'Deployment' | 'External wait';

/** Authorized display projection. Omitted metadata is withheld. A null observation is unavailable. */
export interface EffortLedgerEntry {
  readonly id: string;
  readonly category: EffortCategory;
  readonly person: string;
  readonly actorClass: 'Customer' | 'Provider';
  readonly sourceQuality: 'Timer' | 'Reported' | 'Imported';
  readonly activeMinutes: number | null;
  readonly waitingMinutes: number | null;
  readonly interval?: { readonly startUtc: string | null; readonly endUtc: string | null };
  readonly workItem?: string;
  /** The authorized display reference for the original immutable entry. */
  readonly supersedes?: string;
}

export type EffortLedgerProps =
  | { access: 'authorized'; entries: readonly EffortLedgerEntry[] }
  | { access: 'revoked' };

function minutes(value: number | null, kind: 'active' | 'waiting') {
  return value === null ? `${kind === 'active' ? 'Active' : 'Waiting'} minutes unavailable` : `${value} min ${kind}`;
}

export function EffortLedger(props: EffortLedgerProps) {
  const titleId = useId();
  const notice = useRef<HTMLDivElement>(null);
  const previousAccess = useRef(props.access);
  useLayoutEffect(() => {
    if (previousAccess.current === 'authorized' && props.access === 'revoked') notice.current?.focus();
    previousAccess.current = props.access;
  }, [props.access]);

  if (props.access !== 'authorized') {
    return <div className="ot-effort-access" tabIndex={-1} ref={notice} role="group" aria-label="Access revoked">
      <Notice tone="failure" title="Access revoked"
        body="Your access to this content was removed. Choose an authorized workspace to continue." />
    </div>;
  }

  return <section className="ot-effort-ledger" aria-labelledby={titleId}>
    <h2 id={titleId}>Effort ledger</h2>
    <table role="table" aria-labelledby={titleId}>
      <thead role="rowgroup"><tr role="row">
        <th role="columnheader" scope="col">Entry / category</th>
        <th role="columnheader" scope="col">Active / waiting</th>
        <th role="columnheader" scope="col">Attribution</th>
      </tr></thead>
      <tbody role="rowgroup">{props.entries.map(entry => <tr role="row" key={entry.id}>
        <th scope="row" role="rowheader">
          {entry.id} · {entry.category}
          {entry.workItem && <span className="ot-effort-detail">Work item: {entry.workItem}</span>}
          {entry.supersedes && <span className="ot-effort-detail">Supersedes: {entry.supersedes}</span>}
        </th>
        <td role="cell">
          <span className="ot-effort-prefix" aria-hidden="true">Active / waiting: </span>
          {minutes(entry.activeMinutes, 'active')} / {minutes(entry.waitingMinutes, 'waiting')}
          {entry.interval && <span className="ot-effort-detail">
            UTC interval: {entry.interval.startUtc ?? 'Start unavailable'} – {entry.interval.endUtc ?? 'End unavailable'}
          </span>}
        </td>
        <td role="cell"><span className="ot-effort-prefix" aria-hidden="true">Attribution: </span>
          {entry.person} · {entry.actorClass} · {entry.sourceQuality}
        </td>
      </tr>)}</tbody>
    </table>
  </section>;
}
