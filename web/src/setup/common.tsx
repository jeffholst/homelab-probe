import { createContext, useContext, useEffect, useRef, type FormEvent, type ReactNode } from "react";

import { isApiError } from "../api/errors";
import type { SetupStatus } from "../api/setup";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { Icon, type IconName } from "../components/ui/Icon";

/** What every step of the setup shares: the token (memory only; null when an administrator's session is used) and the
 * last status the server gave. */
export interface SetupContextValue {
  token: string | null;
  status: SetupStatus;
  setStatus: (status: SetupStatus) => void;
}

export const SetupContext = createContext<SetupContextValue | null>(null);

export function useSetup(): SetupContextValue {
  const value = useContext(SetupContext);
  if (value === null) throw new Error("useSetup needs the SetupContext");
  return value;
}

/** The server's fixed sentence for a failed call, or a generic one for anything else. */
export function messageOf(error: unknown): string {
  return isApiError(error) ? error.message : "Something went wrong.";
}

/** The setting a 422 `invalid_setting` (and its relatives) is about, if the server named one. */
export function settingOf(error: unknown): string | null {
  if (!isApiError(error)) return null;
  const setting = error.details["setting"];
  return typeof setting === "string" ? setting : null;
}

export function codeOf(error: unknown): string | null {
  return isApiError(error) ? error.code : null;
}

/** A failed call as a banner: the server's sentence, read out at once. */
export function ErrorBanner({ error, title = "That did not work" }: { error: unknown; title?: string }) {
  return (
    <Banner tone="danger" role="alert" title={title}>
      <p className="banner__text">
        <Text value={messageOf(error)} />
      </p>
    </Banner>
  );
}

/** The message under a field whose value the server refused; `id` is what the field's `aria-describedby` names. */
export function FieldError({ id, message }: { id: string; message: string | null }) {
  if (message === null) return null;
  return (
    <p id={id} className="field-error">
      <Icon name="alert" />
      <Text value={message} />
    </p>
  );
}

interface StepFrameProps {
  title: string;
  icon: IconName;
  lede?: ReactNode;
  children: ReactNode;
  onSubmit?: () => void;
  /** The main button; without it the step has none (it acts through its own buttons). */
  primary?: { label: string; busy?: boolean; disabled?: boolean; brand?: boolean };
  onBack?: (() => void) | undefined;
  /** Buttons beside the main one (Skip). */
  extra?: ReactNode;
}

/**
 * One step of a guided flow: a card with the step's heading (which takes focus when the step appears, so a keyboard or
 * screen reader user starts at its top), what it is for, its fields, and Back and the main action at the bottom. It is
 * a form, so Enter submits it.
 */
export function StepFrame({ title, icon, lede, children, onSubmit, primary, onBack, extra }: StepFrameProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  function submit(event: FormEvent) {
    event.preventDefault();
    if (primary?.busy === true || primary?.disabled === true) return;
    onSubmit?.();
  }
  return (
    <form className="card card--glass" onSubmit={submit} noValidate>
      <div className="step__head">
        <span className="tile-icon">
          <Icon name={icon} />
        </span>
        <h2 ref={heading} tabIndex={-1}>
          {title}
        </h2>
      </div>
      {lede !== undefined && <p className="step__lede">{lede}</p>}
      {children}
      {(onBack !== undefined || primary !== undefined || extra !== undefined) && (
        <div className="step__actions">
          {onBack !== undefined && (
            <button type="button" className="button button--ghost" onClick={onBack}>
              <Icon name="arrowLeft" />
              Back
            </button>
          )}
          <div className="step__actions-end">
            {extra}
            {primary !== undefined && (
              <button
                type="submit"
                className={primary.brand === true ? "button button--brand" : "button"}
                disabled={primary.busy === true || primary.disabled === true}
              >
                {primary.busy === true && <span className="spinner" aria-hidden="true" />}
                {primary.label}
                {primary.busy !== true && <Icon name="arrowRight" />}
              </button>
            )}
          </div>
        </div>
      )}
    </form>
  );
}
