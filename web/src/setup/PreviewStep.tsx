import { useMutation } from "@tanstack/react-query";

import { useServices } from "../app/services";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { Icon } from "../components/ui/Icon";
import { ErrorBanner, StepFrame, useSetup } from "./common";

const LABELS: Record<string, string> = { critical: "Critical", warning: "Warning", info: "Info" };

/**
 * Optional: a first health check of the network with these settings (`diagnose` without the event log, run by the
 * server). It changes nothing; it shows what the dashboard will show, so a wrong site or key is noticed now.
 */
export function PreviewStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const { token } = useSetup();
  const { setup } = useServices();
  const run = useMutation({ mutationFn: () => setup.preview(token) });
  const result = run.data;

  return (
    <StepFrame
      title="A first health check"
      icon="sparkle"
      lede="Optional. Run the health checks once to see what Homelab Probe finds on your network. Nothing on the controller changes."
      onSubmit={onNext}
      onBack={onBack}
      primary={{ label: result === undefined ? "Skip" : "Continue" }}
      extra={
        <button
          type="button"
          className={result === undefined ? "button" : "button button--secondary"}
          disabled={run.isPending}
          onClick={() => {
            run.mutate();
          }}
        >
          {run.isPending ? <span className="spinner" aria-hidden="true" /> : <Icon name={result === undefined ? "pulse" : "refresh"} />}
          {run.isPending ? "Checking…" : result === undefined ? "Run the checks" : "Run again"}
        </button>
      }
    >
      <div aria-live="polite">
        {run.isError && <ErrorBanner error={run.error} title="The checks could not run" />}
        {result !== undefined && !run.isPending && (
          <div className="stack">
            <ul className="counts" aria-label="What was found">
              {(["critical", "warning", "info"] as const).map((severity) => (
                <li key={severity} className={`count count--${severity}`}>
                  <span className="count__value">{result[severity]}</span>
                  <span className="count__label">{LABELS[severity]}</span>
                </li>
              ))}
            </ul>
            {result.total === 0 && (
              <Banner tone="success" title="Nothing to report">
                <p className="banner__text">Every check that ran found the network healthy.</p>
              </Banner>
            )}
            {result.warnings.length > 0 && (
              <Banner tone="warning" title="Some data could not be read">
                <ul>
                  {result.warnings.map((warning, index) => (
                    <li key={index}>
                      <Text value={warning} />
                    </li>
                  ))}
                </ul>
              </Banner>
            )}
            {result.findings.length > 0 && (
              <ul className="checklist" aria-label="Findings">
                {result.findings.map((finding, index) => (
                  <li key={index} className="checklist__item">
                    <span className={finding.severity === "critical" ? "status-fail" : finding.severity === "warning" ? "status-warn" : "status-info"}>
                      <Icon name={finding.severity === "critical" ? "xCircle" : finding.severity === "warning" ? "alert" : "info"} />
                    </span>
                    <span className="checklist__title">
                      <span className="pill">{LABELS[finding.severity] ?? <Text value={finding.severity} />}</span> <Text value={finding.subject} />
                    </span>
                    <p>
                      <Text value={finding.message} />
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {result.total > result.findings.length && (
              <p className="muted small">
                The first {result.findings.length} of {result.total} findings. Run <code>hlp diagnose</code> for all of them.
              </p>
            )}
          </div>
        )}
      </div>
    </StepFrame>
  );
}
