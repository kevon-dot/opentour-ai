import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Badge, Button, Tabs } from '../index';
import { presentationFixtures as fixtures } from '../fixtures';

function Gallery() {
  const [selected, setSelected] = useState('overview');
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState(0);
  const [detailsState, setDetailsState] = useState<'enabled' | 'disabled' | 'removed'>('enabled');
  const query = new URLSearchParams(location.search);
  const mode = query.get('mode');
  const tenant = query.get('tenant') === 'b' ? fixtures.tenants[1] : fixtures.tenants[0];
  if (!fixtures.modes.some(value => value === mode)) throw new Error('Select a declared presentation mode.');
  return <main className="ot-foundation gallery">
    <header><h1>{fixtures.title}</h1><p>{fixtures.description}</p><p>{tenant} · {mode}</p></header>
    <section aria-labelledby="type-heading"><h2 id="type-heading">Typography</h2>
      {fixtures.typography.map(sample => <p key={sample.name} data-type={sample.name} style={{
        fontFamily: sample.name === 'mono' ? '"Geist Mono", monospace' : 'Geist, sans-serif',
        fontSize: `var(--ot-type-${sample.name}-size)`, lineHeight: `var(--ot-type-${sample.name}-line)`,
        fontWeight: `var(--ot-type-${sample.name}-weight)`,
      }}>{sample.label}</p>)}
    </section>
    <section aria-labelledby="buttons-heading"><h2 id="buttons-heading">Buttons</h2>
      <div className="examples">{fixtures.variants.map(variant => <div key={variant} className="example" data-variant={variant}>
        <h3>{variant}</h3>
        <Button variant={variant} label={fixtures.buttonLabel} onPress={() => setEvents(value => value + 1)} />
        <Button variant={variant} label={fixtures.buttonLabel} disabled disabledReason={fixtures.disabledReason}
          onPress={() => setEvents(value => value + 1)} />
        <Button variant={variant} label={fixtures.buttonLabel} busy disabledReason={fixtures.busyReason}
          onPress={() => setEvents(value => value + 1)} />
      </div>)}</div>
      <div className="example" data-example="busy-control">
        <Button label={fixtures.busyLabel} busy={busy} onPress={() => { setBusy(true); setEvents(value => value + 1); }} />
        <Button label={fixtures.resetLabel} variant="quiet" onPress={() => setBusy(false)} />
        <output aria-label={fixtures.invocationLabel}>{events}</output>
      </div>
      <Button label={fixtures.longLabel} variant="secondary" />
    </section>
    <section aria-labelledby="badges-heading"><h2 id="badges-heading">Explicit labels</h2>
      <div className="examples">{fixtures.badges.map(badge => <Badge key={badge.tone} {...badge} />)}</div>
    </section>
    <section aria-labelledby="tabs-heading"><h2 id="tabs-heading">Manual tab activation</h2>
      <Tabs label="Generated panels" items={fixtures.tabs.filter(item => item.key !== 'details' || detailsState !== 'removed')
        .map(item => ({ ...item, disabled: ('disabled' in item && item.disabled) || (item.key === 'details' && detailsState === 'disabled'), panel: item.content }))}
        selectedKey={selected} onSelectionChange={setSelected} />
      <div className="examples" data-example="controlled-tabs">
        <Button label={fixtures.selectNotesLabel} variant="quiet" onPress={() => setSelected('notes')} />
        <Button label={fixtures.disableDetailsLabel} variant="quiet" onPress={() => { setSelected('overview'); setDetailsState('disabled'); }} />
        <Button label={fixtures.removeDetailsLabel} variant="quiet" onPress={() => { setSelected('overview'); setDetailsState('removed'); }} />
      </div>
    </section>
  </main>;
}

const root = document.getElementById('root');
if (!root) throw new Error('Gallery root missing.');
createRoot(root).render(<Gallery />);
