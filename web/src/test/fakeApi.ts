/**
 * A fake of the server's API for Vitest: a `fetch` that answers like the real server does, so a component test fails
 * for the same reasons a real session would.
 *
 * What it copies from homelab_probe/server (and the tests of `fakeApi.test.ts` pin):
 *
 * - **The contract.** Every route it serves must exist, with that method, in `tests/golden/openapi.json` (the OpenAPI
 *   snapshot of the real server); registering one that does not throws.
 * - **CSRF, in the server's order.** An unsafe request is refused first when its `Origin` is not the host
 *   (403 `csrf_origin`) or its body is not JSON (415 `unsupported_media_type`); then, for a route that is not public,
 *   with no session 401 `not_logged_in`, with too small a role 403 `forbidden`, and with a missing or wrong
 *   `X-CSRF-Token` 403 `csrf_token`.
 * - **Login.** The same 401 for a wrong password, an unknown user and a disabled one, `422 invalid_parameter` for a
 *   body that is not `{username, password}`, and the throttle: three free failures, then 2, 4, 8 ... seconds (a
 *   username at most 30) with `429 too_many_attempts`, `Retry-After` and `retry_after`, whatever password is sent.
 * - **Sessions.** An idle timeout and an absolute one (checked on every request, counted by an injectable clock), a new
 *   login ends the one it replaces, and the cookie is only sent and kept when the request says `credentials: "include"`.
 * - **Errors** are `{error, message}` with fixed sentences.
 * - **The setup mode** (`setupToken`, see fakeSetup.ts): while `meta.needs_setup` is true every route that is neither
 *   public nor a setup route answers 503 `not_configured`; a setup route wants the `X-Setup-Token` while no enabled
 *   administrator exists (401 `invalid_setup_token`, and the login's throttle under its own key) and an administrator's
 *   session with the CSRF token once one does.
 *
 * The report documents are synthetic (fakeReports.ts): the site list and the dashboard are served by default
 * (`sites`, `dashboard`, `reportWarnings`); a page issue adds the other routes it reads with `fake.route(...)`.
 */
import golden from "../../../tests/golden/openapi.json";
import type { Dashboard } from "../api/reports";
import type { Meta, Platform, Role } from "../api/types";
import { FAKE_SITES, dashboardFixture } from "./fakeReports";

export interface FakeAccount {
  username: string;
  password: string;
  role: Role;
  disabled?: boolean;
}

export interface FakeApiOptions {
  accounts?: FakeAccount[];
  meta?: Partial<Meta>;
  platforms?: Platform[];
  /** Milliseconds since the epoch; tests move it to age a session or a throttle. */
  now?: () => number;
  idleSeconds?: number;
  maxSeconds?: number;
}

export interface RouteRequest {
  method: string;
  /** The path after /api/v1, with the placeholders of the template filled in `params`. */
  path: string;
  params: Record<string, string>;
  query: URLSearchParams;
  body: unknown;
  session: FakeSession | null;
}

export interface FakeSession {
  id: string;
  username: string;
  role: Role;
  csrf: string;
  createdAt: number;
  lastSeen: number;
}

type Handler = (request: RouteRequest) => Response | object | unknown[];
interface Route {
  method: string;
  template: string;
  pattern: RegExp;
  names: string[];
  handler: Handler;
  public: boolean;
  setup: boolean;
  role: Role;
}

const HOST = "fake.test";
export const FAKE_ORIGIN = `http://${HOST}`;
const API = "/api/v1";
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const RANK: Record<Role, number> = { viewer: 1, admin: 2 };

export const DEFAULT_META: Meta = {
  version: "0.0.0-test",
  needs_setup: false,
  setup_mode: null,
  login_required: true,
  demo: false,
  read_only: false,
  https: false,
  loopback: true,
};

const goldenPaths = (golden as { paths: Record<string, Record<string, unknown>> }).paths;

/** True when the real server's OpenAPI document has this route. */
export function inContract(method: string, template: string): boolean {
  return goldenPaths[`${API}${template}`]?.[method.toLowerCase()] !== undefined;
}

export class ApiRefused extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly extra: Record<string, unknown> = {},
    readonly headers: Record<string, string> = {},
  ) {
    super(message);
  }
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers },
  });
}

