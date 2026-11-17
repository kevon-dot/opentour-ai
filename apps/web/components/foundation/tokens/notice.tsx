import type { NoticeProps, NoticeTone } from './contracts';
import { requireLabel } from './require-label';
import '../../../styles/product-tokens.css';

/** Declared tones in the Figma variant order of component set 12:17 (State=Failed is `failure`). */
export const noticeTones: readonly NoticeTone[] = ['info', 'success', 'hold', 'failure', 'unknown'];

/**
 * Persistent in-flow status text. `role="status"` is silent when the page is first read and
 * announces a later change of title or body politely and as a whole; it is not an alert or toast.
 */
export function Notice({ tone, title, body }: NoticeProps) {
  if (!noticeTones.includes(tone)) throw new Error('A declared notice tone is required.');
  requireLabel(title, 'notice title');
  requireLabel(body, 'notice body');
  return <div role="status" className={`ot-notice ot-notice--${tone}`}>
    <p className="ot-notice-title">{title}</p>
    <p className="ot-notice-body">{body}</p>
  </div>;
}
