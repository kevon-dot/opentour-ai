import './setup-step-indicator.css';

const steps = [
  { id: 'organization', name: 'Organization', details: 'Create or select your authorized organization.' },
  { id: 'workspace', name: 'Workspace', details: 'Continue after organization identity and Owner membership are confirmed.' },
  { id: 'project', name: 'Project', details: 'Continue after the workspace is confirmed.' },
] as const;

export type SetupStep = (typeof steps)[number]['id'];

export function SetupStepIndicator({ currentStep }: { currentStep: SetupStep }) {
  const currentIndex = steps.findIndex(step => step.id === currentStep);

  return (
    <ol className="ot-setup-steps" role="list" aria-label="Setup progress">
      {steps.map((step, index) => {
        const current = index === currentIndex;
        const status = current ? 'Current step' : index < currentIndex ? 'Done' : 'Not started';
        return (
          <li key={step.id} aria-current={current ? 'step' : undefined}>
            <span className="ot-setup-step-title">{step.name} · {status}</span>
            <span className="ot-setup-step-details">{step.details}</span>
          </li>
        );
      })}
    </ol>
  );
}
