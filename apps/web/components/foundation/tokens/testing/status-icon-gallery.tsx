import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { StatusIcon } from '../status-icon';
import { statusIconFixtures as fixtures } from '../status-icon.fixtures';
import './status-icon-gallery.css';

function Gallery() {
  const [renders, setRenders] = useState(0);
  const mode = new URLSearchParams(location.search).get('mode');
  if (!fixtures.modes.some(value => value === mode)) throw new Error('Select a generated presentation mode.');
  return <main className="ot-foundation status-icon-gallery" data-mode={mode} data-renders={renders}>
    <div className="status-icon-guide">
      <h1>{fixtures.title}</h1>
      <div className="status-icon-examples">{fixtures.examples.map(example => <section className="status-icon-example" key={example.tone} data-tone={example.tone}>
        <h2>{example.tone[0].toUpperCase() + example.tone.slice(1)}</h2><StatusIcon {...example} />
      </section>)}</div>
    </div>
    <p>{fixtures.disclaimer}</p><p>{mode}</p>
    <button type="button">{fixtures.beforeLabel}</button>
    <div className="status-icon-additional"><StatusIcon {...fixtures.nonstandard} /><StatusIcon {...fixtures.long} /></div>
    <button type="button">{fixtures.afterLabel}</button>
    <button type="button" onClick={() => setRenders(value => value + 1)}>{fixtures.rerenderLabel}</button>
  </main>;
}
const root = document.getElementById('root');
if (!root) throw new Error('Status icon gallery root is required.');
createRoot(root).render(<Gallery />);
