import type { DataStateProps, ProgressProps, ToastProps } from './feedback.contracts';

/** Generated presentation examples only. No API response, grant, or receipt is simulated. */
export const feedbackFixtures = {
  dataStates: [
    { state: 'loading', title: 'Loading data example', body: 'Generated data state text. Unknown values are not shown as zero.' },
    { state: 'empty', title: 'Empty data example', body: 'Generated data state text for an example with no records.' },
    { state: 'partial', title: 'Partial data example', body: 'Generated data state text. The example remains incomplete.' },
    { state: 'error', title: 'Error data example', body: 'Generated data state text for an example that could not be shown.' },
    { state: 'denied', title: 'Denied data example', body: 'Generated data state text for an example without access.' },
    { state: 'stale', title: 'Stale data example', body: 'Generated data state text. The example is dated and says so.' },
    { state: 'offline', title: 'Offline data example', body: 'Generated data state text for an example that cannot be reached.' },
    { state: 'revoked', title: 'Revoked data example', body: 'Generated data state text for an example whose access has ended.' },
    { state: 'unknown-outcome', title: 'Unknown outcome data example', body: 'Generated data state text. An unknown outcome remains unknown.' },
  ] as readonly DataStateProps[],
  progress: [
    { state: 'queued', title: 'Queued progress example', body: 'Generated progress text. A queued example has not started.' },
    { state: 'running', title: 'Running progress example', body: 'Generated progress text. No completion is implied.' },
    { state: 'stopping', title: 'Stopping progress example', body: 'Generated progress text. A stopping example has not stopped.' },
    { state: 'complete', title: 'Complete progress example', body: 'Generated progress text for a completed presentation example.' },
    { state: 'failed', title: 'Failed progress example', body: 'Generated progress text for a failed presentation example.' },
  ] as readonly ProgressProps[],
  toasts: [
    { state: 'accepted', title: 'Accepted toast example', body: 'Generated toast text. Accepted is not confirmed.' },
    { state: 'confirmed', title: 'Confirmed toast example', body: 'Generated toast text for a confirmed presentation example.' },
    { state: 'failed', title: 'Failed toast example', body: 'Generated toast text for a failed presentation example.' },
    { state: 'unknown', title: 'Unknown toast example', body: 'Generated toast text. An unknown outcome remains unknown.' },
  ] as readonly ToastProps[],
  longTitle: 'Generated feedback title with a long descriptive state name that wraps instead of clipping',
  longBody: 'Generated feedback body with an unbroken token GENERATED-0123456789-0123456789-0123456789-0123456789 and enough words to span several lines at a narrow width.',
} as const;