export class FakeApi {
  readonly calls: { method: string; path: string; csrf: string | null }[] = [];
  readonly meta: Meta;
  platforms: Platform[];
  /** When set, the next request to a path that starts with this is answered with this status and error. */
  private forced: { prefix: string; status: number; code: string; message: string; times: number }[] = [];
  private readonly accounts: FakeAccount[];
  private readonly sessions = new Map<string, FakeSession>();
  private readonly failures = new Map<string, { count: number; until: number; last: number }>();
  private readonly routes: Route[] = [];
  private cookie: string | null = null;
  private counter = 0;
  private readonly clock: () => number;
  private readonly idleMs: number;
  private readonly maxMs: number;
  /** Simulates a page of another site posting to this server: its Origin is not ours. */
  crossSite = false;
  /** The controller's sites and the dashboard each site answers (a site not in `sites` is a 404). */
  sites: { name: string; ref: string; id: string }[] = FAKE_SITES.map((site) => ({ ...site }));
  dashboard: Dashboard = dashboardFixture();
  /** The `warnings` of every report answer. */
  reportWarnings: string[] = [];
  /** The `refresh` parameter of every dashboard request, in order. */
  readonly dashboardRefresh: boolean[] = [];
  /** The setup token of a server in a setup mode (fakeSetup.ts sets it). */
  setupToken: string | null = null;

  constructor(options: FakeApiOptions = {}) {
    this.accounts = [
      ...(options.accounts ?? [
        { username: "demo", password: "correct horse", role: "admin" },
        { username: "viewer", password: "viewer pass", role: "viewer" },
      ]),
    ];
    this.meta = { ...DEFAULT_META, ...options.meta };
    this.platforms = options.platforms ?? [{ id: "unifi", name: "UniFi", configured: true }];
    this.clock = options.now ?? (() => Date.now());
    this.idleMs = (options.idleSeconds ?? 30 * 60) * 1000;
    this.maxMs = (options.maxSeconds ?? 12 * 3600) * 1000;

    this.route("GET", "/meta", () => this.meta, { public: true });
    this.route("POST", "/auth/login", (request) => this.login(request), { public: true });
    this.route("POST", "/auth/logout", (request) => this.logout(request));
    this.route("GET", "/auth/me", (request) => this.me(request.session));
    this.route("GET", "/platforms", () => this.platforms);
    this.route("GET", "/unifi/sites", () => ({ application: { applicationVersion: "10.0.0" }, sites: this.sites, ...this.envelope() }));
    this.route("GET", "/unifi/sites/{site}/dashboard", (request) => {
      if (!this.sites.some((site) => site.ref === request.params["site"] || site.id === request.params["site"])) {
        throw new ApiRefused(404, "site_not_found", "The controller has no such site.");
      }
      this.dashboardRefresh.push(request.query.get("refresh") === "true");
      return { ...this.dashboard, ...this.envelope() };
    });
  }

  /** Registers a route, which must be in the OpenAPI snapshot. A later registration of the same route wins. */
  route(method: string, template: string, handler: Handler, options: { public?: boolean; role?: Role; setup?: boolean } = {}): void {
    if (!inContract(method, template)) {
      throw new Error(`${method} ${API}${template} is not in tests/golden/openapi.json: the fake may not serve it`);
    }
    const names: string[] = [];
    const source = template.replace(/[.*+?^${}()|[\]\\]/g, (char) => (char === "{" || char === "}" ? char : `\\${char}`));
    const pattern = new RegExp(
      `^${source.replace(/\{(\w+)\}/g, (_, name: string) => {
        names.push(name);
        return "([^/]+)";
      })}$`,
    );
    this.routes.unshift({
      method,
      template,
      pattern,
      names,
      handler,
      public: options.public ?? false,
      setup: options.setup ?? false,
      role: options.setup === true ? "admin" : (options.role ?? "viewer"),
    });
  }

  private envelope(): { generated_at: string; warnings: string[] } {
    return { generated_at: new Date(this.clock()).toISOString().replace(/\.\d{3}Z$/, "Z"), warnings: [...this.reportWarnings] };
  }

  /** Answers the next `times` requests whose path starts with `prefix` with this error (a server failure to react to). */
  failWith(prefix: string, status: number, code: string, message: string, times = 1): void {
    this.forced.push({ prefix, status, code, message, times });
  }

  /** The routes this fake serves, as `METHOD /api/v1/template`. */
  served(): string[] {
    return this.routes.map((route) => `${route.method} ${API}${route.template}`);
  }

