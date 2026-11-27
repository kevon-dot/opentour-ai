import { Badge, DataState, Notice, Progress } from '../tokens';
import { requireLabel } from '../tokens/require-label';
import type { OperationStateProps } from './operation-state.types';

export type { OperationStateProps } from './operation-state.types';

type Display = OperationStateProps[keyof OperationStateProps];

function Status({ display }: { display: Display }) {
  switch (display.kind) {
    case 'progress':
      return <Progress state={display.state} title={display.status} body={display.detail} announce="off" />;
    case 'notice':
      return <Notice tone={display.tone} title={display.status} body={display.detail} />;
    case 'data':
      return <DataState state={display.state} title={display.status} body={display.detail} announce="off" />;
    case 'badge':
      return <><div style={{ display: 'grid', alignSelf: 'flex-start', maxInlineSize: '100%',
        minInlineSize: 'min(100%, calc(10 * var(--ot-type-small-size)))' }}>
        <Badge tone={display.tone} label={display.status} />
      </div>
        {display.detail !== undefined && <p className="ot-notice-body">{display.detail}</p>}</>;
  }
}

function Observation({ label, display }: { label: string; display: Display }) {
  requireLabel(display.resource, 'resource');
  requireLabel(display.observedAt, 'observation time');
  return <section aria-label={label} className="ot-notice ot-notice--info">
    <h3 className="ot-notice-title">{label}</h3>
    <p className="ot-notice-body">{display.resource}</p>
    <Status display={display} />
    <p className="ot-notice-body">Observed {display.observedAt}</p>
  </section>;
}

export function OperationState({ operation, decision, runtime }: OperationStateProps) {
  return <div className="ot-foundation" style={{ display: 'grid', gap: 'var(--ot-space-16)', minWidth: 0 }}>
    <Observation label="Operation" display={operation} />
    <Observation label="Decision" display={decision} />
    <Observation label="Runtime" display={runtime} />
  </div>;
}
