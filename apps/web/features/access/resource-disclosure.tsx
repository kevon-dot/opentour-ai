'use client';

import { useId, useLayoutEffect, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { Button, Notice } from '../../components/foundation/tokens';
import { requireLabel } from '../../components/foundation/tokens/require-label';
import './resource-disclosure.css';

export type ResourceDisclosureProps =
  | { disclosure: 'known'; actionLabel: string; safeReason: string; authorizedContent?: ReactNode; relatedAction?: ReactNode }
  | { disclosure: 'undisclosed'; onReturnToWorkspace: () => void };

function suppressActivation(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    event.stopPropagation();
  }
}

/** The caller chooses what may be disclosed. Protected children exist only in the known branch. */
export function ResourceDisclosure(props: ResourceDisclosureProps) {
  const reasonId = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const previous = useRef(props.disclosure);
  useLayoutEffect(() => {
    if (previous.current === 'known' && props.disclosure === 'undisclosed') heading.current?.focus();
    previous.current = props.disclosure;
  }, [props.disclosure]);

  if (props.disclosure !== 'known') {
    return <div className="ot-resource-disclosure">
      <h1 ref={heading} tabIndex={-1}>Resource not found</h1>
      <Notice tone="info" title="Resource not found" body="This resource is unavailable in the current context. Return to your workspace." />
      <Button variant="secondary" label="Return to workspace" onPress={props.onReturnToWorkspace} />
    </div>;
  }

  requireLabel(props.actionLabel, 'unavailable action');
  requireLabel(props.safeReason, 'unavailable action reason');
  return <div className="ot-resource-disclosure">
    {props.authorizedContent}
    <div className="ot-disclosure-actions">
      {props.relatedAction}
      <button className="ot-button ot-unavailable-action" type="button" aria-disabled="true" aria-describedby={reasonId}
        onKeyDown={suppressActivation} onKeyUp={suppressActivation}
        onClick={event => { event.preventDefault(); event.stopPropagation(); }}>
        {props.actionLabel}
      </button>
    </div>
    <p className="ot-button-reason" id={reasonId}>{props.safeReason}</p>
  </div>;
}
