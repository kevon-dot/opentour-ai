import type { BadgeTone, ButtonVariant } from './contracts';

/** Generated presentation examples only. No API response, grant, or receipt is simulated. */
export const presentationFixtures = {
  title: 'Presentation foundations',
  description: 'Reusable components · generated examples · no backend operations',
  modes: ['customer_controlled', 'opentour_managed'] as const,
  tenants: ['Generated tenant A', 'Generated tenant B'] as const,
  typography: [
    { name: 'display', label: 'Display example' }, { name: 'heading', label: 'Heading example' },
    { name: 'section', label: 'Section example' }, { name: 'body', label: 'Readable body example' },
    { name: 'label', label: 'Visible label example' }, { name: 'small', label: 'Supporting text example' },
    { name: 'mono', label: 'Generated value · 0123456789' },
  ],
  buttonLabel: 'View benchmark',
  longLabel: 'Inspect this generated example with a long descriptive label that remains readable',
  disabledReason: 'This presentation example is unavailable.',
  busyReason: 'The caller is busy. No operation receipt is implied.',
  variants: ['primary', 'secondary', 'quiet'] as readonly ButtonVariant[],
  badges: [
    { tone: 'success', label: 'Confirmed example' }, { tone: 'learning', label: 'Learning example' },
    { tone: 'hold', label: 'Hold example' }, { tone: 'failure', label: 'Failed example' },
    { tone: 'unknown', label: 'Unknown example' }, { tone: 'info', label: 'Information example' },
  ] as readonly { tone: BadgeTone; label: string }[],
  tabs: [
    { key: 'overview', label: 'Overview', content: 'First generated panel. Arrow keys only move focus.' },
    { key: 'unavailable', label: 'Unavailable', content: 'Unavailable panel.', disabled: true },
    { key: 'details', label: 'Details', content: 'Second generated panel. Enter or Space selects a tab.' },
    { key: 'notes', label: 'Notes', content: 'Third generated panel. Unknown values remain unknown.' },
  ],
  invocationLabel: 'Local button events',
  busyLabel: 'Begin local example',
  resetLabel: 'Reset local example',
  selectNotesLabel: 'Select notes externally',
  disableDetailsLabel: 'Disable details example',
  removeDetailsLabel: 'Remove details example',
} as const;

/** Editable Figma140 content; these examples have no provider or protected data. */
export const overlayFixtures = {
  dialogTitle: 'Review generated details', drawerTitle: 'Generated record details',
  dialogBody: 'Review this generated example. Opening or closing it does not confirm an action or change any record.',
  drawerBody: 'This generated record is presentation content only. The drawer provides a visible title, reading order and explicit close control.',
  caption: 'Generated example · no provider or protected data',
  sections: [
    ['About this example', 'This example demonstrates long content in a controlled modal. It does not read protected information or submit a request.'],
    ['Review at your own pace', 'The title receives initial focus so the beginning stays visible. Scroll this body to continue. The close control stays reachable outside the scrolling region.'],
    ['Content remains local', 'These paragraphs are generated solely to demonstrate wrapping and reading order. The surrounding page stays inert while the overlay is open.'],
    ['Dismissal is a request', 'Escape or the close button asks the caller to dismiss. If the caller keeps open true, the modal and its focus containment remain in place.'],
    ['Return focus safely', 'After the caller closes the modal, return only to a valid target in the current context. Use the explicit safe fallback if the origin was removed, disabled or became stale.'],
    ['End of generated content', 'No confirmation, authorization, exposure or cancellation is implied by opening or closing this view.'],
  ],
} as const;
