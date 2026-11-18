'use client';

import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Button } from '../tokens';
import { requireLabel } from '../tokens/require-label';
import type { DerivedValueProps } from './derived-value.types';
import './derived-value.css';

export type { DerivedValueProps } from './derived-value.types';

/** Selectable presentation with an explicit local copy action; never a submitted field. */
export function DerivedValue({ label, value, helpText, copyLabel, copyFeedback }: DerivedValueProps) {
  requireLabel(label);
  requireLabel(helpText, 'help text');
  requireLabel(copyLabel, 'copy label');
  requireLabel(copyFeedback?.success, 'copy success feedback');
  requireLabel(copyFeedback?.failure, 'copy failure feedback');
  if (typeof value !== 'string') throw new Error('A derived display value must be a string.');
  const id = useId();
  const { success, failure } = copyFeedback;
  // A replacement of any contextual text starts a distinct presentation, even when a prior write is unresolved.
  const identity = useMemo(() => ({}), [label, value, helpText, copyLabel, success, failure]);
  const mounted = useRef<object | null>(null);
  const active = useRef<{ identity: object } | null>(null);
  const [outcome, setOutcome] = useState<{ identity: object; phase: 'pending' | 'success' | 'failure' } | null>(null);
  const phase = outcome?.identity === identity ? outcome.phase : null;
  useLayoutEffect(() => {
    mounted.current = identity;
    return () => { mounted.current = null; active.current = null; };
  }, [identity]);

  function copy() {
    if (active.current?.identity === identity) return;
    const request = { identity };
    active.current = request;
    setOutcome({ identity, phase: 'pending' });
    const settle = (result: 'success' | 'failure') => {
      if (mounted.current !== identity || active.current !== request) return;
      active.current = null;
      setOutcome({ identity, phase: result });
    };
    try {
      // Keep the write in the activation call stack. No clipboard read, fallback copy or automatic retry.
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') { settle('failure'); return; }
      Promise.resolve(navigator.clipboard.writeText(value)).then(() => settle('success'), () => settle('failure'));
    } catch { settle('failure'); }
  }

  return <section className="ot-derived-value" aria-labelledby={`${id}-label`}>
    <div className="ot-derived-display">
      <span id={`${id}-label`} className="ot-derived-label">{label}</span>
      <span id={`${id}-value`} className="ot-derived-text">{value}</span>
      <span id={`${id}-help`} className="ot-derived-help">{helpText}</span>
    </div>
    <div className="ot-derived-copy" role="group" aria-labelledby={`${id}-label`} aria-describedby={`${id}-value ${id}-help`}>
      <Button label={copyLabel} variant="secondary" busy={phase === 'pending'} onPress={copy} />
    </div>
    <p role="status" className="ot-derived-feedback">{phase === 'success' ? success : phase === 'failure' ? failure : ''}</p>
  </section>;
}
