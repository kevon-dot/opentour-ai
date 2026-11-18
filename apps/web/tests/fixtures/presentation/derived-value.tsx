import { createRoot } from 'react-dom/client';
import { useLayoutEffect, useState } from 'react';
import { DerivedValue } from '../../../components/foundation/forms/derived-value';
import { derivedValueFixtures as fixtures } from '../../../components/foundation/forms/derived-value.fixtures';
import './derived-value.css';

function Gallery() {
  const [first, setFirst] = useState(fixtures.first);
  const [visible, show] = useState(true);
  const [renders, setRenders] = useState(0);
  const mode = new URLSearchParams(location.search).get('mode');
  useLayoutEffect(() => {
    window.derivedFixture = {
      replace: props => setFirst(previous => ({ ...previous, ...props })),
      rerender: () => setRenders(previous => previous + 1),
      show,
    };
  }, []);
  return <main data-renders={renders}>
    <h1>Generated derived values</h1>
    <p>Generated presentation fixtures · {mode}</p>
    <form aria-label="Generated read-only form" onSubmit={event => event.preventDefault()}>
      <button type="button" className="fixture-sentinel">Before values</button>
      {visible && <div className="derived-first"><DerivedValue {...first} /></div>}
      <div className="derived-long"><DerivedValue {...fixtures.long} /></div>
      <button type="button" className="fixture-sentinel">After values</button>
    </form>
  </main>;
}
createRoot(document.getElementById('root')!).render(<Gallery />);
