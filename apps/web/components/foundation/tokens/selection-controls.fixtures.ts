import type { CheckboxState, SwitchState } from './selection-controls.contracts';

type Specimen<State> = { key: string; checked: boolean; label: string; helper: string; state?: State };
const termsLabel = 'I accept the displayed terms';

/** Generated presentation fixtures. No request, saved preference, authority or provider result is claimed. */
export const selectionControlFixtures = {
  title: 'Checkbox and switch · labelled controlled selection',
  disclaimer: 'Generated presentation examples. A change request is recorded here, never saved, and no provider result is claimed.',
  modes: ['customer-controlled', 'opentour-managed'],
  viewports: [{ width: 320, height: 800 }, { width: 768, height: 1024 },
    { width: 1280, height: 900 }, { width: 1440, height: 1024 }],
  scales: [100, 200],
  /** Native family 12:68. Focus and hover are ordinary specimens reached by real keyboard focus and pointer hover. */
  checkbox: [
    { key: 'unchecked', checked: false, label: 'Checkbox', helper: 'Not selected' },
    { key: 'checked', checked: true, label: 'Checkbox', helper: '✓ Selected' },
    { key: 'disabled', checked: false, label: 'Checkbox', helper: 'Not selected', state: 'disabled' },
    { key: 'error', checked: false, label: 'Checkbox', helper: 'Not selected', state: 'error' },
    { key: 'focus', checked: false, label: 'Checkbox', helper: 'Not selected' },
    { key: 'hover', checked: false, label: 'Checkbox', helper: 'Not selected' },
  ] satisfies Specimen<CheckboxState>[],
  /** Native family 12:81. */
  switch: [
    { key: 'off', checked: false, label: 'Switch', helper: 'Disabled by preference' },
    { key: 'on', checked: true, label: 'Switch', helper: 'Enabled' },
    { key: 'disabled', checked: false, label: 'Switch', helper: 'Disabled by preference', state: 'disabled' },
    { key: 'pending', checked: false, label: 'Switch', helper: 'Saving preference…', state: 'pending' },
    { key: 'focus', checked: false, label: 'Switch', helper: 'Disabled by preference' },
    { key: 'hover', checked: false, label: 'Switch', helper: 'Disabled by preference' },
  ] satisfies Specimen<SwitchState>[],
  /** The other supplied value for each blocked treatment. */
  suppliedCheckboxes: [
    { key: 'disabled-checked', checked: true, label: 'Checkbox', helper: '✓ Selected', state: 'disabled' },
  ] satisfies Specimen<CheckboxState>[],
  suppliedSwitches: [
    { key: 'disabled-on', checked: true, label: 'Switch', helper: 'Enabled', state: 'disabled' },
    { key: 'pending-on', checked: true, label: 'Switch', helper: 'Saving preference…', state: 'pending' },
  ] satisfies Specimen<SwitchState>[],
  /** Native product frames 166:10019, 166:10027, 166:10038 and 166:10053. */
  contexts: [
    { key: 'terms-loading', node: '166:10019', title: 'Terms still loading',
      description: 'The acceptance control remains unavailable until the displayed revision is known.',
      checkboxes: [{ key: 'terms-loading', checked: false, label: termsLabel, helper: 'Terms revision is loading. Review it before accepting.', state: 'disabled' }],
      switches: [] },
    { key: 'terms-error', node: '166:10027', title: 'Terms not accepted',
      description: 'A submit attempt without acceptance identifies the unmet requirement.',
      checkboxes: [{ key: 'terms-error', checked: false, label: termsLabel, helper: 'Accept the displayed terms before continuing.', state: 'error' }],
      switches: [] },
    { key: 'automation', node: '166:10038', title: 'Automation is off',
      description: 'Off is an explicit preference. Disabled is an unmet capability or prerequisite.',
      checkboxes: [],
      switches: [
        { key: 'automation-off', checked: false, label: 'Automation', helper: 'Off · No new automated cycle is admitted.' },
        { key: 'automation-disabled', checked: false, label: 'Enable automation', helper: 'Unavailable until current readiness checks pass.', state: 'disabled' },
      ] },
    { key: 'hover', node: '166:10053', title: 'Pointer hover does not commit',
      description: 'Hover never changes a preference, accepts terms or selects an option.',
      checkboxes: [{ key: 'hover-terms', checked: false, label: termsLabel, helper: 'Pointer over control · Not accepted.' }],
      switches: [{ key: 'hover-automation', checked: false, label: 'Automation', helper: 'Pointer over control · Existing preference unchanged.' }] },
  ] satisfies { key: string; node: string; title: string; description: string;
    checkboxes: Specimen<CheckboxState>[]; switches: Specimen<SwitchState>[] }[],
  long: {
    checkbox: { key: 'long-checkbox', checked: true,
      label: 'Generated acknowledgement with a longer visible label that wraps across several lines without losing any exact word',
      helper: 'Generated helper text stays visibly associated beneath the label and wraps safely at narrow widths and enlarged text sizes.' },
    switch: { key: 'long-switch', checked: false,
      label: 'Generated preference with a longer visible label that wraps across several lines without losing any exact word',
      helper: 'Generated reason text stays visibly associated beneath the label and wraps safely at narrow widths and enlarged text sizes.' },
  },
  interaction: {
    before: 'Before generated controls', after: 'After generated controls',
    selection: { key: 'interactive-checkbox', label: 'Generated selection', helpers: { false: 'Not selected', true: '✓ Selected' } },
    requestOnly: { key: 'request-only-checkbox', label: 'Generated request only', helper: 'Requests are recorded. The supplied value stays not selected.' },
    setting: { key: 'interactive-switch', label: 'Generated setting', helpers: { false: 'Disabled by preference', true: 'Enabled' } },
    preference: { key: 'preference-switch', label: 'Generated preference', pendingHelper: 'Saving preference…',
      helpers: { false: 'Disabled by preference', true: 'Enabled' } },
    parentUpdate: 'Set generated selection from parent',
    apply: 'Apply generated request', discard: 'Discard generated request',
    rerender: 'Repeat presentation render', requestCount: 'Change requests recorded',
  },
  figmaNodes: {
    checkbox: { family: '12:68', unchecked: '12:56', checked: '12:59', disabled: '12:62', error: '12:65', focus: '152:22', hover: '152:27' },
    switch: { family: '12:81', off: '12:69', on: '12:72', disabled: '12:75', pending: '12:78', focus: '152:32', hover: '152:38' },
    guide: '170:4',
  },
} as const;

