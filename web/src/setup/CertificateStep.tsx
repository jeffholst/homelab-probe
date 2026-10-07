import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import type { DraftChange, Verify } from "../api/setup";
import { useServices } from "../app/services";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { Icon } from "../components/ui/Icon";
import { ErrorBanner, FieldError, StepFrame, codeOf, messageOf, useSetup } from "./common";

const CHOICES: { value: Verify; title: string; text: string }[] = [
  {
    value: "pin",
    title: "Trust this controller's own certificate",
    text: "Recommended for a UniFi console, whose certificate is usually self-signed: only the certificate you accept here will do.",
  },
  {
    value: "true",
    title: "Its certificate is from a trusted authority",
    text: "For a controller with a certificate from a public or company authority this server already trusts.",
  },
  {
    value: "false",
    title: "Do not check the certificate",
    text: "Not recommended: the API key would go to whoever answers at that address.",
  },
];

/** Groups a fingerprint for reading aloud and comparing: "AB:CD:EF:…" with a little room every four pairs. */
function grouped(fingerprint: string): string {
  const pairs = fingerprint.split(":");
  const groups: string[] = [];
  for (let index = 0; index < pairs.length; index += 4) groups.push(pairs.slice(index, index + 4).join(":"));
  return groups.join("  ");
}

/**
 * How the controller is recognised before the API key is sent to it: a pinned certificate (fetched here, compared and
 * accepted by its fingerprint), the system's authorities, or no check at all after typing the confirmation sentence.
 * Each is an explicit request; nothing is accepted by default.
 */
export function CertificateStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const { token, status, setStatus } = useSetup();
  const { setup } = useServices();
  const draft = status.draft;
  const [choice, setChoice] = useState<Verify>(draft.verify === "true" && draft.certificate === null ? "pin" : draft.verify);
  const [compared, setCompared] = useState(draft.verify === "pin");
  const [confirm, setConfirm] = useState("");
  const [comparedMissing, setComparedMissing] = useState(false);

  const fetchCertificate = useMutation({
    mutationFn: () => setup.certificate(token),
    onSuccess: (next) => {
      setStatus(next);
      setCompared(false);
    },
  });
  const save = useMutation({
    mutationFn: (change: DraftChange) => setup.draft(token, change),
    onSuccess: (next) => {
      setConfirm("");
      setStatus(next);
      onNext();
    },
  });

  const certificate = draft.certificate;
  const confirmError = codeOf(save.error) === "confirmation_required" ? messageOf(save.error) : null;

  function submit() {
    if (choice === "pin") {
      if (certificate === null) return;
      if (!compared) {
        setComparedMissing(true);
        return;
      }
      save.mutate({ verify: "pin", fingerprint: certificate.fingerprint });
    } else if (choice === "false") {
      save.mutate({ verify: "false", confirm });
    } else {
      save.mutate({ verify: "true" });
    }
  }

  const blocked = choice === "pin" && (certificate === null || !certificate.usable);
  return (
    <StepFrame
      title="Recognise the controller"
      icon="shield"
      lede={
        <>
          Before the API key is sent to <Text value={draft.url} />, this server has to know it is talking to your controller.
        </>
      }
      onSubmit={submit}
      onBack={onBack}
      primary={{ label: "Save and continue", busy: save.isPending, disabled: blocked }}
    >
      {save.isError && confirmError === null && <ErrorBanner error={save.error} title="Could not save" />}
      <fieldset className="option-cards">
        <legend className="visually-hidden">How to recognise the controller</legend>
        {CHOICES.map((option) => (
          <div key={option.value} className="option-card">
            <label className="option-card__label">
            <input
              type="radio"
              name="verify"
              value={option.value}
              checked={choice === option.value}
              onChange={() => {
                setChoice(option.value);
                save.reset();
              }}
            />
            <span className="option-card__title">{option.title}</span>
            <span className="option-card__text">{option.text}</span>
            </label>
            {option.value === "pin" && choice === "pin" && (
              <div className="option-card__extra stack">
                {certificate === null ? (
                  <span className="row">
                    <button
                      type="button"
                      className="button button--secondary"
                      disabled={fetchCertificate.isPending}
                      onClick={() => {
                        fetchCertificate.mutate();
                      }}
                    >
                      {fetchCertificate.isPending ? <span className="spinner" aria-hidden="true" /> : <Icon name="download" />}
                      Fetch the certificate
                    </button>
                  </span>
                ) : (
                  <span className="stack stack--tight">
                    <span className="label">SHA-256 fingerprint of the certificate it shows</span>
                    <code className="fingerprint">{grouped(certificate.fingerprint)}</code>
                    {certificate.usable ? (
                      <span className="hint">
                        Compare it with the fingerprint your browser shows for the controller&apos;s page (its certificate
                        details). If they differ, somebody else may be answering at that address.
                      </span>
                    ) : (
                      <Banner tone="warning" title="This certificate cannot be pinned">
                        <p className="banner__text">
                          <Text value={certificate.problem_message} />
                        </p>
                      </Banner>
                    )}
                    {certificate.usable && (
                      <span className="check">
                        <input
                          id="setup-compared"
                          type="checkbox"
                          checked={compared}
                          aria-describedby={comparedMissing && !compared ? "setup-compared-error" : undefined}
                          onChange={(event) => {
                            setCompared(event.target.checked);
                          }}
                        />
                        <label htmlFor="setup-compared">I compared the fingerprint and it is my controller&apos;s</label>
                      </span>
                    )}
                    <FieldError
                      id="setup-compared-error"
                      message={comparedMissing && !compared ? "Compare the fingerprint first, then tick the box." : null}
                    />
                    <span className="row">
                      <button
                        type="button"
                        className="button button--ghost button--small"
                        disabled={fetchCertificate.isPending}
                        onClick={() => {
                          fetchCertificate.mutate();
                        }}
                      >
                        <Icon name="refresh" />
                        Fetch again
                      </button>
                    </span>
                  </span>
                )}
                {fetchCertificate.isError && <ErrorBanner error={fetchCertificate.error} title="Could not fetch the certificate" />}
              </div>
            )}
            {option.value === "false" && choice === "false" && (
              <div className="option-card__extra field">
                <label htmlFor="setup-confirm">
                  Type <q>{status.unverified_phrase}</q> to confirm
                </label>
                <input
                  id="setup-confirm"
                  className="input"
                  type="text"
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={confirm}
                  aria-invalid={confirmError !== null}
                  aria-describedby={confirmError !== null ? "setup-confirm-error" : undefined}
                  onChange={(event) => {
                    setConfirm(event.target.value);
                  }}
                />
                <FieldError id="setup-confirm-error" message={confirmError} />
              </div>
            )}
          </div>
        ))}
      </fieldset>
    </StepFrame>
  );
}
