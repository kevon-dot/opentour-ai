import { useId } from 'react';
import { Link } from '../../components/foundation/tokens/primitives';
import './blocking-reasons.css';

const responsibilities = {
  automatic: 'Automatic', customer_action: 'Your action', operator_action: 'OpenTour action',
  waiting_external: 'Waiting for external response', none: 'No action scheduled',
} as const;
export type NextResponsibility = keyof typeof responsibilities;
export interface BlockingReason {
  key: string;
  title: string;
  observation?: string;
  responsibility?: NextResponsibility;
  recovery?: { label: string; href: string };
}
export interface BlockingReasonsProps {
  reasons: readonly BlockingReason[];
  nextAction?: { kind: NextResponsibility; detail?: string; observation?: string; nextScheduledCheck?: string };
}

/** Navigation within the caller's authorized workspace. Reject ambiguous or external URLs. */
function safeRecovery(recovery: BlockingReason['recovery']) {
  if (!recovery || !recovery.label.trim()) return undefined;
  const { href } = recovery;
  const base = 'https://workspace.invalid';
  if (!href.trim() || href.trim() !== href || /[\\\u0000-\u001f]/.test(href)) return undefined;
  if (!URL.canParse(href, base) || new URL(href, base).origin !== base || /^[a-z][a-z\d+.-]*:|^\/\//i.test(href)) return undefined;
  return recovery;
}

export function BlockingReasons({ reasons, nextAction }: BlockingReasonsProps) {
  const prefix = useId();
  if (reasons.length > 30) throw new RangeError('At most 30 current blocking reasons may be displayed.');
  const rows = reasons.map((reason, index) => ({
    ...reason, recovery: safeRecovery(reason.recovery), titleId: prefix + '-' + index,
  }));
  return <div className="ot-blocking-reasons">
    <section className="ot-blocking-panel" aria-labelledby={prefix + '-heading'}>
      <h2 id={prefix + '-heading'}>Blocking reasons</h2>
      <p>{rows.length} current {rows.length === 1 ? 'reason' : 'reasons'}</p>
      {rows.length > 0 && <ol role="list">{rows.map(row => <li key={row.key} className="ot-blocking-record">
        <h3 id={row.titleId}>{row.title}</h3>
        <div>
          {row.observation && <p>Source observation: {row.observation}</p>}
          {row.recovery && <p>Permitted recovery: {row.recovery.label}</p>}
          {row.responsibility && <p>Next action · {responsibilities[row.responsibility]}</p>}
        </div>
      </li>)}</ol>}
    </section>
    {nextAction && <section className="ot-blocking-record" aria-labelledby={prefix + '-next'}>
      <h3 id={prefix + '-next'}>Next action · {responsibilities[nextAction.kind]}</h3>
      <div>
        {nextAction.detail && <p>{nextAction.detail}</p>}
        {nextAction.observation && <p>Source observation {nextAction.observation}</p>}
        {nextAction.nextScheduledCheck && <p>Next scheduled check {nextAction.nextScheduledCheck}</p>}
      </div>
    </section>}
    {rows.some(row => row.recovery) && <nav aria-label="Permitted recovery" className="ot-blocking-recovery">
      {rows.map(row => row.recovery && <div key={row.key} role="group" aria-labelledby={row.titleId}>
        <Link size="label" href={row.recovery.href} label={row.recovery.label} />
      </div>)}
    </nav>}
  </div>;
}
