import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { IconButton } from '../../../components/foundation/tokens/icon-button';

import './icon-button.css';

function IconButtonGallery() {
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(false);
  return <section aria-label="Icon button states" className="ot-foundation icon-button-gallery">
    <h1>Icon button · 44 px target</h1>
    <div className="icon-examples">{['Default', 'Hover', 'Focus', 'Disabled', 'Loading'].map(state =>
      <div className="icon-example" key={state} data-state={state.toLowerCase()}>
        <span>{state}</span>
        <IconButton label={'Close ' + state.toLowerCase() + ' example'} disabled={state === 'Disabled'}
          loading={state === 'Loading' || (state === 'Default' && loading)}
          onClick={() => { setCount(count => count + 1); if (state === 'Default') setLoading(true); }} />
      </div>)}</div>
    <output aria-label="Activation count">{count}</output>
  </section>;
}
createRoot(document.querySelector('main')!).render(<IconButtonGallery />);
