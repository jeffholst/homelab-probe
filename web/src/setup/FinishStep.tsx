import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import type { FinishResult } from "../api/setup";
import { useServices } from "../app/services";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { CodeBlock } from "../components/ui/CodeBlock";
import { SecretInput } from "../components/ui/SecretInput";
import { ErrorBanner, FieldError, StepFrame, codeOf, messageOf, useSetup } from "./common";

/** The server's account rules (`accounts.check_username`, `check_password_policy`), checked here first for a quick answer. */
export const MIN_PASSWORD = 12;
const USERNAME = /^[A-Za-z0-9][A-Za-z0-9._@-]{2,63}$/;
const MAX_PASSWORD = 1024;

type Fallback = Extract<FinishResult, { finished: false }>;

/**
 * The last step: the first administrator (when there is none yet) and Finish, which saves the settings, creates the
 * account and leaves the setup mode without a restart. When the settings cannot be saved on this machine the server
 * says why and gives back the files to put in place by hand, with placeholders where the secrets go.
 */
export function FinishStep({
  needsAdmin,
  onBack,
  onFinished,
  onRetest,
}: {
  needsAdmin: boolean;
  onBack?: (() => void) | undefined;
  onFinished: (adminCreated: boolean) => void;
  onRetest?: (() => void) | undefined;
}) {
  const { token, status } = useSetup();
  const { setup } = useServices();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [problem, setProblem] = useState<{ field: "username" | "password" | "repeat"; message: string } | null>(null);
  const [fallback, setFallback] = useState<Fallback | null>(null);

  const finish = useMutation({
    mutationFn: () => setup.finish(token, needsAdmin ? { username: username.trim(), password } : null),
    onSuccess: (result) => {
      setPassword("");
      setRepeat("");
      if (result.finished) onFinished(result.admin_created);
      else setFallback(result);
    },
    onError: () => {
      setPassword("");
      setRepeat("");
    },
  });

  if (fallback !== null) return <FallbackPanel fallback={fallback} onBack={() => { setFallback(null); }} />;

  function submit() {
    if (needsAdmin) {
      if (!USERNAME.test(username.trim())) {
        setProblem({ field: "username", message: "Use 3 to 64 letters, digits or . _ @ -, starting with a letter or a digit." });
        return;
      }
      const length = Array.from(password).length; // characters, as the server counts them
      if (length < MIN_PASSWORD || length > MAX_PASSWORD) {
        setProblem({ field: "password", message: `Use ${MIN_PASSWORD} to ${MAX_PASSWORD} characters.` });
        return;
      }
      if (password !== repeat) {
        setProblem({ field: "repeat", message: "The two passwords are not the same." });
        return;
      }
    }
    setProblem(null);
    finish.mutate();
  }

  const code = codeOf(finish.error);
  const adminError = code === "invalid_admin" ? messageOf(finish.error) : null;
  const fieldError = (field: "username" | "password" | "repeat") =>
    problem?.field === field ? problem.message : field === "username" ? adminError : null;
  const admin = status.mode === "admin";

  return (
    <StepFrame
      title={needsAdmin ? "Create the administrator" : "Finish the setup"}
      icon={needsAdmin ? "user" : "checkCircle"}
      lede={
        needsAdmin
          ? admin
            ? "The settings are in place. Create the first administrator to open the web interface."
            : "The first account of the web interface. It can add other people later, as administrators or viewers."
          : "Save the settings and start reading the controller."
      }
      onSubmit={submit}
      onBack={onBack}
      primary={{ label: "Finish setup", busy: finish.isPending, brand: true }}
    >
      {finish.isError && adminError === null && (
        <div className="stack">
          <ErrorBanner error={finish.error} title="The setup could not be finished" />
          {code === "not_tested" && onRetest !== undefined && (
            <p>
              <button type="button" className="button button--secondary" onClick={onRetest}>
                Test the connection again
              </button>
            </p>
          )}
        </div>
      )}
      {needsAdmin && (
        <>
          <div className="field">
            <label htmlFor="admin-username">User name</label>
            <input
              id="admin-username"
              className="input"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              aria-invalid={fieldError("username") !== null}
              aria-describedby={fieldError("username") !== null ? "admin-username-error" : undefined}
              onChange={(event) => {
                setUsername(event.target.value);
              }}
            />
            <FieldError id="admin-username-error" message={fieldError("username")} />
          </div>
          <div className="field">
            <label htmlFor="admin-password">Password</label>
            <p id="admin-password-hint" className="hint">
              At least {MIN_PASSWORD} characters. A sentence of a few words is easy to remember and hard to guess.
            </p>
            <SecretInput
              id="admin-password"
              autoComplete="new-password"
              value={password}
              aria-invalid={fieldError("password") !== null}
              aria-describedby={`admin-password-hint${fieldError("password") !== null ? " admin-password-error" : ""}`}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
            />
            <FieldError id="admin-password-error" message={fieldError("password")} />
          </div>
          <div className="field">
            <label htmlFor="admin-repeat">Password again</label>
            <SecretInput
              id="admin-repeat"
              autoComplete="new-password"
              value={repeat}
              aria-invalid={fieldError("repeat") !== null}
              aria-describedby={fieldError("repeat") !== null ? "admin-repeat-error" : undefined}
              onChange={(event) => {
                setRepeat(event.target.value);
              }}
            />
            <FieldError id="admin-repeat-error" message={fieldError("repeat")} />
          </div>
        </>
      )}
      {!admin && (
        <p className="muted small">
          The settings are saved in the server&apos;s data directory: <code>.env</code> (readable by its owner only), <code>hlp.toml</code> and,
          for a pinned certificate, <code>certs/controller.pem</code>.
        </p>
      )}
    </StepFrame>
  );
}

const REASONS: Record<string, string> = {
  environment: "Some of these settings are also set in the server's environment, which would keep winning over a saved file:",
  env_file_named: "This server reads its settings from a file named with --env-file or HLP_ENV, so a file saved in its data directory would not be used.",
  not_writable: "The settings could not be written to the server's data directory.",
};

/** What to do by hand when the server cannot save the settings itself: the files, with placeholders for the secrets. */
function FallbackPanel({ fallback, onBack }: { fallback: Fallback; onBack: () => void }) {
  return (
    <StepFrame title="Save the settings yourself" icon="file" onBack={onBack}>
      <Banner tone="warning" title="Nothing was saved">
        <p className="banner__text">
          {REASONS[fallback.reason] ?? "The settings could not be saved here."}
          {fallback.environment_names.length > 0 && (
            <>
              {" "}
              <Text value={fallback.environment_names.join(", ")} />.
            </>
          )}
        </p>
        {fallback.detail !== "" && (
          <p className="banner__text">
            <Text value={fallback.detail} />
          </p>
        )}
      </Banner>
      <p>
        Put these settings where the server reads them, replace each placeholder with its real value (
        <Text value={fallback.placeholders.join(", ")} />
        ), then restart the server. The secrets are not shown again: they were never sent back to this page.
      </p>
      <div className="stack">
        <CodeBlock title=".env" text={fallback.env} fileName="hlp.env" />
        <CodeBlock title="compose.yaml (environment)" text={fallback.compose} fileName="compose.hlp.yaml" mediaType="text/yaml" />
        {fallback.certificate !== null && (
          <>
            <p>
              Save the pinned certificate as a file and put its path in <code>UNIFI_VERIFY_SSL</code>:
            </p>
            <CodeBlock title="controller.pem" text={fallback.certificate} fileName="controller.pem" mediaType="application/x-pem-file" />
          </>
        )}
      </div>
    </StepFrame>
  );
}
