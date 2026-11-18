import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Checkbox, Switch } from '../selection-controls';
import { selectionControlFixtures as fixtures } from '../selection-controls.fixtures';
import './selection-controls-gallery.css';

type Request = { control: string; next: boolean };

function Gallery() {
  const [renders, setRenders] = useState(0);
  const [requests, setRequests] = useState<Request[]>([]);
  const [selection, setSelection] = useState(false);
  const [setting, setSetting] = useState(false);
  const [preference, setPreference] = useState(false);
  const [requested, setRequested] = useState<boolean | null>(null);
  const mode = new URLSearchParams(location.search).get('mode');
  if (!fixtures.modes.some(value => value === mode)) throw new Error('Select a generated presentation mode.');
  // Every specimen records the request. Only the named interaction examples let this parent change a value.
  const record = (control: string, commit?: (next: boolean) => void) => (next: boolean) => {
    setRequests(log => [...log, { control, next }]);
    commit?.(next);
  };
  const { interaction } = fixtures;
  return <main className="ot-foundation selection-gallery" data-mode={mode} data-renders={renders} data-requests={JSON.stringify(requests)}>
    <h1>{fixtures.title}</h1>
    <p>{fixtures.disclaimer}</p><p>{mode}</p>
    <section className="selection-family" data-region="checkbox-family">
      <h2>Checkbox</h2>
      <div className="selection-specimens">
        {[...fixtures.checkbox, ...fixtures.suppliedCheckboxes].map(({ key, ...props }) => <div className="selection-specimen" key={key} data-specimen={`checkbox-${key}`}>
          <Checkbox {...props} onCheckedChange={record(`checkbox-${key}`)} />
        </div>)}
      </div>
    </section>
    <section className="selection-family" data-region="switch-family">
      <h2>Switch</h2>
      <div className="selection-specimens">
        {[...fixtures.switch, ...fixtures.suppliedSwitches].map(({ key, ...props }) => <div className="selection-specimen" key={key} data-specimen={`switch-${key}`}>
          <Switch {...props} onCheckedChange={record(`switch-${key}`)} />
        </div>)}
      </div>
    </section>
    <section className="selection-contexts" data-region="contexts">
      {fixtures.contexts.map(context => <article className="selection-context" key={context.key} data-context={context.key}>
        <h2>{context.title}</h2><p>{context.description}</p>
        {context.checkboxes.map(({ key, ...props }) => <div className="selection-context-control" key={key} data-specimen={key}>
          <Checkbox {...props} onCheckedChange={record(key)} />
        </div>)}
        {context.switches.map(({ key, ...props }) => <div className="selection-context-control" key={key} data-specimen={key}>
          <Switch {...props} onCheckedChange={record(key)} />
        </div>)}
      </article>)}
    </section>
    <section className="selection-family" data-region="long">
      <h2>Long generated text</h2>
      <div className="selection-specimens">
        <div className="selection-specimen" data-specimen={fixtures.long.checkbox.key}>
          <Checkbox checked={fixtures.long.checkbox.checked} label={fixtures.long.checkbox.label} helper={fixtures.long.checkbox.helper}
            onCheckedChange={record(fixtures.long.checkbox.key)} />
        </div>
        <div className="selection-specimen" data-specimen={fixtures.long.switch.key}>
          <Switch checked={fixtures.long.switch.checked} label={fixtures.long.switch.label} helper={fixtures.long.switch.helper}
            onCheckedChange={record(fixtures.long.switch.key)} />
        </div>
      </div>
    </section>
    <section className="selection-family" data-region="interaction">
      <h2>Generated change requests</h2>
      <button type="button">{interaction.before}</button>
      <div className="selection-specimens">
        <div className="selection-specimen" data-specimen={interaction.selection.key}>
          <Checkbox checked={selection} label={interaction.selection.label} helper={interaction.selection.helpers[`${selection}`]}
            onCheckedChange={record(interaction.selection.key, setSelection)} />
        </div>
        <div className="selection-specimen" data-specimen={interaction.requestOnly.key}>
          <Checkbox checked={false} label={interaction.requestOnly.label} helper={interaction.requestOnly.helper}
            onCheckedChange={record(interaction.requestOnly.key)} />
        </div>
        <div className="selection-specimen" data-specimen={interaction.setting.key}>
          <Switch checked={setting} label={interaction.setting.label} helper={interaction.setting.helpers[`${setting}`]}
            onCheckedChange={record(interaction.setting.key, setSetting)} />
        </div>
        <div className="selection-specimen" data-specimen={interaction.preference.key}>
          {/* The requested value is held apart: the switch keeps showing the supplied preference while pending. */}
          <Switch checked={preference} label={interaction.preference.label} state={requested === null ? 'enabled' : 'pending'}
            helper={requested === null ? interaction.preference.helpers[`${preference}`] : interaction.preference.pendingHelper}
            onCheckedChange={record(interaction.preference.key, setRequested)} />
        </div>
      </div>
      <button type="button">{interaction.after}</button>
      <button type="button" onClick={() => setSelection(value => !value)}>{interaction.parentUpdate}</button>
      <button type="button" onClick={() => { if (requested !== null) setPreference(requested); setRequested(null); }}>{interaction.apply}</button>
      <button type="button" onClick={() => setRequested(null)}>{interaction.discard}</button>
      <button type="button" onClick={() => setRenders(value => value + 1)}>{interaction.rerender}</button>
      <p className="selection-request-count">{interaction.requestCount}: {requests.length}</p>
    </section>
  </main>;
}
const root = document.getElementById('root');
if (!root) throw new Error('Selection control gallery root is required.');
createRoot(root).render(<Gallery />);
