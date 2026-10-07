/**
 * Where a multi-step flow is: a bar per step (done, current, to come) with its name from 40rem up, and one written
 * line ("Step 2 of 6: Security") below that. The current step is marked with `aria-current="step"`, and every step's
 * state is written out for a screen reader, so the colour is never the only cue.
 */
export function Stepper({ steps, current, label }: { steps: readonly string[]; current: number; label: string }) {
  return (
    <>
      <ol className="stepper" aria-label={label}>
        {steps.map((step, index) => {
          const state = index < current ? "done" : index === current ? "current" : "upcoming";
          return (
            <li key={step} className="stepper__step" data-state={state} aria-current={state === "current" ? "step" : undefined}>
              <span className="stepper__marker" aria-hidden="true" />
              <span className="stepper__label">
                {step}
                <span className="visually-hidden">{state === "done" ? " (done)" : state === "current" ? " (current step)" : ""}</span>
              </span>
            </li>
          );
        })}
      </ol>
      <p className="stepper__caption" aria-hidden="true">
        Step {current + 1} of {steps.length}: {steps[current]}
      </p>
    </>
  );
}
