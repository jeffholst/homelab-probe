/**
 * The guided setup (`/api/v1/setup/*`) and the restore of a backup on a fresh installation (`/api/v1/backup/preview`,
 * `/api/v1/backup/restore`): the calls, their answers and the parsers that check them.
 *
 * Who may call them: while no administrator exists, whoever has the **setup token** (printed on the server's console,
 * or set in `HLP_SETUP_TOKEN`), sent in `X-Setup-Token`; once one exists, an administrator's session (the cookie and
 * the CSRF token, which the client adds by itself). The token is given to every call by the caller, which keeps it in
 * memory only: it is never stored, put in a URL or logged.
 *
 * What is typed is kept **on the server** (the draft): the API key and the notification settings are sent once and
 * only ever reported back as "set". The answers below hold no secret.
 */
import { SETUP_TOKEN_HEADER, type ApiClient } from "./client";
import { bad, flag, record, text } from "./types";

export type SetupMode = "setup" | "admin";
export type Verify = "true" | "pin" | "false";
export type CheckStatus = "ok" | "warn" | "fail" | "skip" | "info";

export interface CertificateInfo {
  /** SHA-256, as colon-separated hex pairs. */
  fingerprint: string;
  usable: boolean;
  /** Why it would not be accepted (a fixed reason), or "". */
  problem: string;
  problem_message: string;
}

export interface SetupDraft {
  url: string;
  site: string;
  api_key_set: boolean;
  verify: Verify;
  certificate: CertificateInfo | null;
  /** The last connection test of this draft: null when it has not been tested since it changed. */
  connection_ok: boolean | null;
  /** The names of the notification settings that are set (never their values). */
  notify: string[];
}

export interface SetupStatus {
  /** null: the server is set up. */
  mode: SetupMode | null;
  reason: string;
  /** The sentence that must be typed to turn certificate checking off. */
  unverified_phrase: string;
  draft: SetupDraft;
}

export interface Check {
  id: string;
  section: string;
  status: CheckStatus;
  title: string;
  message: string;
  fix: string;
}

export interface SiteChoice {
  name: string;
  ref: string;
  id: string;
}

export interface ConnectionResult {
  ok: boolean;
  sites: SiteChoice[];
  checks: Check[];
}

export interface PreviewFinding {
  severity: string;
  code: string;
  subject: string;
  message: string;
}

export interface PreviewResult {
  areas: string[];
  critical: number;
  warning: number;
  info: number;
  findings: PreviewFinding[];
  total: number;
  warnings: string[];
}

export interface WrittenStep {
  id: string;
  status: string;
  message: string;
}

export type FinishResult =
  | { finished: true; written: WrittenStep[]; admin_created: boolean }
  | {
      finished: false;
      /** `environment`, `env_file_named` or `not_writable`. */
      reason: string;
      detail: string;
      environment_names: string[];
      placeholders: string[];
      /** The finished `.env`, with placeholders where the secrets go. */
      env: string;
      compose: string;
      /** The pinned certificate (PEM), to save beside the settings, or null. */
      certificate: string | null;
    };

/** The fields of the draft to change; the ones left out stay as they are. A notification set to null is removed. */
export interface DraftChange {
  url?: string;
  site?: string;
  api_key?: string;
  verify?: Verify;
  fingerprint?: string;
  confirm?: string;
  notify?: Record<string, string | null>;
}

export const NOTIFY_SETTINGS = [
  "NOTIFY_NTFY_URL",
  "NOTIFY_NTFY_TOKEN",
  "NOTIFY_WEBHOOK_URL",
  "NOTIFY_WEBHOOK_TOKEN",
  "NOTIFY_SMTP_HOST",
  "NOTIFY_SMTP_PORT",
  "NOTIFY_SMTP_SECURITY",
  "NOTIFY_SMTP_USER",
  "NOTIFY_SMTP_PASSWORD",
  "NOTIFY_EMAIL_FROM",
  "NOTIFY_EMAIL_TO",
] as const;

// -- the parsers ------------------------------------------------------------------------------------------------

const WHAT = "the setup";

function strings(value: unknown, what: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) throw bad(what);
  return value as string[];
}

function count(fields: Record<string, unknown>, key: string, what: string): number {
  const value = fields[key] ?? 0;
  if (typeof value !== "number" || !Number.isFinite(value)) throw bad(what);
  return value;
}

function checkStatus(value: unknown, what: string): CheckStatus {
  if (value === "ok" || value === "warn" || value === "fail" || value === "skip" || value === "info") return value;
  throw bad(what);
}

function parseCertificate(value: unknown): CertificateInfo | null {
  if (value === null) return null;
  const fields = record(value, WHAT);
  return {
    fingerprint: text(fields, "fingerprint", WHAT),
    usable: flag(fields, "usable", WHAT),
    problem: text(fields, "problem", WHAT),
    problem_message: text(fields, "problem_message", WHAT),
  };
}

