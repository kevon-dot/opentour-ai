'use client';

import { useCallback, useId, useRef, useState } from 'react';
import { Notice } from '../../components/foundation/tokens/notice';
import { Link } from '../../components/foundation/tokens/primitives';
import './activity-history.css';

/** Display projection. The parent omits actor, reason, receipt and destination independently. */
export interface ActivityEvent {
  id: string;
  time: string;
  timestamp: string;
  event: string;
  operation?: string;
  actor?: string;
  reason?: string;
  receipt?: string;
  detail?: { label: string; href: string };
}

export interface ActivitySnapshot {
  generation: string;
  events: readonly ActivityEvent[];
  exhausted: boolean;
  readOnly: boolean;
}
export interface ActivityHistoryProps {
  snapshot: ActivitySnapshot;
  onLoadOlder: () => void;
}

function uniqueEvents(events: readonly ActivityEvent[]) {
  const seen = new Set<string>();
  return events.filter(event => {
    if (seen.has(event.id)) return false;
    seen.add(event.id);
    return true;
  });
}

export function ActivityHistory({ snapshot, onLoadOlder }: ActivityHistoryProps) {
  const id = useId();
  const loadButton = useRef<HTMLButtonElement>(null);
  const receiptRef = useCallback((link: HTMLAnchorElement | null) => {
    if (link) return () => {
      if (link === document.activeElement) loadButton.current?.focus();
    };
  }, []);
  const events = uniqueEvents(snapshot.events);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [previous, setPrevious] = useState(snapshot);
  const [announcement, setAnnouncement] = useState({ sequence: 0, text: '' });
  if (previous !== snapshot) {
    setPrevious(snapshot);
    if (previous.generation !== snapshot.generation) {
      setExpanded(null);
      setAnnouncement({ sequence: announcement.sequence + 1, text: 'Activity refreshed; prior snapshot expired' });
    } else {
      const previousIds = new Set(previous.events.map(event => event.id));
      const added = events.filter(event => !previousIds.has(event.id)).length;
      if (added > 0) setAnnouncement({ sequence: announcement.sequence + 1,
        text: added + (added === 1 ? ' older event loaded' : ' older events loaded'),
      });
      if (expanded && !events.some(event => event.id === expanded)) setExpanded(null);
    }
  }
  const selected = events.find(event => event.id === expanded);
  return <section className="ot-activity-history" aria-labelledby={id}>
    {snapshot.readOnly && <Notice tone="info" title="Read-only access"
      body="Your current access permits activity review. Automation controls are unavailable." />}
    <div className="ot-activity-panel">
      <h2 id={id}>Activity history</h2>
      <table role="table" aria-labelledby={id}>
        <thead role="rowgroup"><tr role="row">{['Time', 'Event', 'Actor / receipt'].map(label =>
          <th key={label} scope="col" role="columnheader">{label}</th>)}</tr></thead>
        <tbody role="rowgroup">{events.map(event => <tr key={event.id} role="row">
          <th scope="row" role="rowheader">{event.time}</th>
          <td role="cell"><span className="ot-activity-prefix" aria-hidden="true">Event: </span>{event.event}
            <button type="button" className="ot-activity-disclosure" aria-expanded={event.id === expanded}
              aria-controls={event.id === expanded ? id + '-detail' : undefined}
              aria-label={(event.id === expanded ? 'Hide details' : 'Show details') + ' for ' + event.event}
              onClick={() => setExpanded(event.id === expanded ? null : event.id)}>
              {event.id === expanded ? 'Hide details' : 'Show details'}
            </button>
          </td>
          <td role="cell"><span className="ot-activity-prefix" aria-hidden="true">Actor / receipt: </span>
            {[event.actor, event.receipt].filter(Boolean).join(' · ') || 'Unavailable'}</td>
        </tr>)}</tbody>
      </table>
    </div>
    {selected && <section id={id + '-detail'} className="ot-activity-panel" aria-label={selected.event}>
      <h3>{selected.event}</h3><p>{selected.timestamp}</p>
      {selected.actor && <p>Actor: {selected.actor}</p>}
      {selected.operation && <p>Operation: {selected.operation}</p>}
      {selected.reason && <p>Reason: {selected.reason}</p>}
      {selected.receipt && <p>Source receipt: {selected.receipt}</p>}
    </section>}
    <div className="ot-activity-actions" role="group" aria-label="Activity actions">
      {selected?.detail && <Link key={selected.detail.href} ref={receiptRef} size="label" {...selected.detail} />}
      <button ref={loadButton} className="ot-button ot-button--secondary ot-activity-load" type="button"
        aria-disabled={snapshot.exhausted || undefined} onClick={() => { if (!snapshot.exhausted) onLoadOlder(); }}>
        Load older activity
      </button>
    </div>
    <div role="status" aria-atomic="true">
      {announcement.text && <p key={announcement.sequence}>{announcement.text}</p>}
      {snapshot.exhausted && <p>You have reached the beginning.</p>}
    </div>
  </section>;
}