  /** Ends every session, as a restart of the server does. */
  restartServer(): void {
    this.sessions.clear();
  }

  /** Is there an enabled administrator (the setup token stops working once there is)? */
  hasAdministrator(): boolean {
    return this.accounts.some((account) => account.role === "admin" && account.disabled !== true);
  }

  addAccount(account: FakeAccount): void {
    this.accounts.push(account);
  }

  /** Replaces every account (a restore), ending every session. */
  replaceAccounts(accounts: FakeAccount[]): void {
    this.accounts.splice(0, this.accounts.length, ...accounts);
    this.sessions.clear();
  }

  /** The CSRF token of the current session, or null. */
  currentCsrf(): string | null {
    return this.currentSession()?.csrf ?? null;
  }

  readonly fetch: typeof fetch = (input, init) => {
    try {
      return Promise.resolve(this.handle(input, init));
    } catch (error) {
      if (error instanceof ApiRefused) {
        return Promise.resolve(json(error.status, { error: error.code, message: error.message, ...error.extra }, error.headers));
      }
      throw error;
    }
  };

  // -- the request ----------------------------------------------------------------------------------------------

  private handle(input: RequestInfo | URL, init: RequestInit | undefined): Response {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, FAKE_ORIGIN);
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    const withCookies = init?.credentials === "include" || init?.credentials === "same-origin" || init?.credentials === undefined;
    if (!url.pathname.startsWith(`${API}/`)) return json(404, { detail: "Not Found" });
    const path = url.pathname.slice(API.length);
    this.calls.push({ method, path, csrf: headers.get("X-CSRF-Token") });

    // The origin guard (a middleware of the real server) comes before anything else.
    if (UNSAFE.has(method)) {
      const origin = this.crossSite ? "http://other.example" : FAKE_ORIGIN; // a browser adds it by itself
      if (origin !== FAKE_ORIGIN) throw new ApiRefused(403, "csrf_origin", "The request does not come from this site.");
      const body = init?.body;
      if (body !== undefined && body !== null && headers.get("Content-Type")?.split(";")[0]?.trim().toLowerCase() !== "application/json") {
        throw new ApiRefused(415, "unsupported_media_type", "The body must be JSON.");
      }
    }

    const matched = this.match(method, path);
    if (matched === null) return json(404, { detail: "Not Found" });
    const { route, params } = matched;

    // A refusal that comes before the login check on the real server (not set up, restoring, a proxy's failure).
    const forced = this.forced.find((entry) => path.startsWith(entry.prefix) && entry.times > 0);
    if (forced) {
      forced.times -= 1;
      throw new ApiRefused(forced.status, forced.code, forced.message);
    }

    let session: FakeSession | null = null;
    if (!route.public && !route.setup && this.meta.needs_setup) {
      throw new ApiRefused(503, "not_configured", "The server is not set up yet: finish the setup first.");
    }
    if (route.setup && this.meta.needs_setup && !this.hasAdministrator()) {
      this.checkSetupToken(headers.get("X-Setup-Token"));
    } else if (!route.public) {
      session = withCookies ? this.currentSession() : null;
      if (session === null) throw new ApiRefused(401, "not_logged_in", "Log in first.");
      if (RANK[session.role] < RANK[route.role]) throw new ApiRefused(403, "forbidden", "Your role may not do this.");
      if (UNSAFE.has(method) && headers.get("X-CSRF-Token") !== session.csrf) {
        throw new ApiRefused(403, "csrf_token", "The CSRF token is missing or wrong.");
      }
      session.lastSeen = this.clock();
    }

