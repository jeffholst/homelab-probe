import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { RestoreResult } from "../api/backup";
import { isApiError } from "../api/errors";
import type { SetupStatus } from "../api/setup";
import type { Meta, Session } from "../api/types";
import { useMeta } from "../app/meta";
import { SESSION_KEY, endSession, useServices } from "../app/services";
import { Banner, ErrorState, Loading } from "../components/DataStates";
import { Text } from "../components/Text";
import { Icon, type IconName } from "../components/ui/Icon";
import { SecretInput } from "../components/ui/SecretInput";
import { Stepper } from "../components/ui/Stepper";
import { AuthLayout } from "../layouts/AuthLayout";
import { useCountdown } from "../lib/useCountdown";
import { usePageTitle } from "../lib/usePageTitle";
import { CertificateStep } from "./CertificateStep";
import { InsecureNotice, SetupContext, messageOf, useSetup } from "./common";
import { ConnectionStep } from "./ConnectionStep";
import { ControllerStep } from "./ControllerStep";
import { FinishStep } from "./FinishStep";
import { NotificationsStep } from "./NotificationsStep";
import { PreviewStep } from "./PreviewStep";
import { RestoreFlow } from "./RestoreFlow";

/** How this page may use the setup routes: with the token, with an administrator's session, or not yet. */
type Access =
  | { kind: "checking" }
  | { kind: "token" }
  | { kind: "login" }
  | { kind: "failed"; error: unknown }
  | { kind: "ready"; token: string | null; status: SetupStatus };

type Done = { kind: "configured"; adminCreated: boolean } | { kind: "restored"; result: RestoreResult };

const CONFIGURE_STEPS = ["Controller", "Security", "Connection", "Notifications", "Health check", "Administrator"] as const;

/**
 * The first-run setup of a server with no settings (or no administrator), and the restore of a backup onto a fresh
 * installation. The server decides who may do it (the setup token while no administrator exists, an administrator's
 * session after that) and keeps everything typed in its own memory; this page holds the token in its state only, so
 * a reload asks for it again, and nothing is written to the browser's storage.
 */
export function SetupPage() {
  usePageTitle("Set up");
  const meta = useMeta();
  const { setup } = useServices();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [decided, setAccess] = useState<Access | null>(null);
  const [path, setPath] = useState<"choose" | "configure" | "restore">("choose");
  const [done, setDone] = useState<Done | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const needsSetup = meta.data?.needs_setup === true;
  const [attempt, setAttempt] = useState(0);
  // Only an administrator who logged in on this page can use the setup without the token. Anybody else is asked for
  // it first: a request without it would count as a failed attempt on the server, which slows down wrong tokens.
  const administrator = queryClient.getQueryData<Session | null>(SESSION_KEY)?.role === "admin";
  const access: Access = decided ?? (administrator ? { kind: "checking" } : { kind: "token" });

  useEffect(() => {
    if (!needsSetup || !administrator) return;
    setup.status(null).then(
      (status) => {
        setAccess({ kind: "ready", token: null, status });
      },
      (error: unknown) => {
        setAccess(isApiError(error) && error.status === 401 ? { kind: "token" } : { kind: "failed", error });
      },
    );
  }, [needsSetup, administrator, setup, attempt]);

  function finish(result: Done) {
    setDone(result);
    // Every session ended (a restore) or none existed: the next page is the login, with the server's new state.
    endSession(queryClient);
    queryClient.setQueryData<Meta>(["meta"], (meta) => meta && { ...meta, needs_setup: false, setup_mode: null });
    void queryClient.invalidateQueries({ queryKey: ["meta"] });
  }

  if (done !== null) return <DoneScreen done={done} onLogin={() => void navigate("/login")} />;

  if (meta.isPending) {
    return (
      <AuthLayout>
        <Loading label="the server's details" />
      </AuthLayout>
    );
  }
  if (meta.isError) {
    return (
      <AuthLayout>
        <ErrorState error={meta.error} label="the server's details" onRetry={() => void meta.refetch()} />
      </AuthLayout>
    );
  }
  // An open flow stays on screen when the server's state changes under it (a finish or a restore whose answer was lost
  // asks the server itself, and says what the answer means).
  if (!needsSetup && access.kind !== "ready") {
    return (
      <AuthLayout>
        <div className="auth__intro">
          <h1>This server is set up</h1>
          <p>There is nothing left to set up here. Log in to use it.</p>
        </div>
        <p className="center">
          <Link className="button button--brand" to="/login">
            Go to the login
          </Link>
        </p>
      </AuthLayout>
    );
  }

  if (access.kind === "checking") {
    return (
      <AuthLayout>
        <Loading label="the setup" />
      </AuthLayout>
    );
  }
  if (access.kind === "failed") {
    return (
      <AuthLayout>
        <ErrorState
          error={access.error}
          label="the setup"
          onRetry={() => {
            setAccess(null);
            setAttempt((value) => value + 1);
          }}
        />
      </AuthLayout>
    );
  }
  if (access.kind === "login") {
    return (
      <AuthLayout>
        <div className="auth__intro">
          <h1>Log in to continue the setup</h1>
          <p>This server has an administrator already, so the setup needs that account rather than the setup token.</p>
        </div>
        <p className="center">
          <Link className="button button--brand" to="/login?next=%2Fsetup">
            Log in
          </Link>
        </p>
      </AuthLayout>
    );
  }
  if (access.kind === "token") {
    return (
      <TokenScreen
        notice={notice}
        onAccepted={(token, status) => {
          setNotice(null);
          setAccess({ kind: "ready", token, status });
        }}
        onLoginNeeded={() => {
          setAccess({ kind: "login" });
        }}
      />
    );
  }

  const { token, status } = access;
  const context = {
    token,
    status,
    setStatus: (next: SetupStatus) => {
      setAccess({ kind: "ready", token, status: next });
    },
    lost: (error: unknown) => {
      if (!isApiError(error)) return;
      if (error.code === "invalid_setup_token") {
        setNotice("The setup token is no longer accepted: the server may have restarted with a new one. Enter the token it shows now.");
        setPath("choose");
        setAccess({ kind: "token" });
      } else if (error.code === "not_logged_in") {
        setPath("choose");
        setAccess({ kind: "login" });
      } else if (error.code === "already_set_up" || error.code === "not_configured" || error.code === "step_unavailable") {
        void meta.refetch();
      }
    },
  };
  return (
    <SetupContext.Provider value={context}>
      {path === "choose" && (
        <ChooseScreen
          mode={status.mode}
          onChoose={(choice) => {
            setPath(choice);
          }}
        />
      )}
      {path === "configure" && (
        <AuthLayout wide hero={false}>
          <ConfigureWizard
            needsAdmin={token !== null}
            onCancel={() => {
              setPath("choose");
            }}
            onFinished={(adminCreated) => {
              finish({ kind: "configured", adminCreated });
            }}
          />
        </AuthLayout>
      )}
      {path === "restore" && (
        <AuthLayout wide hero={false}>
          <RestoreFlow
            token={token}
            onCancel={() => {
              setPath("choose");
            }}
            onRestored={(result) => {
              finish({ kind: "restored", result });
            }}
          />
        </AuthLayout>
      )}
    </SetupContext.Provider>
  );
}

