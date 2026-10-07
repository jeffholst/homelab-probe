import { useState, type FormEvent } from "react";
import { Navigate, useSearchParams } from "react-router-dom";

import { isApiError } from "../api/errors";
import { useMeta } from "../app/meta";
import { safeNext, useLogin, useSession } from "../auth/session";
import { Banner, ErrorState, Loading } from "../components/DataStates";
import { Text } from "../components/Text";
import { Icon } from "../components/ui/Icon";
import { AuthLayout } from "../layouts/AuthLayout";
import { useCountdown } from "../lib/useCountdown";
import { usePageTitle } from "../lib/usePageTitle";

export function LoginPage() {
  usePageTitle("Log in");
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const session = useSession();
  const meta = useMeta();
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [waitUntil, setWaitUntil] = useState<number | null>(null);
  const secondsLeft = useCountdown(waitUntil);

  if (session.data) return <Navigate to={next} replace />;
  // A server that is not set up has nobody to log in: its setup is the only thing to do (unless an administrator
  // exists already and has to log in to continue it, which the setup page sends here with next=/setup).
  if (meta.data?.needs_setup === true && next !== "/setup") return <Navigate to="/setup" replace />;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (secondsLeft > 0 || login.isPending) return;
    login.mutate(
      { username, password },
      {
        onSuccess: () => {
          setPassword("");
        },
        onError: (error) => {
          setPassword("");
          if (isApiError(error) && error.status === 429) setWaitUntil(Date.now() + (error.retryAfter ?? 5) * 1000);
        },
      },
    );
  }

  const error = login.error;
  const unencrypted = meta.data !== undefined && !meta.data.https && !meta.data.loopback;

  return (
    <AuthLayout>
      <div className="auth__intro">
        <h1>Log in</h1>
        <p>Welcome back. Sign in to see the health of your network.</p>
      </div>
      <div className="card card--glass">
        {unencrypted && (
          <Banner tone="warning" title="This connection is not encrypted">
            <p className="banner__text">
              Your password would travel in clear text. Use an SSH tunnel or an HTTPS reverse proxy to reach this server.
            </p>
          </Banner>
        )}
        {meta.isPending && <Loading label="the server's details" />}
        {meta.isError && <ErrorState error={meta.error} label="the server's details" onRetry={() => void meta.refetch()} />}
        {login.isError && (
          <Banner tone="danger" role="alert" title={isApiError(error) && error.status === 429 ? "Too many attempts" : "Could not log in"}>
            <p className="banner__text">
              <Text value={isApiError(error) ? error.message : "Something went wrong."} />
            </p>
          </Banner>
        )}
        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="username">User name</label>
            <input
              id="username"
              name="username"
              className="input"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              className="input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
            />
          </div>
          <button type="submit" className="button button--brand button--block" disabled={secondsLeft > 0 || login.isPending}>
            {login.isPending ? <span className="spinner" aria-hidden="true" /> : <Icon name="lock" />}
            {secondsLeft > 0 ? `Wait ${secondsLeft} s` : login.isPending ? "Logging in…" : "Log in"}
          </button>
        </form>
      </div>
      <p className="center muted small">
        Forgot your password? Ask an administrator, or run <code>hlp web-user</code> on the server.
      </p>
    </AuthLayout>
  );
}
