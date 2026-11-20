import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { EffortLedger } from './effort-ledger';
import { historyEntries, referenceEntries } from './effort-ledger.fixtures';

it('keeps active failed diagnosis, external waiting and timed expert labor on separate rows', () => {
  const html = renderToStaticMarkup(<EffortLedger access="authorized" entries={historyEntries} />);
  const rows = html.split('<tbody')[1].split('</tr>');
  assert.match(rows[0], /Failed work.*18 min active \/ 0 min waiting/);
  assert.match(rows[1], /External wait.*0 min active \/ 42 min waiting/);
  assert.match(rows[3], /Deployment.*60 min active \/ 0 min waiting.*Provider · Timer/);
  assert.match(rows[2], /Mina Patel · Customer · Reported/);
  assert.equal((html.match(/scope="row"/g) ?? []).length, historyEntries.length);
  assert.doesNotMatch(html, /Record effort|Append correction|<button/);
});

it('retains caller order, all categories, supplied intervals, work item and correction reference', () => {
  const html = renderToStaticMarkup(<EffortLedger access="authorized" entries={historyEntries} />);
  const rows = html.split('<tbody')[1].split('</tr>');
  historyEntries.forEach((entry, i) => assert.ok(rows[i].includes(entry.id + ' · ' + entry.category)));
  assert.match(rows[0], /work-042.*UTC interval: 2026-11-16 09:00 – 2026-11-16 09:18/);
  assert.match(rows[4], /Active minutes unavailable \/ 0 min waiting.*End unavailable/);
  assert.match(rows[5], /4 min active \/ Waiting minutes unavailable/);
  assert.match(rows[6], /Supersedes: effort-001.*16 min active/);
  assert.doesNotMatch(rows[1], /Work item|UTC interval|Supersedes/);
});

it('renders an access notice even when a caller retains earlier props', () => {
  const stale = { access: 'revoked' as const, entries: referenceEntries };
  const html = renderToStaticMarkup(<EffortLedger {...stale} />);
  assert.match(html, /Access revoked/);
  assert.doesNotMatch(html, /effort-001|Jordan Davis|External wait|Mina Patel|<table/);
});