/** The setup token, asked for once and kept in memory; a wrong one is slowed down by the server like a password. */
function TokenScreen({
  notice,
  onAccepted,
  onLoginNeeded,
}: {
  notice: string | null;
  onAccepted: (token: string, status: SetupStatus) => void;
  onLoginNeeded: () => void;
}) {
  const { setup } = useServices();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [waitUntil, setWaitUntil] = useState<number | null>(null);
  const secondsLeft = useCountdown(waitUntil);

  function submit(event: FormEvent) {
    event.preventDefault();
    const typed = token.trim();
    if (typed === "" || busy || secondsLeft > 0) return;
    setBusy(true);
    setup.status(typed).then(
      (status) => {
        setBusy(false);
        onAccepted(typed, status);
      },
      (failure: unknown) => {
        setBusy(false);
        // An administrator exists already: the server wants that account's session, not the token.
        if (isApiError(failure) && failure.code === "not_logged_in") {
          onLoginNeeded();
          return;
        }
        setError(failure);
        if (isApiError(failure) && failure.status === 429) setWaitUntil(Date.now() + (failure.retryAfter ?? 5) * 1000);
      },
    );
  }

  const wrong = isApiError(error) && error.code === "invalid_setup_token";
  return (
    <AuthLayout>
      <div className="auth__intro">
        <p className="eyebrow">
          <Icon name="sparkle" /> First-run setup
        </p>
        <h1>Welcome to Homelab Probe</h1>
        <p>This server has no settings yet. A few steps connect it to your UniFi controller.</p>
      </div>
      <InsecureNotice />
      <form className="card card--glass" onSubmit={submit} noValidate>
        {notice !== null && error === null && (
          <Banner tone="info" role="alert" title="Enter the setup token again">
            <p className="banner__text">{notice}</p>
          </Banner>
        )}
        {error !== null && !wrong && (
          <Banner tone="danger" role="alert" title={isApiError(error) && error.status === 429 ? "Too many attempts" : "Could not check the token"}>
            <p className="banner__text">
              <Text value={messageOf(error)} />
            </p>
          </Banner>
        )}
        <div className="field">
          <label htmlFor="setup-token">Setup token</label>
          <p id="setup-token-hint" className="hint">
            The server printed it once when it started (in its terminal, or <code>docker logs</code> for a container), unless you chose it with{" "}
            <code>HLP_SETUP_TOKEN</code>.
          </p>
          <SecretInput
            id="setup-token"
            mono
            autoComplete="off"
            value={token}
            aria-invalid={wrong}
            aria-describedby={`setup-token-hint${wrong ? " setup-token-error" : ""}`}
            onChange={(event) => {
              setToken(event.target.value);
            }}
          />
          {wrong && (
            <p id="setup-token-error" className="field-error" role="alert">
              <Icon name="alert" />
              <Text value={messageOf(error)} />
            </p>
          )}
        </div>
        <button type="submit" className="button button--brand button--block" disabled={busy || secondsLeft > 0 || token.trim() === ""}>
          {busy ? <span className="spinner" aria-hidden="true" /> : <Icon name="key" />}
          {secondsLeft > 0 ? `Wait ${secondsLeft} s` : busy ? "Checking…" : "Start the setup"}
        </button>
      </form>
    </AuthLayout>
  );
}

