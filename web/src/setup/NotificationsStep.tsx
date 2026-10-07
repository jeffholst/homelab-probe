import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { type NOTIFY_SETTINGS, type Check } from "../api/setup";
import { useServices } from "../app/services";
import { Banner } from "../components/DataStates";
import { ChecksList } from "../components/ui/ChecksList";
import { Icon } from "../components/ui/Icon";
import { SecretInput } from "../components/ui/SecretInput";
import { ErrorBanner, StepFrame, useSetup } from "./common";

type Kind = "none" | "ntfy" | "webhook" | "email";

interface FieldSpec {
  name: (typeof NOTIFY_SETTINGS)[number];
  label: string;
  hint?: string;
  secret?: boolean;
  type?: "text" | "url" | "email" | "number";
  required?: boolean;
  choices?: readonly string[];
}

const KINDS: { value: Kind; title: string; text: string; fields: FieldSpec[] }[] = [
  { value: "none", title: "No notifications", text: "Health checks are shown here and on the command line only.", fields: [] },
  {
    value: "ntfy",
    title: "ntfy",
    text: "A push notification to your phone through an ntfy topic.",
    fields: [
      { name: "NOTIFY_NTFY_URL", label: "Topic address", hint: "Such as https://ntfy.sh/my-homelab-alerts", type: "url", required: true },
      { name: "NOTIFY_NTFY_TOKEN", label: "Access token", hint: "Only for a protected topic.", secret: true },
    ],
  },
  {
    value: "webhook",
    title: "Webhook",
    text: "A JSON message to an HTTPS address of your own (a home automation, a chat bridge).",
    fields: [
      { name: "NOTIFY_WEBHOOK_URL", label: "Webhook address", type: "url", required: true },
      { name: "NOTIFY_WEBHOOK_TOKEN", label: "Bearer token", hint: "Sent in the Authorization header, if your receiver wants one.", secret: true },
    ],
  },
  {
    value: "email",
    title: "Email",
    text: "One plain-text message per run, through your mail provider (with an app password).",
    fields: [
      { name: "NOTIFY_SMTP_HOST", label: "Mail server", hint: "Such as smtp.example.com", required: true },
      { name: "NOTIFY_SMTP_PORT", label: "Port", hint: "587 for STARTTLS, 465 for SSL; empty for the usual one.", type: "number" },
      { name: "NOTIFY_SMTP_SECURITY", label: "Encryption", choices: ["starttls", "ssl"] },
      { name: "NOTIFY_SMTP_USER", label: "User name" },
      { name: "NOTIFY_SMTP_PASSWORD", label: "Password or app password", secret: true },
      { name: "NOTIFY_EMAIL_FROM", label: "From", type: "email", required: true },
      { name: "NOTIFY_EMAIL_TO", label: "To", hint: "One address, or several separated by commas.", required: true },
    ],
  },
];

function kindOf(names: readonly string[]): Kind {
  if (names.some((name) => name.startsWith("NOTIFY_NTFY"))) return "ntfy";
  if (names.some((name) => name.startsWith("NOTIFY_WEBHOOK"))) return "webhook";
  if (names.some((name) => name.startsWith("NOTIFY_SMTP") || name.startsWith("NOTIFY_EMAIL"))) return "email";
  return "none";
}

/**
 * Optional: where the health checks send what changed (the destinations the owner approved: ntfy, a webhook, email).
 * The values are secrets: they are sent to the server once, the fields are emptied, and from then on only their names
 * are known here. The dry run says what would be sent and sends nothing.
 */
