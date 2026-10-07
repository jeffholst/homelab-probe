import { useMutation, useQueryClient, type UseMutationOptions } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { isApiError } from "../api/errors";
import type { SetupStatus } from "../api/setup";
import { useMeta } from "../app/meta";
import { endSession } from "../app/services";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { Icon, type IconName } from "../components/ui/Icon";

/** What every step of the setup shares: the token (memory only; null when an administrator's session is used) and the
 * last status the server gave. */
export interface SetupContextValue {
  token: string | null;
  status: SetupStatus;
  setStatus: (status: SetupStatus) => void;
  /** Called with every failed setup call: a token that stopped working (a restart makes a new one) or a session that
   * ended sends the page back to the token or the login, instead of failing every step that follows. */
  lost: (error: unknown) => void;
}

export const SetupContext = createContext<SetupContextValue | null>(null);

export function useSetup(): SetupContextValue {
  const value = useContext(SetupContext);
  if (value === null) throw new Error("useSetup needs the SetupContext");
  return value;
}

/** `useMutation` for a setup call: its failure also goes to `lost`. Never retried: a finish or a restore that may have
 * happened must not be sent twice. */
export function useSetupMutation<T, V = void>(options: UseMutationOptions<T, unknown, V>) {
  const { lost } = useSetup();
  return useMutation<T, unknown, V>({
    ...options,
    retry: false,
    onError: (...args) => {
      lost(args[0]);
      return options.onError?.(...args);
    },
  });
}

/** An answer that does not say whether the server did it: no answer at all, a proxy's error, a timeout. */
export function isUncertain(error: unknown): boolean {
  return !isApiError(error) || error.status === 0 || error.status === 502 || error.status === 504 || error.code === "bad_response";
}

/**
 * What to show when a finish or a restore got no clear answer: it may have happened. The page does not try again by
 * itself; it offers to ask the server whether it is still waiting for its setup (the public `meta`), and says what
 * that answer means.
 */
export function UncertainOutcome({ what }: { what: "setup" | "restore" }) {
  const meta = useMeta();
  const queryClient = useQueryClient();
  const [answer, setAnswer] = useState<"set_up" | "waiting" | "unknown" | null>(null);
  async function check() {
    const result = await meta.refetch();
    if (result.data === undefined) setAnswer("unknown");
    else if (result.data.needs_setup) setAnswer("waiting");
    else {
      endSession(queryClient);
      setAnswer("set_up");
    }
  }
  return (
    <Banner tone="warning" role="alert" title={`It is not known whether the ${what} finished`}>
      <p className="banner__text">
        The server&apos;s answer did not arrive. Do not try again before checking: the {what} may have finished.
      </p>
      {answer === "set_up" && (
        <p className="banner__text">
          The server is set up now, so the {what} most likely finished. <Link to="/login">Go to the login</Link>.
        </p>
      )}
      {answer === "waiting" && <p className="banner__text">The server is still waiting for its setup: nothing shows that the {what} finished. You can try again.</p>}
      {answer === "unknown" && <p className="banner__text">The server did not answer. Check that it is running, then check again.</p>}
      <button type="button" className="button button--secondary button--small" onClick={() => void check()}>
        Check the server
      </button>
    </Banner>
  );
}

/** A warning that what is typed here (the token, the API key, passwords) would cross the network unencrypted. */
export function InsecureNotice() {
  const meta = useMeta();
  if (meta.data === undefined || meta.data.https || meta.data.loopback) return null;
  return (
    <Banner tone="warning" title="This connection is not encrypted">
      <p className="banner__text">
        The setup token, the API key and the passwords you type here would travel in clear text. Use an SSH tunnel or an HTTPS reverse proxy to
        reach this server.
      </p>
    </Banner>
  );
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
