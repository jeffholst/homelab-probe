import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { isApiError } from "../api/errors";
import { setupApi, type Check, type Connection, type Finish, type Preview, type SetupStatus } from "../api/setup";
import { useMeta } from "../app/meta";
import { endSession, useServices } from "../app/services";
import { Banner } from "../components/DataStates";
import { Text } from "../components/Text";
import { ThemeSwitch } from "../components/ThemeSwitch";
import { safeText } from "../lib/safeText";
import { useCountdown } from "../lib/useCountdown";
import { usePageTitle } from "../lib/usePageTitle";

const ERRORS: Record<string, string> = {
  invalid_setup_token: "The setup token is missing, expired or wrong. Enter the token from the current server log.",
  not_logged_in: "An administrator session is now required. Log in below, or recheck the server state.",
  forbidden: "An administrator account is required.",
  csrf_token: "Your session has changed. Authorize setup again.",
  invalid_credentials: "The administrator user name or password is wrong.",
  too_many_attempts: "Too many attempts. Wait for the countdown before trying again.",
  read_only: "This server is read-only. Restart it without --read-only before setting it up.",
  invalid_setting: "The settings were rejected. Check the address, site, key and certificate choice. Nothing in this update was saved.",
  confirmation_required: "Type the certificate warning exactly before disabling verification.",
  certificate_required: "Fetch the controller certificate first.",
  certificate_unusable: "The certificate cannot be trusted for this address. Correct the controller certificate or address.",
  fingerprint_mismatch: "The fingerprint does not match the fetched certificate.",
  controller_unavailable: "The controller could not be reached safely. Check its address and certificate.",
  not_tested: "Test the connection with the current settings before finishing.",
  invalid_admin: "The administrator was rejected. Use a unique user name of 3 to 64 characters (letters, numbers, dot, underscore or hyphen) and a password of at least 12 characters.",
  admin_required: "Enter the first administrator's user name and password.",
  admin_exists: "An administrator was created elsewhere. Recheck the server state and log in with that account.",
  already_set_up: "This installation is already set up. Recheck the server state to continue to login.",
  step_unavailable: "The setup mode has changed. Recheck the server state.",
  reload_failed: "Settings were saved but could not be loaded. Restart the server; do not submit finish again.",
  admin_not_created: "Settings were saved but the administrator could not be created. Run hlp web-user add NAME --role admin with the same data directory and restart.",
};

function setupError(error: unknown): string {
  if (isApiError(error)) return (Object.hasOwn(ERRORS, error.code) ? ERRORS[error.code] : undefined)
    ?? "The setup request could not be completed. Check the server log and recheck its state.";
  return "The setup request could not be completed. Recheck the server state.";
}

function Field({ id, label, value, onChange, secret = false, required = false, maxLength = 2048, minLength, errorId }: {
  id: string; label: string; value: string; onChange: (value: string) => void;
  secret?: boolean; required?: boolean; maxLength?: number; minLength?: number;
  errorId?: string | undefined;
}) {
  return <div className="field">
    <label htmlFor={id}>{label}</label>
    <input id={id} className="input" type={secret ? "password" : "text"} autoComplete="off"
      autoCapitalize="none" spellCheck={false} value={value} required={required}
      aria-invalid={errorId ? true : undefined} aria-describedby={errorId}
      maxLength={maxLength} minLength={minLength} onChange={(event) => onChange(event.target.value)} />
  </div>;
}

function Checks({ checks }: { checks: Check[] }) {
  return <ul className="plain-list">{checks.map((check, index) => <li key={index}>
    <strong><Text value={check.status.toUpperCase()} />: <Text value={check.title} /></strong>
    <p><Text value={check.message} /> {check.fix && <Text value={check.fix} />}</p>
  </li>)}</ul>;
}

