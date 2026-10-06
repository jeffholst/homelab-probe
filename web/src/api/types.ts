/**
 * The shapes of the server's own (non-report) answers: `/api/v1/meta`, `/api/v1/auth/*` and `/api/v1/platforms`.
 *
 * The OpenAPI document (tests/golden/openapi.json) leaves these answers as free-form objects, so they are written
 * here, checked at run time by the parsers below (an answer that does not fit is a `bad_response` error rather than a
 * value that fails somewhere else) and compared with a real server by the Playwright smoke. The report documents are
 * generated from docs/schemas instead (src/generated).
 */
import { ApiError } from "./errors";

export type Role = "viewer" | "admin";

/** What a client may know before it logs in (`GET /api/v1/meta`). */
export interface Meta {
  version: string;
  needs_setup: boolean;
  setup_mode: string | null;
  login_required: boolean;
  demo: boolean;
  read_only: boolean;
  /** This request came over HTTPS. */
  https: boolean;
  /** The server was reached by a loopback name (this machine). */
  loopback: boolean;
}

/** Who is logged in (`GET /api/v1/auth/me`), without the CSRF token, which the client keeps to itself. */
export interface Session {
  username: string;
  role: Role;
  idle_seconds_left: number;
  session_seconds_left: number;
  /** True for the local accounts, whose passwords are kept by the server (`POST /api/v1/auth/password`). */
  can_change_password: boolean;
}

export interface Platform {
  id: string;
  name: string;
  configured: boolean;
}

function bad(what: string): ApiError {
  return new ApiError(0, "bad_response", `The server's answer for ${what} was not what this app expects.`);
}

function record(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw bad(what);
  return value as Record<string, unknown>;
}

function text(fields: Record<string, unknown>, key: string, what: string): string {
  const value = fields[key];
  if (typeof value !== "string") throw bad(what);
  return value;
}

function flag(fields: Record<string, unknown>, key: string, what: string): boolean {
  const value = fields[key];
  if (typeof value !== "boolean") throw bad(what);
  return value;
}

function seconds(fields: Record<string, unknown>, key: string, what: string): number {
  const value = fields[key];
  if (typeof value !== "number" || !Number.isFinite(value)) throw bad(what);
  return value;
}

export function parseMeta(value: unknown): Meta {
  const fields = record(value, "meta");
  const mode = fields["setup_mode"];
  if (mode !== null && typeof mode !== "string") throw bad("meta");
  return {
    version: text(fields, "version", "meta"),
    needs_setup: flag(fields, "needs_setup", "meta"),
    setup_mode: mode ?? null,
    login_required: flag(fields, "login_required", "meta"),
    demo: flag(fields, "demo", "meta"),
    read_only: flag(fields, "read_only", "meta"),
    https: flag(fields, "https", "meta"),
    loopback: flag(fields, "loopback", "meta"),
  };
}

/** The session and the CSRF token of a login or `me` answer. */
export function parseSession(value: unknown): { session: Session; csrfToken: string } {
  const fields = record(value, "the session");
  const role = fields["role"];
  if (role !== "viewer" && role !== "admin") throw bad("the session");
  return {
    csrfToken: text(fields, "csrf_token", "the session"),
    session: {
      username: text(fields, "username", "the session"),
      role,
      idle_seconds_left: seconds(fields, "idle_seconds_left", "the session"),
      session_seconds_left: seconds(fields, "session_seconds_left", "the session"),
      can_change_password: flag(fields, "can_change_password", "the session"),
    },
  };
}

export function parsePlatforms(value: unknown): Platform[] {
  if (!Array.isArray(value)) throw bad("the platforms");
  return value.map((entry: unknown) => {
    const fields = record(entry, "the platforms");
    return {
      id: text(fields, "id", "the platforms"),
      name: text(fields, "name", "the platforms"),
      configured: flag(fields, "configured", "the platforms"),
    };
  });
}