const desktopCaptures = ['checkbox-unchecked', 'checkbox-checked', 'checkbox-disabled', 'checkbox-error',
  'switch-off', 'switch-on', 'switch-disabled', 'switch-pending',
  'context-terms-loading', 'context-terms-error', 'context-automation', 'context-hover'] as const;
const narrowCaptures = ['checkbox-family', 'switch-family', 'contexts'] as const;
const behaviorCaptures = ['checkbox-interaction', 'switch-interaction', 'controlled-rerender', 'disabled-error', 'pending',
  'checkbox-focus', 'checkbox-hover', 'switch-focus', 'switch-hover', 'context-hover-checkbox', 'context-hover-switch',
  'forced-colors', 'forced-colors-focus', 'reduced-motion'] as const;
export type SelectionControlCaptureState = 'gallery' | typeof desktopCaptures[number]
  | typeof narrowCaptures[number] | typeof behaviorCaptures[number];

export function selectionControlCaptureName(mode: string, width: number, scale: number, state: SelectionControlCaptureState): string {
  return `selection-controls-${mode}-${width}-${scale}-${state}.png`;
}

/** Exact render inventory. `tools/ci/browser_evidence.py` retains a PASS only for this set of file names. */
export function selectionControlCaptureInventory(): string[] {
  return selectionControlFixtures.modes.flatMap(mode => [
    ...selectionControlFixtures.viewports.flatMap(({ width }) => selectionControlFixtures.scales.flatMap(scale => [
      selectionControlCaptureName(mode, width, scale, 'gallery'),
      ...(width === 1280 && scale === 100 ? desktopCaptures : width === 320 && scale === 200 ? narrowCaptures : [])
        .map(state => selectionControlCaptureName(mode, width, scale, state)),
    ])),
    ...behaviorCaptures.map(state => selectionControlCaptureName(mode, 1280, 100, state)),
  ]);
}
