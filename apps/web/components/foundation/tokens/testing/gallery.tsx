import React, { StrictMode, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Badge, Button, Dialog, Drawer, Tabs } from '../index';
import type { InitialFocus } from '../contracts';
import { overlayFixtures as overlays, presentationFixtures as fixtures } from '../fixtures';

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
const query = new URLSearchParams(location.search);
const gallery = query.has('overlay') ? <OverlayGallery /> : <Gallery />;
createRoot(root).render(query.get('strict') === '1' ? <StrictMode>{gallery}</StrictMode> : gallery);

function OverlayGallery() {
  const kind = query.get('overlay') === 'drawer' ? 'drawer' : 'dialog';
  const Component = kind === 'drawer' ? Drawer : Dialog;
  const long = query.get('content') === 'long';
  const probe = query.get('probe') === '1';
  const mode = query.get('mode');
  if (!fixtures.modes.some(value => value === mode)) throw new Error('Select a declared presentation mode.');
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(true);
  const [generation, setGeneration] = useState(0);
  const [openEvents, setOpenEvents] = useState(0);
  const [requests, setRequests] = useState<string[]>([]);
  const [events, setEvents] = useState(0);
  const [renders, setRenders] = useState(0);
  const [context, setContext] = useState('a');
  const [origin, setOrigin] = useState<'available' | 'disabled' | 'removed'>('available');
  const [fallback, setFallback] = useState(true);
  const originRef = useRef<HTMLButtonElement>(null);
  const fallbackRef = useRef<HTMLButtonElement>(null);
  const currentRef = useRef<HTMLButtonElement>(null);
  const elementRef = useRef<HTMLButtonElement>(null);
  const absentRef = useRef<HTMLElement>(null);
  const initialFocus: InitialFocus = query.get('focus') === 'invalid' ? { kind: 'element', ref: absentRef } :
    query.get('focus') === 'outside' ? { kind: 'element', ref: originRef } :
    ['element', 'disabled'].includes(query.get('focus') ?? '') ? { kind: 'element', ref: elementRef } :
      long || query.get('focus') === 'heading' ? { kind: 'heading' } : { kind: 'close-button' };
  function begin() { setOpenEvents(value => value + 1); setMounted(true); setOpen(true); }
  return <main className="ot-foundation gallery overlay-gallery">
    <header><h1>Generated overlay examples</h1><p>{overlays.caption}</p><p>{mode}</p></header>
    <div className="examples">
      {origin !== 'removed' && <Button ref={originRef} label="Open generated overlay" disabled={origin === 'disabled'} onPress={begin} />}
      <Button label="Reopen generated overlay" onPress={begin} />
      {fallback && <Button ref={fallbackRef} label="Safe current fallback" variant="secondary" />}
      <Button ref={currentRef} label="Current context target" variant="secondary" />
    </div>
    <output aria-label="Dismissal requests">{requests.join(',')}</output>
    <output aria-label="Overlay action events">{events}</output>
    <output aria-label="Generated render count">{renders}</output>
    {probe && <output aria-label="Generated opening events">{openEvents}</output>}
    <form onSubmit={event => { event.preventDefault(); setEvents(value => value + 100); }}>
      {mounted && <Component key={generation} open={open} title={kind === 'dialog' ? overlays.dialogTitle : overlays.drawerTitle}
        initialFocus={initialFocus} returnFocus={() => context === 'a' ? originRef.current : context === 'b' ? currentRef.current : null}
        fallbackFocus={() => fallbackRef.current}
        onDismiss={reason => { setRequests(values => [...values, reason]); if (query.get('ignore') !== '1') setOpen(false); }}
        actions={!long && (kind === 'dialog' || probe) ? <Button ref={elementRef} label="Example action" disabled={query.get('focus') === 'disabled'} onPress={() => setEvents(value => value + 1)} /> : undefined}>
        {long ? overlays.sections.map(([heading, body]) => <React.Fragment key={heading}>
          <h3>{heading}</h3><p>{body}</p>
        </React.Fragment>) : <><p>{kind === 'dialog' ? overlays.dialogBody : overlays.drawerBody}</p><p className="overlay-caption">{overlays.caption}</p></>}
        {probe && <div className="overlay-probes">
          <Button label="Rerender generated content" onPress={() => setRenders(value => value + 1)} />
          <Button label="Disable origin" onPress={() => setOrigin('disabled')} />
          <Button label="Remove origin" onPress={() => setOrigin('removed')} />
          <Button label="Change local context" onPress={() => setContext('b')} />
          <Button label="Remove both return targets" onPress={() => { setContext('none'); setFallback(false); setOrigin('disabled'); }} />
          <Button label="Caller closes" onPress={() => setOpen(false)} />
          <Button label="Unmount overlay" onPress={() => setMounted(false)} />
          <Button label="Replace open instance" onPress={() => { setContext('b'); setGeneration(value => value + 1); }} />
          <p>Generated render {renders}</p>
        </div>}
      </Component>}
    </form>
  </main>;
}
