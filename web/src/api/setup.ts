import { type ApiClient } from "./client";
import { ApiError } from "./errors";

export interface Certificate {
  fingerprint: string;
  usable: boolean;
  problem_message: string;
}
export interface SetupStatus {
  mode: "setup" | "admin" | null;
  unverified_phrase: string;
  draft: {
    url: string;
    site: string;
    api_key_set: boolean;
    verify: "true" | "pin" | "false";
    certificate: Certificate | null;
    connection_ok: boolean | null;
    notify: string[];
  };
}
export interface DraftInput {
  url?: string;
  site?: string;
  api_key?: string;
  verify?: "true" | "pin" | "false";
  fingerprint?: string;
  confirm?: string;
  notify?: Record<string, string>;
}
export interface Check {
  id: string;
  status: string;
  title: string;
  message: string;
  fix: string;
}
export interface Connection {
  ok: boolean;
  sites: { name: string; ref: string; id: string }[];
  checks: Check[];
}
export interface Preview {
  total: number;
  warnings: string[];
}
export type Finish = { finished: true } | {
  finished: false;
  written: false;
  reason: string;
  environment_names: string[];
  placeholders: string[];
  env: string;
  compose: string;
  certificate: string | null;
};

function bad(): never {
  throw new ApiError(0, "bad_response", "The setup answer could not be used.");
}
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return bad();
  return value as Record<string, unknown>;
}
function text(value: unknown): string {
  return typeof value === "string" ? value : bad();
}
function flag(value: unknown): boolean {
  return typeof value === "boolean" ? value : bad();
}
function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.map(text) : bad();
}
export function parseSetupStatus(value: unknown): SetupStatus {
  const fields = record(value), draft = record(fields["draft"]);
  const mode = fields["mode"], verify = draft["verify"], connected = draft["connection_ok"];
  if (mode !== "setup" && mode !== "admin" && mode !== null) return bad();
  if (verify !== "true" && verify !== "pin" && verify !== "false") return bad();
  if (connected !== null && typeof connected !== "boolean") return bad();
  const cert = draft["certificate"] === null ? null : record(draft["certificate"]);
  return {
    mode, unverified_phrase: text(fields["unverified_phrase"]),
    draft: {
      url: text(draft["url"]), site: text(draft["site"]), api_key_set: flag(draft["api_key_set"]),
      verify, connection_ok: connected, notify: texts(draft["notify"]),
      certificate: cert === null ? null : {
        fingerprint: text(cert["fingerprint"]), usable: flag(cert["usable"]),
        problem_message: text(cert["problem_message"]),
      },
    },
  };
}
function parseChecks(value: unknown): Check[] {
  if (!Array.isArray(value)) return bad();
  return value.map((item: unknown) => {
    const check = record(item);
    return { id: text(check["id"]), status: text(check["status"]), title: text(check["title"]),
      message: text(check["message"]), fix: text(check["fix"]) };
  });
}
function parseConnection(value: unknown): Connection {
  const fields = record(value), sites = fields["sites"];
  if (!Array.isArray(sites)) return bad();
  return { ok: flag(fields["ok"]), checks: parseChecks(fields["checks"]), sites: sites.map((item: unknown) => {
    const site = record(item);
    return { name: text(site["name"]), ref: text(site["ref"]), id: text(site["id"]) };
  }) };
}
function parsePreview(value: unknown): Preview {
  const fields = record(value), total = fields["total"];
  if (typeof total !== "number" || !Number.isSafeInteger(total) || total < 0) return bad();
  return { total, warnings: texts(fields["warnings"]) };
}
export function parseFinish(value: unknown): Finish {
  const fields = record(value);
  if (fields["finished"] === true) return { finished: true };
  if (fields["finished"] !== false || fields["written"] !== false) return bad();
  return { finished: false, written: false, reason: text(fields["reason"]),
    environment_names: texts(fields["environment_names"]), placeholders: texts(fields["placeholders"]),
    env: text(fields["env"]), compose: text(fields["compose"]),
    certificate: fields["certificate"] === null ? null : text(fields["certificate"]) };
}

/** No query/mutation hooks: request bodies and setup credentials must not enter their caches. */
export function setupApi(client: ApiClient, token?: string) {
  const options = { setupToken: token };
  return {
    status: () => client.get("/setup/status", { ...options, parse: parseSetupStatus }),
    draft: (body: DraftInput) => client.post("/setup/draft", { ...options, body, parse: parseSetupStatus }),
    certificate: () => client.post("/setup/certificate", { ...options, parse: parseSetupStatus }),
    connection: () => client.post("/setup/connection", { ...options, parse: parseConnection }),
    preview: () => client.post("/setup/preview", { ...options, parse: parsePreview }),
    notifications: () => client.post("/setup/notifications", { ...options, parse: (value) => parseChecks(record(value)["checks"]) }),
    finish: (body: { username?: string; password?: string }) => client.post("/setup/finish", { ...options, body, parse: parseFinish }),
  };
}
