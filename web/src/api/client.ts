/**
 * A typed `fetch` client for the server's API (`/api/v1`).
 *
 * - **Same origin, with the session cookie** (`credentials: "include"`). The browser adds the `Origin` header of an
 *   unsafe request by itself; the server refuses one that does not name its own host, so nothing is configured here.
 * - **CSRF.** The login and `GET /auth/me` answers carry a token; it is held in this object (memory only, never in
 *   `localStorage`, a cookie or the URL) and sent as `X-CSRF-Token` on every `POST`, `PUT`, `PATCH` and `DELETE`.
 *   A reload loses it and `GET /auth/me` brings it back, which is the point: it lives as long as the page.
 * - **One error type.** Every failure is an `ApiError` built from the server's `{error, message}` (or, for an answer
 *   that is not that, a fixed sentence by status): the text of a response body is never shown unchecked.
 * - **401** (not logged in, or the session ended) clears the token and calls `onUnauthorized`, which sends the user
 *   to the login page. The login's own 401 (wrong password) and a wrong setup token (`invalid_setup_token`, which says
 *   nothing about a session) are ordinary errors.
 * - **Extra headers** are for the setup token (`X-Setup-Token`) only; the caller holds it in memory, like the CSRF
 *   token.
 */
import { ApiError, errorFromResponse } from "./errors";

export const API_PREFIX = "/api/v1";
export const CSRF_HEADER = "X-CSRF-Token";
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
export const LOGIN_PATH = "/auth/login";
export const SETUP_TOKEN_HEADER = "X-Setup-Token";
const SETUP_TOKEN_REFUSED = "invalid_setup_token";

type QueryValue = string | number | boolean | undefined;
export type Query = Readonly<Record<string, QueryValue | readonly QueryValue[]>>;

export interface RequestOptions<T> {
  query?: Query;
  /** Sent as JSON. */
  body?: unknown;
  signal?: AbortSignal | undefined;
  /** Sent as they are (the setup token); never `Content-Type`, `Accept` or the CSRF header, which the client sets. */
  headers?: Readonly<Record<string, string>>;
  /** Checks and converts the parsed JSON; without it the answer is taken as `T` (the report documents). */
  parse?: (value: unknown) => T;
}

export interface ApiClientOptions {
  /** Defaults to the global `fetch`; tests pass the fake API's. */
  fetch?: typeof fetch;
  /** Called after a 401 from anything but the login. */
  onUnauthorized?: () => void;
}

export interface ApiClient {
  get<T = unknown>(path: string, options?: RequestOptions<T>): Promise<T>;
  post<T = unknown>(path: string, options?: RequestOptions<T>): Promise<T>;
  put<T = unknown>(path: string, options?: RequestOptions<T>): Promise<T>;
  patch<T = unknown>(path: string, options?: RequestOptions<T>): Promise<T>;
  delete<T = unknown>(path: string, options?: RequestOptions<T>): Promise<T>;
  setCsrfToken(token: string): void;
  clearCsrfToken(): void;
  hasCsrfToken(): boolean;
}

/** One path segment (a site, a MAC address, a username) made safe to put in a URL path. */
export function segment(value: string): string {
  return encodeURIComponent(value);
}

function queryString(query: Query | undefined): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    const values: readonly QueryValue[] = Array.isArray(value) ? (value as readonly QueryValue[]) : [value as QueryValue];
    for (const item of values) if (item !== undefined) params.append(key, String(item));
  }
  const text = params.toString();
  return text === "" ? "" : `?${text}`;
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  let csrfToken: string | null = null;

  async function request<T>(method: string, path: string, settings: RequestOptions<T> = {}): Promise<T> {
    const headers = new Headers(settings.headers);
    headers.set("Accept", "application/json");
    const init: RequestInit = { method, headers, credentials: "include", cache: "no-store" };
    if (settings.signal) init.signal = settings.signal;
    if (settings.body !== undefined) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(settings.body);
    }
    if (UNSAFE_METHODS.has(method) && csrfToken !== null) headers.set(CSRF_HEADER, csrfToken);

    let response: Response;
    try {
      response = await (options.fetch ?? fetch)(`${API_PREFIX}${path}${queryString(settings.query)}`, init);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError(0, "network_error", "The server could not be reached.");
    }

    const body = await readBody(response);
    if (!response.ok) {
      const failure = errorFromResponse(response.status, response.headers.get("Retry-After"), body);
      if (response.status === 401 && path !== LOGIN_PATH && failure.code !== SETUP_TOKEN_REFUSED) {
        csrfToken = null;
        options.onUnauthorized?.();
      }
      throw failure;
    }
    return settings.parse ? settings.parse(body) : (body as T);
  }

  return {
    get: (path, settings) => request("GET", path, settings),
    post: (path, settings) => request("POST", path, settings),
    put: (path, settings) => request("PUT", path, settings),
    patch: (path, settings) => request("PATCH", path, settings),
    delete: (path, settings) => request("DELETE", path, settings),
    setCsrfToken(token) {
      csrfToken = token;
    },
    clearCsrfToken() {
      csrfToken = null;
    },
    hasCsrfToken: () => csrfToken !== null,
  };
}