export function NotificationsStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const { token, status, setStatus } = useSetup();
  const { setup } = useServices();
  const saved = status.draft.notify;
  const [kind, setKind] = useState<Kind>(kindOf(saved));
  const [values, setValues] = useState<Record<string, string>>({});
  const [checks, setChecks] = useState<Check[] | null>(null);

  const save = useMutation({
    mutationFn: (notify: Record<string, string | null>) => setup.draft(token, { notify }),
    onSuccess: (next) => {
      setValues({});
      setChecks(null);
      setStatus(next);
    },
  });
  const dryRun = useMutation({
    mutationFn: () => setup.notifications(token),
    onSuccess: setChecks,
  });

  const spec = KINDS.find((entry) => entry.value === kind) ?? KINDS[0];
  const fields = spec?.fields ?? [];
  const savedKind = kindOf(saved);

  function change(): Record<string, string | null> {
    const notify: Record<string, string | null> = {};
    for (const name of saved) if (!fields.some((field) => field.name === name)) notify[name] = null;
    for (const field of fields) {
      const value = (values[field.name] ?? "").trim();
      if (value !== "") notify[field.name] = value;
    }
    return notify;
  }

  const delta = change();
  const pending = Object.keys(delta).length > 0;
  const nextLabel = kind !== "none" && kind !== savedKind ? "Skip" : "Continue";
  return (
    <StepFrame
      title="Notifications"
      icon="bell"
      lede="Optional. Get told when a health check finds something new, or when it is fixed. You can change this later in the settings."
      onSubmit={() => {
        if (pending) save.mutate(delta);
        else onNext();
      }}
      onBack={onBack}
      primary={{ label: pending ? "Save" : nextLabel, busy: save.isPending }}
      extra={
        !pending &&
        saved.length > 0 && (
          <button
            type="button"
            className="button button--secondary"
            disabled={dryRun.isPending}
            onClick={() => {
              dryRun.mutate();
            }}
          >
            {dryRun.isPending ? <span className="spinner" aria-hidden="true" /> : <Icon name="pulse" />}
            Check without sending
          </button>
        )
      }
    >
      {save.isError && <ErrorBanner error={save.error} title="Could not save the notifications" />}
      {save.isSuccess && !pending && (
        <Banner tone="success" title="Saved on the server">
          <p className="banner__text">
            {saved.length === 0 ? "No notifications will be sent." : "Check them without sending anything, or continue."}
          </p>
        </Banner>
      )}
      <fieldset className="option-cards option-cards--row">
        <legend className="visually-hidden">Destination</legend>
        {KINDS.map((entry) => (
          <div key={entry.value} className="option-card">
            <label className="option-card__label">
              <input
                type="radio"
                name="notify-kind"
                value={entry.value}
                checked={kind === entry.value}
                onChange={() => {
                  setKind(entry.value);
                  setValues({});
                  save.reset();
                }}
              />
              <span className="option-card__title">{entry.title}</span>
              <span className="option-card__text">{entry.text}</span>
            </label>
          </div>
        ))}
      </fieldset>
      {fields.length > 0 && (
        <div className="stack">
          {fields.map((field) => {
            const id = `notify-${field.name}`;
            const isSet = saved.includes(field.name);
            const hint = [field.hint, isSet ? "Saved on the server: leave empty to keep it." : undefined].filter(Boolean).join(" ");
            const common = {
              id,
              value: values[field.name] ?? "",
              "aria-describedby": hint === "" ? undefined : `${id}-hint`,
              onChange: (event: { target: { value: string } }) => {
                setValues((current) => ({ ...current, [field.name]: event.target.value }));
              },
            };
            return (
              <div key={field.name} className="field">
                <label htmlFor={id}>
                  {field.label}
                  {field.required !== true && <span className="muted"> (optional)</span>}
                </label>
                {hint !== "" && (
                  <p id={`${id}-hint`} className="hint">
                    {hint}
                  </p>
                )}
                {field.choices !== undefined ? (
                  <select {...common} className="input">
                    <option value="">{isSet ? "Keep the saved choice" : "starttls (the usual)"}</option>
                    {field.choices.map((choice) => (
                      <option key={choice} value={choice}>
                        {choice}
                      </option>
                    ))}
                  </select>
                ) : field.secret === true ? (
                  <SecretInput {...common} autoComplete="off" />
                ) : (
                  <input {...common} className="input" type={field.type ?? "text"} autoComplete="off" autoCapitalize="none" spellCheck={false} />
                )}
              </div>
            );
          })}
        </div>
      )}
      {dryRun.isError && <ErrorBanner error={dryRun.error} title="The check could not run" />}
      {checks !== null && <ChecksList checks={checks} label="Notification check" />}
    </StepFrame>
  );
}