function Download({ name, value, children }: { name: string; value: string; children: ReactNode }) {
  const [problem, setProblem] = useState(false);
  function download() {
    try {
      const url = URL.createObjectURL(new Blob([value], { type: "text/plain;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setProblem(true); }
  }
  return <><button type="button" className="button button--secondary" onClick={download}>{children}</button>
    {problem && <p role="alert">Download unavailable. Select and copy the text below.</p>}</>;
}

function Fallback({ result }: { result: Extract<Finish, { finished: false }> }) {
  return <section className="setup__section" aria-labelledby="fallback-heading">
    <h2 id="fallback-heading">Settings were not saved</h2>
    <Banner tone="warning" title="Setup is not finished">
      <p>The environment, a named configuration file or filesystem permissions prevented saving. Apply the placeholder files manually, then restart the server.</p>
    </Banner>
    <p>Reason: <Text value={result.reason} /></p>
    {result.environment_names.length > 0 && <p>Environment overrides: <Text value={result.environment_names.join(", ")} /></p>}
    <p>Fill these placeholders on the server: <Text value={result.placeholders.join(", ")} />. No administrator was created.</p>
    <h3>Environment file</h3>
    <Download name="env.example" value={result.env}>Download environment template</Download>
    <pre className="setup__template"><Text value={result.env} /></pre>
    <h3>Compose settings</h3>
    <Download name="compose-settings.yaml" value={result.compose}>Download Compose template</Download>
    <pre className="setup__template"><Text value={result.compose} /></pre>
    {result.certificate && <><h3>Controller certificate</h3>
      <Download name="controller.pem" value={result.certificate}>Download certificate</Download>
      <pre className="setup__template"><Text value={result.certificate} /></pre></>}
  </section>;
}

export function SetupPage() {
  usePageTitle("Set up");
  const meta = useMeta();
  const { client, api, queryClient } = useServices();
  const [tokenInput, setTokenInput] = useState("");
  const [token, setToken] = useState<string | undefined>(undefined);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [useAdmin, setUseAdmin] = useState(false);
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [waitUntil, setWaitUntil] = useState<number | null>(null);
  const seconds = useCountdown(waitUntil);
  const mounted = useRef(false);
  const inFlight = useRef(false);
  const epoch = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; client.clearCsrfToken(); };
  }, [client]);

  function forget() {
    epoch.current++;
    setToken(undefined);
    setTokenInput(""); setPassword(""); setStatus(null); setError("");
    client.clearCsrfToken(); endSession(queryClient);
  }

  async function authorize(event: FormEvent) {
    event.preventDefault();
    if (inFlight.current || seconds > 0) return;
    inFlight.current = true; setBusy(true); setError("");
    const generation = epoch.current;
    const supplied = tokenInput;
    setTokenInput("");
    const adminPassword = password;
    setPassword("");
    try {
      if (useAdmin) {
        const session = await api.login(username, adminPassword);
        if (!mounted.current || generation !== epoch.current) { client.clearCsrfToken(); return; }
        if (session.role !== "admin") {
          client.clearCsrfToken();
          throw new Error("Administrator required");
        }
      }
      const found = await setupApi(client, useAdmin ? undefined : supplied).status();
      if (!mounted.current || generation !== epoch.current) return;
      if (found.mode === null) { await meta.refetch(); return; }
      setToken(useAdmin ? undefined : supplied);
      setStatus(found);
    } catch (caught) {
      if (!mounted.current || generation !== epoch.current) return;
      setToken(undefined);
      client.clearCsrfToken();
      setError(setupError(caught));
      if (isApiError(caught) && caught.status === 429) setWaitUntil(Date.now() + (caught.retryAfter ?? 5) * 1000);
      if (isApiError(caught) && caught.code === "not_logged_in") setUseAdmin(true);
      await meta.refetch();
    } finally {
      if (!mounted.current || generation !== epoch.current) client.clearCsrfToken();
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  const restricted = meta.data?.read_only || meta.data?.demo;
  const knownMode = meta.data?.setup_mode === "setup" || meta.data?.setup_mode === "admin";
  return <div className="login">
    <a className="skip-link" href="#main">Skip to main content</a>
    <header className="login__top"><p className="brand">Homelab Probe</p><ThemeSwitch /></header>
    <main id="main" className="setup">
      <h1>Set up Homelab Probe</h1>
      {meta.data && !meta.data.https && !meta.data.loopback && <Banner tone="warning" title="This connection is not encrypted">
        <p>Your setup token, key and password would travel in clear text. Use an SSH tunnel or an HTTPS reverse proxy.</p>
      </Banner>}
      {restricted ? <Banner tone="warning" title="Setup is unavailable">
        <p>Restart without --read-only or --demo to configure this installation.</p>
      </Banner> : !knownMode ? <Banner tone="danger" title="Unknown setup mode">
        <p>Check the server version and recheck its state. No setup requests have been sent.</p>
      </Banner> : status ? <SetupFlow key={status.mode} initial={status} token={token}
        onForget={forget} onBusy={setBusy} onExpired={(message, requireAdmin) => { forget(); setError(message); setUseAdmin(requireAdmin); }}
        onSaved={() => setToken(undefined)} administrator={useAdmin} /> : <section className="setup__section">
        <h2>{useAdmin ? "Authorize with an administrator" : "Authorize setup"}</h2>
        {error && <Banner tone="danger" role="alert" title="Could not authorize setup"><p id="authorization-error">{error}</p></Banner>}
        <form onSubmit={(event) => void authorize(event)}>
          <fieldset disabled={busy || seconds > 0} className="setup__fields">
            <legend className="visually-hidden">Setup authorization</legend>
            {useAdmin ? <>
              <Field id="setup-user" label="Administrator user name" value={username} onChange={setUsername} required maxLength={256} />
              <Field id="setup-password" label="Administrator password" value={password} onChange={setPassword} secret required maxLength={1024} errorId={error ? "authorization-error" : undefined} />
            </> : <Field id="setup-token" label="Setup token from the server log" value={tokenInput} onChange={setTokenInput} secret required maxLength={1024} errorId={error ? "authorization-error" : undefined} />}
            <button className="button" type="submit">{seconds > 0 ? `Wait ${seconds} s` : busy ? "Authorizing..." : "Continue"}</button>
          </fieldset>
        </form>
        <div className="setup__actions"><button className="button button--secondary" type="button" disabled={busy || seconds > 0}
          onClick={() => { forget(); setUseAdmin(!useAdmin); }}>{useAdmin ? "Use a setup token" : "Use an administrator account"}</button></div>
      </section>}
      <div className="setup__actions"><button type="button" className="button button--secondary" disabled={busy}
        onClick={() => { forget(); void meta.refetch(); }}>Recheck server state</button></div>
    </main>
    <footer className="login__foot muted"><span>Controller configuration is never changed.</span></footer>
  </div>;
}

const NOTIFICATIONS = [
  ["NOTIFY_NTFY_URL", "ntfy topic URL"], ["NOTIFY_NTFY_TOKEN", "ntfy token"],
  ["NOTIFY_WEBHOOK_URL", "Webhook URL"], ["NOTIFY_WEBHOOK_TOKEN", "Webhook token"],
  ["NOTIFY_SMTP_HOST", "SMTP host"], ["NOTIFY_SMTP_PORT", "SMTP port"],
  ["NOTIFY_SMTP_USER", "SMTP user"], ["NOTIFY_SMTP_PASSWORD", "SMTP password"],
  ["NOTIFY_EMAIL_FROM", "Email sender"], ["NOTIFY_EMAIL_TO", "Email recipients"],
] as const;

function SetupFlow({ initial, token, onForget, onExpired, onBusy, onSaved, administrator }: {
  initial: SetupStatus; token: string | undefined; onForget: () => void; administrator: boolean;
  onExpired: (message: string, requireAdmin: boolean) => void; onBusy: (busy: boolean) => void; onSaved: () => void;
}) {
  const { client, api, queryClient } = useServices();
  const meta = useMeta();
  const setup = setupApi(client, token);
  const [status, setStatus] = useState(initial);
  const [url, setUrl] = useState(initial.draft.url);
  const [site, setSite] = useState(initial.draft.site);
  const [key, setKey] = useState("");
  const [dirty, setDirty] = useState(false);
  const [fingerprint, setFingerprint] = useState("");
  const [phrase, setPhrase] = useState("");
  const [notify, setNotify] = useState<Record<string, string>>({});
  const [checks, setChecks] = useState<Check[]>([]);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [fallback, setFallback] = useState<Extract<Finish, { finished: false }> | null>(null);
  const [error, setError] = useState("");
  const [invalidField, setInvalidField] = useState("");
  const [pending, setPending] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [finished, setFinished] = useState(false);
  const [waitUntil, setWaitUntil] = useState<number | null>(null);
  const seconds = useCountdown(waitUntil);
  const mounted = useRef(false), inFlight = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  function changed() { setDirty(true); setConnection(null); setPreview(null); setFallback(null); setConfirmed(false); }
  function remember(found: SetupStatus) {
    setStatus(found); setUrl(found.draft.url); setSite(found.draft.site); setDirty(false);
    setFingerprint(""); setPhrase(""); setConfirmed(false); setFallback(null); setPreview(null);
  }
  async function run(label: string, action: () => Promise<void>, finishing = false) {
    if (inFlight.current || seconds > 0 || uncertain || finished) return;
    inFlight.current = true; setPending(label); onBusy(true); setError(""); setInvalidField("");
    try { await action(); } catch (caught) {
      if (!mounted.current) return;
      setKey(""); setPassword(""); setRepeat(""); setPhrase(""); setNotify({}); setConfirmed(false);
      setError(setupError(caught));
      if (isApiError(caught)) {
        const fields: Record<string, string> = { UNIFI_URL: "controller-url", UNIFI_SITE_ID: "controller-site", UNIFI_API_KEY: "controller-key" };
        const setting = caught.details["setting"];
        if (typeof setting === "string" && Object.hasOwn(fields, setting)) setInvalidField(fields[setting] ?? "");
      }
      if (isApiError(caught) && caught.status === 429) setWaitUntil(Date.now() + (caught.retryAfter ?? 5) * 1000);
      // Finish may have written files before its response failed; require an explicit state check, not a retry.
      if (finishing && (!isApiError(caught) || caught.status === 0 || caught.status >= 500)) setUncertain(true);
      if (isApiError(caught) && caught.code === "not_tested") {
        setStatus((previous) => ({ ...previous, draft: { ...previous.draft, connection_ok: null } }));
      }
      if (isApiError(caught) && [401, 403].includes(caught.status) && caught.code !== "read_only") {
        onExpired(setupError(caught), caught.code !== "invalid_setup_token");
      }
      if (isApiError(caught) && [401, 403, 409, 503].includes(caught.status)) await meta.refetch();
    } finally { inFlight.current = false; onBusy(false); if (mounted.current) setPending(""); }
  }
  const disabled = pending !== "" || seconds > 0 || uncertain || finished;
  const ready = !dirty && status.draft.connection_ok === true;
  const cert = status.draft.certificate;

  async function finish() {
    const body = administrator ? {} : { username, password };
    setPassword(""); setRepeat(""); setKey(""); setNotify({});
    const result = await setup.finish(body);
    if (!mounted.current) return;
    if (!result.finished) { setFallback(result); setConfirmed(false); return; }
    setFinished(true);
    onSaved();
    client.clearCsrfToken();
    await queryClient.cancelQueries();
    endSession(queryClient);
    queryClient.getMutationCache().clear();
    try {
      const fresh = await api.meta();
      if (!mounted.current) return;
      queryClient.setQueryData(["meta"], fresh);
      onForget();
    } catch {
      if (mounted.current) setError("Setup was saved, but the server state could not be refreshed. Recheck its state to continue to login.");
    }
  }

  return <>
    {error && <Banner tone="danger" role="alert" title="Setup needs attention"><p id="setup-error">{error}</p></Banner>}
    {uncertain && <Banner tone="warning" title="Completion is uncertain">
      <p>Files may already have changed. Do not submit finish again. Check the server log, follow its recovery guidance and recheck the server state.</p>
    </Banner>}
    {finished && <Banner tone="success" title="Setup was saved"><p>Recheck the server state to continue to login. No restart is required after a successful reload.</p></Banner>}
    {pending && <p className="refreshing" role="status"><span className="spinner" aria-hidden="true" />{pending}</p>}
    {seconds > 0 && <p role="status">Wait {seconds} s before another request.</p>}
    <fieldset disabled={disabled} className="setup__fields">
      <legend className="visually-hidden">Installation settings</legend>
      {status.mode === "setup" && <>
        <section className="setup__section" aria-labelledby="controller-heading">
          <h2 id="controller-heading">Controller connection</h2>
          <form onSubmit={(event) => {
            event.preventDefault();
            void run("Saving the draft...", async () => {
              const body = { url, site, ...(key ? { api_key: key } : {}) };
              setKey("");
              const found = await setup.draft(body);
              if (mounted.current) { remember(found); setConnection(null); }
            });
          }}>
            <Field id="controller-url" label="Controller HTTPS address" value={url} onChange={(value) => { setUrl(value); changed(); }} required errorId={invalidField === "controller-url" ? "setup-error" : undefined} />
            <Field id="controller-site" label="Site name, reference or ID" value={site} onChange={(value) => { setSite(value); changed(); }} required maxLength={128} errorId={invalidField === "controller-site" ? "setup-error" : undefined} />
            <Field id="controller-key" label={status.draft.api_key_set ? "Replace API key (already set)" : "API key"} value={key} onChange={(value) => { setKey(value); changed(); }} secret required={!status.draft.api_key_set} maxLength={512} errorId={invalidField === "controller-key" ? "setup-error" : undefined} />
            <button type="submit" className="button">Save draft</button>
          </form>
          {dirty && <p role="status">Unsaved changes. Save and test this draft before finishing.</p>}
        </section>
        <section className="setup__section" aria-labelledby="certificate-heading">
          <h2 id="certificate-heading">Certificate trust</h2>
          <p>Verification: <Text value={status.draft.verify === "true" ? "System certificate authorities" : status.draft.verify === "pin" ? "Pinned controller certificate" : "Not verified"} /></p>
          <div className="setup__actions">
            {/* Re-fetching replaces the server's certificate. Leave pin mode first so the old test cannot authorize a new pin. */}
            <button type="button" className="button button--secondary" disabled={dirty || !status.draft.url || status.draft.verify === "pin"}
              title={status.draft.verify === "pin" ? "Use system trust before fetching another certificate" : undefined}
              onClick={() => void run("Fetching certificate...", async () => { const found = await setup.certificate(); if (mounted.current) remember(found); })}>Fetch certificate</button>
            <button type="button" className="button button--secondary" disabled={dirty || status.draft.verify === "true"}
              onClick={() => void run("Enabling certificate verification...", async () => { const found = await setup.draft({ verify: "true" }); if (mounted.current) { remember(found); setConnection(null); } })}>Use system trust</button>
          </div>
          {cert && <>
            <p>SHA-256 fingerprint: <code><Text value={cert.fingerprint} /></code></p>
            {!cert.usable ? <Banner tone="warning" title="Certificate cannot be pinned"><p><Text value={cert.problem_message} /></p></Banner> :
              <form onSubmit={(event) => { event.preventDefault(); void run("Trusting certificate...", async () => {
                const found = await setup.draft({ verify: "pin", fingerprint });
                if (mounted.current) { remember(found); setConnection(null); }
              }); }}>
                <p>Compare the fingerprint with the controller's certificate details before trusting it.</p>
                <Field id="certificate-fingerprint" label="Verified fingerprint" value={fingerprint} onChange={setFingerprint} required maxLength={200} />
                <button type="submit" className="button button--secondary" disabled={dirty || !fingerprint}>Trust this fingerprint</button>
              </form>}
          </>}
          <details>
            <summary>Connect without verifying the certificate</summary>
            <Banner tone="warning" title="Your API key could reach an impersonator"><p>Certificate verification protects the controller connection. Disabling it is not recommended.</p></Banner>
            <p>Confirmation: <Text value={status.unverified_phrase} /></p>
            <form onSubmit={(event) => { event.preventDefault(); void run("Changing certificate verification...", async () => {
              const confirm = phrase; setPhrase("");
              const found = await setup.draft({ verify: "false", confirm });
              if (mounted.current) { remember(found); setConnection(null); }
            }); }}>
              <Field id="unverified-confirmation" label="Type the confirmation sentence" value={phrase} onChange={setPhrase} required maxLength={200} />
              <button type="submit" className="button button--secondary" disabled={dirty || phrase !== status.unverified_phrase}>Disable verification</button>
            </form>
          </details>
        </section>
        <section className="setup__section" aria-labelledby="test-heading">
          <h2 id="test-heading">Connection checks</h2>
          <div className="setup__actions"><button type="button" className="button" disabled={dirty || !status.draft.api_key_set}
            onClick={() => void run("Testing the connection...", async () => {
              const result = await setup.connection();
              const found = await setup.status();
              if (mounted.current) { remember(found); setConnection(result); }
            })}>Test connection</button>
            <button type="button" className="button button--secondary" disabled={!ready}
              onClick={() => void run("Running initial health checks...", async () => { const result = await setup.preview(); if (mounted.current) setPreview(result); })}>Preview health checks</button></div>
          {connection && <><Banner tone={connection.ok ? "success" : "warning"} title={connection.ok ? "Connection test passed" : "Connection test failed"} /><Checks checks={connection.checks} />
            {connection.sites.length > 0 && <div className="field"><label htmlFor="site-picker">Available sites</label>
              <select className="select" id="site-picker" value={site} onChange={(event) => { setSite(event.target.value); changed(); }}>
                <option value={site}>{safeText(site)}</option>
                {connection.sites.filter((item) => (item.ref || item.id) !== site).map((item, index) => <option key={index} value={item.ref || item.id}>{safeText(item.name)}</option>)}
              </select></div>}
          </>}
          {preview && <Banner tone={preview.warnings.length > 0 || preview.total > 0 ? "warning" : "info"} title="Initial health check preview">
            <p>{preview.total} findings. This preview excludes event checks and is not a complete health assessment.</p>
            <ul>{preview.warnings.map((warning, index) => <li key={index}><Text value={warning} /></li>)}</ul>
          </Banner>}
        </section>
        <section className="setup__section" aria-labelledby="notifications-heading">
          <h2 id="notifications-heading">Notifications</h2>
          {status.draft.notify.length > 0 && <p>Configured settings: <Text value={status.draft.notify.join(", ")} /></p>}
          <details><summary>Optional notification destinations</summary>
            <form onSubmit={(event) => { event.preventDefault(); void run("Saving notification settings...", async () => {
              const values = notify; setNotify({}); setChecks([]);
              const found = await setup.draft({ notify: values });
              if (mounted.current) remember(found);
            }); }}>
              <div className="setup__grid">{NOTIFICATIONS.map(([name, label]) => <Field key={name} id={name} label={label}
                value={notify[name] ?? ""} secret onChange={(value) => setNotify({ ...notify, [name]: value })} maxLength={1024} />)}
                <div className="field"><label htmlFor="smtp-security">SMTP security</label><select id="smtp-security" className="select" value={notify["NOTIFY_SMTP_SECURITY"] ?? ""}
                  onChange={(event) => setNotify({ ...notify, NOTIFY_SMTP_SECURITY: event.target.value })}>
                  <option value="">Unchanged</option><option value="starttls">STARTTLS</option><option value="ssl">TLS</option><option value="none">Unencrypted (no password)</option>
                </select></div>
              </div>
              <button type="submit" className="button button--secondary" disabled={Object.keys(notify).length === 0}>Save notification draft</button>
            </form>
            <div className="setup__actions"><button type="button" className="button button--secondary" disabled={!ready || Object.keys(notify).length > 0}
              onClick={() => void run("Checking notifications without sending...", async () => { const result = await setup.notifications(); if (mounted.current) setChecks(result); })}>Dry run notifications</button></div>
            {checks.length > 0 && <Checks checks={checks} />}
          </details>
        </section>
      </>}
      <section className="setup__section" aria-labelledby="admin-heading">
        <h2 id="admin-heading">{administrator ? "Finish setup" : "First administrator"}</h2>
        {status.mode === "admin" && <p>The controller is already configured. Only the first administrator is missing.</p>}
        <form onSubmit={(event) => { event.preventDefault(); if (confirmed && (administrator || password === repeat)) void run("Finishing setup...", finish, true); }}>
          {!administrator && <>
            <Field id="first-admin" label="Administrator user name" value={username} onChange={setUsername} required minLength={3} maxLength={64} />
            <Field id="first-password" label="Administrator password" value={password} onChange={setPassword} secret required minLength={12} maxLength={1024} />
            <Field id="first-repeat" label="Confirm administrator password" value={repeat} onChange={setRepeat} secret required minLength={12} maxLength={1024} />
            {repeat && password !== repeat && <p role="alert">Passwords do not match.</p>}
          </>}
          <label className="setup__confirmation"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            {administrator ? "Save this installation's configuration" : "Create this administrator and finish setup"}</label>
          <button className="button" type="submit" disabled={!confirmed || (status.mode === "setup" && (!ready || Object.keys(notify).length > 0)) || (!administrator && (!password || password !== repeat))}>Finish setup</button>
        </form>
      </section>
    </fieldset>
    {fallback && <Fallback result={fallback} />}
    <div className="setup__actions"><button type="button" className="button button--secondary" disabled={pending !== ""}
      onClick={onForget}>Clear browser credentials</button></div>
  </>;
}
