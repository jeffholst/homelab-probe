import { useState } from "react";

import type { ConnectionResult } from "../api/setup";
import { useServices } from "../app/services";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { ChecksList } from "../components/ui/ChecksList";
import { Icon } from "../components/ui/Icon";
import { ErrorBanner, StepFrame, useSetup, useSetupMutation } from "./common";

/**
 * The connection test (the controller checks of `hlp doctor`, run by the server on the draft) and, when it passes, the
 * site to watch. Choosing another site changes the draft, so the test has to run again: the server only finishes a
 * setup whose latest draft passed it.
 */
export function ConnectionStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const { token, status, setStatus } = useSetup();
  const { setup } = useServices();
  const [result, setResult] = useState<ConnectionResult | null>(null);
  const test = useSetupMutation({
    mutationFn: () => setup.connection(token),
    onSuccess: async (answer) => {
      setResult(answer);
      setStatus(await setup.status(token));
    },
  });
  const chooseSite = useSetupMutation({
    mutationFn: (site: string) => setup.draft(token, { site }),
    onSuccess: (next) => {
      setStatus(next);
    },
  });

  const draft = status.draft;
  const passed = draft.connection_ok === true;
  const warned = result?.checks.some((check) => check.status === "warn") ?? false;
  const sites = result?.sites ?? [];
  const current = sites.find((site) => site.ref === draft.site || site.id === draft.site || site.name === draft.site);

  return (
    <StepFrame
      title="Test the connection"
      icon="pulse"
      lede="The server reads the controller once with these settings: is it reachable, does it accept the key, and which data does it offer."
      onSubmit={onNext}
      onBack={onBack}
      primary={{ label: "Continue", disabled: !passed }}
      extra={
        <button
          type="button"
          className={passed ? "button button--secondary" : "button"}
          disabled={test.isPending || chooseSite.isPending}
          onClick={() => {
            test.mutate();
          }}
        >
          {test.isPending ? <span className="spinner" aria-hidden="true" /> : <Icon name={result === null ? "pulse" : "refresh"} />}
          {test.isPending ? "Testing…" : result === null && !passed ? "Test the connection" : "Test again"}
        </button>
      }
    >
      <div aria-live="polite" className="stack">
        {test.isError && <ErrorBanner error={test.error} title="The test could not run" />}
        {result !== null && !test.isPending && (
          <Banner
            tone={!result.ok ? "danger" : warned ? "warning" : "success"}
            title={!result.ok ? "The controller could not be used" : warned ? "Connected, with warnings" : "Connected"}
          >
            <p className="banner__text">
              {!result.ok
                ? "See what failed below, change the settings and test again."
                : warned
                  ? "The controller answered and accepted the key, but some checks need a look (below). You can continue; the data they cover may be incomplete."
                  : "The controller answered and accepted the key."}
            </p>
          </Banner>
        )}
        {result === null && passed && (
          <Banner tone="success" title="These settings passed the test">
            <p className="banner__text">You can continue, or test again.</p>
          </Banner>
        )}
        {result !== null && draft.connection_ok === null && result.ok && (
          <Banner tone="info" title="The site changed">
            <p className="banner__text">Test again with the new site before you continue.</p>
          </Banner>
        )}
      </div>
      {result !== null && <ChecksList checks={result.checks} label="What was checked" />}
      {sites.length > 0 && (
        <fieldset className="option-cards option-cards--row">
          <legend className="label">Site to watch</legend>
          {sites.map((site) => (
            <div key={site.id || site.ref} className="option-card">
              <label className="option-card__label">
                <input
                  type="radio"
                  name="site"
                  value={site.ref}
                  checked={current === site}
                  disabled={chooseSite.isPending}
                  onChange={() => {
                    chooseSite.mutate(site.ref);
                  }}
                />
                <span className="option-card__title">
                  <Text value={site.name} fallback={site.ref} />
                </span>
                <span className="option-card__text">
                  Reference <Text value={site.ref} />
                </span>
              </label>
            </div>
          ))}
        </fieldset>
      )}
      {chooseSite.isError && <ErrorBanner error={chooseSite.error} title="Could not choose that site" />}
    </StepFrame>
  );
}