export function parseSetupStatus(value: unknown): SetupStatus {
  const fields = record(value, WHAT);
  const mode = fields["mode"];
  if (mode !== null && mode !== "setup" && mode !== "admin") throw bad(WHAT);
  const draft = record(fields["draft"], WHAT);
  const verify = draft["verify"];
  if (verify !== "true" && verify !== "pin" && verify !== "false") throw bad(WHAT);
  const tested = draft["connection_ok"];
  if (tested !== null && typeof tested !== "boolean") throw bad(WHAT);
  return {
    mode,
    reason: text(fields, "reason", WHAT),
    unverified_phrase: text(fields, "unverified_phrase", WHAT),
    draft: {
      url: text(draft, "url", WHAT),
      site: text(draft, "site", WHAT),
      api_key_set: flag(draft, "api_key_set", WHAT),
      verify,
      certificate: parseCertificate(draft["certificate"] ?? null),
      connection_ok: tested,
      notify: strings(draft["notify"], WHAT),
    },
  };
}

export function parseChecks(value: unknown, what = "the checks"): Check[] {
  if (!Array.isArray(value)) throw bad(what);
  return value.map((entry: unknown) => {
    const fields = record(entry, what);
    return {
      id: text(fields, "id", what),
      section: text(fields, "section", what),
      status: checkStatus(fields["status"], what),
      title: text(fields, "title", what),
      message: text(fields, "message", what),
      fix: text(fields, "fix", what),
    };
  });
}

export function parseConnection(value: unknown): ConnectionResult {
  const what = "the connection test";
  const fields = record(value, what);
  const sites = fields["sites"];
  if (!Array.isArray(sites)) throw bad(what);
  return {
    ok: flag(fields, "ok", what),
    checks: parseChecks(fields["checks"], what),
    sites: sites.map((entry: unknown) => {
      const site = record(entry, what);
      return { name: text(site, "name", what), ref: text(site, "ref", what), id: text(site, "id", what) };
    }),
  };
}

export function parsePreview(value: unknown): PreviewResult {
  const what = "the health check";
  const fields = record(value, what);
  const summary = record(fields["summary"], what);
  const findings = fields["findings"];
  if (!Array.isArray(findings)) throw bad(what);
  return {
    areas: strings(fields["areas"], what),
    critical: count(summary, "critical", what),
    warning: count(summary, "warning", what),
    info: count(summary, "info", what),
    total: count(fields, "total", what),
    warnings: strings(fields["warnings"], what),
    findings: findings.map((entry: unknown) => {
      const finding = record(entry, what);
      const code = finding["code"];
      return {
        severity: text(finding, "severity", what),
        code: typeof code === "string" ? code : "",
        subject: text(finding, "subject", what),
        message: text(finding, "message", what),
      };
    }),
  };
}

export function parseFinish(value: unknown): FinishResult {
  const what = "the end of the setup";
  const fields = record(value, what);
  if (flag(fields, "finished", what)) {
    const written = fields["written"];
    if (!Array.isArray(written)) throw bad(what);
    return {
      finished: true,
      admin_created: flag(fields, "admin_created", what),
      written: written.map((entry: unknown) => {
        const step = record(entry, what);
        return { id: text(step, "id", what), status: text(step, "status", what), message: text(step, "message", what) };
      }),
    };
  }
  const certificate = fields["certificate"] ?? null;
  if (certificate !== null && typeof certificate !== "string") throw bad(what);
  return {
    finished: false,
    reason: text(fields, "reason", what),
    detail: text(fields, "detail", what),
    environment_names: strings(fields["environment_names"], what),
    placeholders: strings(fields["placeholders"], what),
    env: text(fields, "env", what),
    compose: text(fields, "compose", what),
    certificate,
  };
}

// -- the calls --------------------------------------------------------------------------------------------------

export interface SetupApi {
  status(token: string | null, signal?: AbortSignal): Promise<SetupStatus>;
  draft(token: string | null, change: DraftChange): Promise<SetupStatus>;
  certificate(token: string | null): Promise<SetupStatus>;
  connection(token: string | null): Promise<ConnectionResult>;
  preview(token: string | null): Promise<PreviewResult>;
  notifications(token: string | null): Promise<Check[]>;
  finish(token: string | null, admin: { username: string; password: string } | null): Promise<FinishResult>;
}

/** The token header, or none when the caller acts with an administrator's session. */
export function tokenHeaders(token: string | null): Record<string, string> {
  return token === null ? {} : { [SETUP_TOKEN_HEADER]: token };
}

export function createSetupApi(client: ApiClient): SetupApi {
  return {
    status: (token, signal) => client.get("/setup/status", { signal, headers: tokenHeaders(token), parse: parseSetupStatus }),
    draft: (token, change) => client.post("/setup/draft", { body: change, headers: tokenHeaders(token), parse: parseSetupStatus }),
    certificate: (token) => client.post("/setup/certificate", { headers: tokenHeaders(token), parse: parseSetupStatus }),
    connection: (token) => client.post("/setup/connection", { headers: tokenHeaders(token), parse: parseConnection }),
    preview: (token) => client.post("/setup/preview", { headers: tokenHeaders(token), parse: parsePreview }),
    notifications: (token) =>
      client.post("/setup/notifications", {
        headers: tokenHeaders(token),
        parse: (value) => parseChecks(record(value, "the notification check")["checks"], "the notification check"),
      }),
    finish: (token, admin) => client.post("/setup/finish", { body: admin ?? {}, headers: tokenHeaders(token), parse: parseFinish }),
  };
}
