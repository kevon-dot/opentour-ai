import type { DerivedValueProps } from './derived-value.types';

/** Generated presentation examples; the first three display strings are the native generic specimen. */
export const derivedValueFixtures = {
  modes: ['customer-controlled', 'opentour-managed'] as const,
  widths: [320, 768, 1280, 1440],
  first: { label: 'Field', value: 'Input value', helpText: 'Supporting information', copyLabel: 'Copy reference',
    copyFeedback: { success: 'Copied', failure: 'Could not copy. Select the value and copy it manually.' } } satisfies DerivedValueProps,
  long: { label: 'Generated reference with a long contextual name that remains readable',
    value: '  0007α\n<exact & generated>\n' + 'generated-reference-'.repeat(14) + '  ',
    helpText: 'Generated current-context text. Select this exact value to copy it manually if the clipboard is unavailable.',
    copyLabel: 'Copy the long generated reference',
    copyFeedback: { success: 'Copied', failure: 'Could not copy. Select the value and copy it manually.' } } satisfies DerivedValueProps,
};

export interface DerivedFixtureControls {
  replace: (props: Partial<DerivedValueProps>) => void;
  rerender: () => void;
  show: (visible: boolean) => void;
}
export interface ClipboardFixtureControls {
  writes: string[];
  configure: (kind: 'success' | 'reject' | 'throw' | 'absent' | 'deferred') => void;
  settle: (index: number, success: boolean) => void;
}
declare global {
  interface Window {
    derivedFixture: DerivedFixtureControls;
    clipboardFixture: ClipboardFixtureControls;
    derivedObservation: { mutations: number; updates: string[]; button: Element; value: Element };
  }
}
