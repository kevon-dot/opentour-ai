import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Skeleton } from '../skeleton';
import type { SkeletonVariant } from '../skeleton.contracts';
import { skeletonFixtures as fixtures } from '../skeleton.fixtures';
import './skeleton-gallery.css';

function SkeletonGallery() {
  const query = new URLSearchParams(location.search);
  const variant = query.get('variant') as SkeletonVariant;
  const mode = query.get('mode');
  if (!fixtures.variants.some(value => value === variant) || !fixtures.modes.some(value => value === mode)) {
    throw new Error('Select a declared skeleton variant and generated mode.');
  }
  const [phase, setPhase] = useState<'idle' | 'loading' | 'ready'>('idle');
  const [renders, setRenders] = useState(0);
  return <main className="ot-foundation skeleton-gallery" data-mode={mode} data-variant={variant} data-renders={renders}>
    <header><h1>{fixtures.title}</h1><p>{fixtures.description}</p>
      <p>{query.get('tenant') === 'b' ? fixtures.tenants[1] : fixtures.tenants[0]} · {mode}</p></header>
    <div className="skeleton-controls">
      <button type="button" onClick={() => setPhase('loading')}>{fixtures.startLabel}</button>
      <button type="button" onClick={() => setRenders(value => value + 1)}>{fixtures.rerenderLabel}</button>
      <button type="button" onClick={() => setPhase('ready')}>{fixtures.readyLabel}</button>
    </div>
    <section aria-label="Generated reserved content" className="skeleton-slot" data-phase={phase}>
      {phase === 'loading' && <Skeleton variant={variant} />}
      <div className={`skeleton-ready skeleton-ready--${variant}`} aria-hidden={phase !== 'ready' ? true : undefined}>
        <p className="skeleton-ready-label">{fixtures.loadedTitle}</p>
        {fixtures.loadedRows.map(row => <p className="skeleton-ready-row" key={row}>{row}</p>)}
      </div>
    </section>
    <p className="skeleton-following">{fixtures.followingLabel}</p>
  </main>;
}

const root = document.getElementById('root');
if (!root) throw new Error('Skeleton gallery root is required.');
createRoot(root).render(<SkeletonGallery />);