function ChoiceButton({ icon, title, text, brand = false, onClick }: { icon: IconName; title: string; text: string; brand?: boolean; onClick: () => void }) {
  return (
    <button type="button" className="choice" onClick={onClick}>
      <span className={brand ? "tile-icon tile-icon--brand" : "tile-icon"}>
        <Icon name={icon} />
      </span>
      <span>
        <span className="choice__title">{title}</span>
        <span className="choice__text">{text}</span>
      </span>
      <Icon name="chevronRight" />
    </button>
  );
}

function ChooseScreen({ mode, onChoose }: { mode: SetupStatus["mode"]; onChoose: (choice: "configure" | "restore") => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const admin = mode === "admin";
  return (
    <AuthLayout wide>
      <div className="auth__intro">
        <h1 ref={heading} tabIndex={-1}>
          {admin ? "One step left" : "How would you like to start?"}
        </h1>
        <p>{admin ? "This server has its settings; it needs its first administrator." : "Set up a new installation, or bring back one you backed up."}</p>
      </div>
      <div className="choices">
        <ChoiceButton
          icon={admin ? "user" : "server"}
          title={admin ? "Create the first administrator" : "Set up a new installation"}
          text={
            admin
              ? "Choose the user name and password of the account that manages this server."
              : "Connect your UniFi controller, check it, and create the first administrator. About five minutes."
          }
          onClick={() => {
            onChoose("configure");
          }}
        />
        <ChoiceButton
          icon="history"
          brand
          title="Restore from a backup"
          text="Bring back the settings, accounts, notes and history of another installation from its .hlpbackup file."
          onClick={() => {
            onChoose("restore");
          }}
        />
      </div>
    </AuthLayout>
  );
}

/** The configure path: the steps of a server with no settings, or only the administrator in the admin mode. */
function ConfigureWizard({ needsAdmin, onCancel, onFinished }: { needsAdmin: boolean; onCancel: () => void; onFinished: (adminCreated: boolean) => void }) {
  const [step, setStep] = useState(0);
  const admin = useSetup().status.mode === "admin";
  const next = () => {
    setStep((value) => value + 1);
  };
  const back = () => {
    setStep((value) => value - 1);
  };

  if (admin) {
    return (
      <>
        <div className="auth__intro">
          <p className="eyebrow">
            <Icon name="sparkle" /> Setup
          </p>
          <h1>The first administrator</h1>
        </div>
        <InsecureNotice />
        <FinishStep needsAdmin onBack={onCancel} onFinished={onFinished} />
      </>
    );
  }
  return (
    <>
      <div className="auth__intro">
        <p className="eyebrow">
          <Icon name="sparkle" /> Setup
        </p>
        <h1>Connect your controller</h1>
      </div>
      <InsecureNotice />
      <Stepper steps={CONFIGURE_STEPS} current={step} label="Setup progress" />
      {step === 0 && <ControllerStep onNext={next} onBack={onCancel} />}
      {step === 1 && <CertificateStep onNext={next} onBack={back} />}
      {step === 2 && <ConnectionStep onNext={next} onBack={back} />}
      {step === 3 && <NotificationsStep onNext={next} onBack={back} />}
      {step === 4 && <PreviewStep onNext={next} onBack={back} />}
      {step === 5 && (
        <FinishStep
          needsAdmin={needsAdmin}
          onBack={back}
          onFinished={onFinished}
          onRetest={() => {
            setStep(2);
          }}
        />
      )}
    </>
  );
}

function DoneScreen({ done, onLogin }: { done: Done; onLogin: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  return (
    <AuthLayout>
      <div className="card card--glass center">
        <div className="success-mark">
          <Icon name="check" />
        </div>
        <h1 ref={heading} tabIndex={-1}>
          {done.kind === "configured" ? "You're all set" : "Backup restored"}
        </h1>
        {done.kind === "configured" ? (
          <p>
            {done.adminCreated
              ? "The settings are saved and the administrator is ready. Log in with the account you just created."
              : "The settings are saved. Log in to start."}
          </p>
        ) : (
          <>
            <p>
              <Text value={done.result.message} />
            </p>
            {done.result.recovery_backup !== null && (
              <p className="muted small">
                The previous state is kept in the recovery backup <code><Text value={done.result.recovery_backup} /></code>.
              </p>
            )}
          </>
        )}
        <p>
          <button type="button" className="button button--brand" onClick={onLogin}>
            Go to the login
            <Icon name="arrowRight" />
          </button>
        </p>
      </div>
    </AuthLayout>
  );
}
