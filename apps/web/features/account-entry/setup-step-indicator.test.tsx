import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { SetupStepIndicator, type SetupStep } from './setup-step-indicator';

const states = {
  organization: ['Current step', 'Not started', 'Not started'],
  workspace: ['Done', 'Current step', 'Not started'],
  project: ['Done', 'Done', 'Current step'],
} as const;

for (const currentStep of Object.keys(states) as SetupStep[]) {
  it(`renders the supplied ${currentStep} position in the canonical order`, () => {
    const html = renderToStaticMarkup(<SetupStepIndicator currentStep={currentStep} />);
    assert.match(html, /<ol[^>]*aria-label="Setup progress"/);
    const rows = [...html.matchAll(/<li([^>]*)><span[^>]+>([^<]+)<\/span>/g)];
    const names = ['Organization', 'Workspace', 'Project'];
    assert.deepEqual(rows.map(row => row[2]), names.map((name, i) => name + ' · ' + states[currentStep][i]));
    assert.deepEqual(rows.filter(row => row[1].includes('aria-current="step"')).map(row => row[2]),
      rows.filter(row => row[2].endsWith('Current step')).map(row => row[2]));
    assert.doesNotMatch(html, /<(button|form|a|input)\b|tabindex=/i);
  });
}