    let body: unknown;
    if (typeof init?.body === "string" && init.body !== "") {
      try {
        body = JSON.parse(init.body);
      } catch {
        throw new ApiRefused(422, "invalid_parameter", "A parameter is not valid.");
      }
    }
    const result = route.handler({ method, path, params, query: url.searchParams, body, session });
    return result instanceof Response ? result : json(200, result);
  }

  private checkSetupToken(supplied: string | null): void {
    const wait = Math.max(0, (this.failures.get("setup")?.until ?? 0) - this.clock());
    if (wait > 0) {
      const seconds = Math.ceil(wait / 1000);
      throw new ApiRefused(429, "too_many_attempts", `Too many attempts. Try again in ${seconds} seconds.`, { retry_after: seconds }, {
        "Retry-After": String(seconds),
      });
    }
    if (this.setupToken === null || supplied !== this.setupToken) {
      this.fail("setup", 300);
      throw new ApiRefused(401, "invalid_setup_token", "The setup token is missing or wrong.");
    }
    this.failures.delete("setup");
  }

  private match(method: string, path: string): { route: Route; params: Record<string, string> } | null {
    for (const route of this.routes) {
      if (route.method !== method) continue;
      const found = route.pattern.exec(path);
      if (found === null) continue;
      const params: Record<string, string> = {};
      route.names.forEach((name, index) => {
        params[name] = decodeURIComponent(found[index + 1] ?? "");
      });
      return { route, params };
    }
    return null;
  }

  private currentSession(): FakeSession | null {
    if (this.cookie === null) return null;
    const session = this.sessions.get(this.cookie);
    if (session === undefined) return null;
    const now = this.clock();
    const account = this.accounts.find((entry) => entry.username === session.username);
    if (
      now - session.lastSeen > this.idleMs ||
      now - session.createdAt > this.maxMs ||
      account === undefined ||
      account.disabled === true ||
      account.role !== session.role
    ) {
      this.sessions.delete(session.id);
      return null;
    }
    return session;
  }

  // -- the auth routes ------------------------------------------------------------------------------------------

  private me(session: FakeSession | null): object {
    if (session === null) throw new ApiRefused(401, "not_logged_in", "Log in first.");
    const now = this.clock();
    return {
      username: session.username,
      role: session.role,
      csrf_token: session.csrf,
      idle_seconds_left: Math.floor((this.idleMs - (now - session.lastSeen)) / 1000),
      session_seconds_left: Math.floor((this.maxMs - (now - session.createdAt)) / 1000),
      can_change_password: true,
    };
  }

  private waitFor(username: string): number {
    const now = this.clock();
    const until = Math.max(this.failures.get("address")?.until ?? 0, this.failures.get(`user:${username.toLowerCase()}`)?.until ?? 0);
    return Math.max(0, until - now);
  }

  private fail(key: string, capSeconds: number): void {
    const now = this.clock();
    const before = this.failures.get(key);
    const count = (before !== undefined && now - before.last < 900_000 ? before.count : 0) + 1;
    const extra = count - 3;
    const wait = extra > 0 ? Math.min(capSeconds, 2 * 2 ** (extra - 1)) : 0;
    this.failures.set(key, { count, until: now + wait * 1000, last: now });
  }

  private login(request: RouteRequest): object {
    const body = request.body;
    if (
      typeof body !== "object" ||
      body === null ||
      typeof (body as Record<string, unknown>)["username"] !== "string" ||
      typeof (body as Record<string, unknown>)["password"] !== "string"
    ) {
      throw new ApiRefused(422, "invalid_parameter", "A parameter is not valid.");
    }
    const { username, password } = body as { username: string; password: string };
    const wait = this.waitFor(username);
    if (wait > 0) {
      const seconds = Math.ceil(wait / 1000);
      throw new ApiRefused(429, "too_many_attempts", `Too many attempts. Try again in ${seconds} seconds.`, { retry_after: seconds }, {
        "Retry-After": String(seconds),
      });
    }
    const account = this.accounts.find((entry) => entry.username.toLowerCase() === username.toLowerCase());
    if (account === undefined || account.disabled === true || account.password !== password) {
      this.fail("address", 300);
      this.fail(`user:${username.toLowerCase()}`, 30);
      throw new ApiRefused(401, "invalid_credentials", "Invalid username or password.");
    }
    this.failures.delete("address");
    this.failures.delete(`user:${username.toLowerCase()}`);
    if (this.cookie !== null) this.sessions.delete(this.cookie);
    const now = this.clock();
    this.counter += 1;
    const session: FakeSession = {
      id: `session-${this.counter}`,
      username: account.username,
      role: account.role,
      csrf: `csrf-${this.counter}-${Math.floor(now)}`,
      createdAt: now,
      lastSeen: now,
    };
    this.sessions.set(session.id, session);
    this.cookie = session.id;
    return this.me(session);
  }

  private logout(request: RouteRequest): object {
    if (request.session !== null) this.sessions.delete(request.session.id);
    this.cookie = null;
    return { status: "ok" };
  }
}
