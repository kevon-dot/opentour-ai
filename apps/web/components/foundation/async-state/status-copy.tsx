import { Notice } from '../tokens/notice';
import { Toast } from '../tokens/feedback';
import { statusFixtures, toastVariants, type StatusCopyProps } from './status-copy.types';

/**
 * Keep a StatusCopy mounted in the same caller-owned slot across updates. React
 * reconciles the existing live region and only mutates changed text nodes.
 * Removing the slot is the caller's decision, including for supplementary toasts.
 */
export function StatusCopy(props: StatusCopyProps) {
  if (!Object.hasOwn(statusFixtures, props.fixture)) throw new Error('Unknown status fixture.');
  const { tone, title, body } = statusFixtures[props.fixture];
  if (props.presentation === 'notice' && props.urgent === undefined) {
    return <Notice tone={tone} title={title} body={body} />;
  }
  if (props.presentation !== 'toast' || !Object.hasOwn(toastVariants, props.fixture)) {
    throw new Error('Unsupported status presentation.');
  }
  return <Toast state={toastVariants[props.fixture]} title={title} body={body}
    announce={props.urgent ? 'assertive' : 'polite'} />;
}
