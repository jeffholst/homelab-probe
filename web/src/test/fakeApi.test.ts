import { describe, expect, it } from "vitest";

import golden from "../../../tests/golden/openapi.json";
import { FAKE_ORIGIN, FakeApi, inContract } from "./fakeApi";

const post = (fake: FakeApi, path: string, body?: unknown, headers: Record<string, string> = {}) =>
  fake.fetch(`/api/v1${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });

const login = async (fake: FakeApi, password = "correct horse") =>
  post(fake, "/auth/login", { username: "demo", password });

describe("the fake API follows the real server's contract", () => {
  it("serves only routes that are in the OpenAPI snapshot, with the methods the snapshot gives them", () => {
    const fake = new FakeApi();
    expect(fake.served().length).toBeGreaterThan(0);
    for (const served of fake.served()) {
      const [method = "", path = ""] = served.split(" ");
      expect(Object.keys((golden.paths as Record<string, object>)[path] ?? {})).toContain(method.toLowerCase());
    }
  });

  it("refuses to serve a route the real server does not have", () => {
    expect(inContract("GET", "/meta")).toBe(true);
    expect(inContract("GET", "/no-such-route")).toBe(false);
    expect(() => new FakeApi().route("GET", "/no-such-route", () => ({}))).toThrow(/not in tests\/golden\/openapi.json/);
    expect(() => new FakeApi().route("DELETE", "/meta", () => ({}))).toThrow();
  });
});

describe("the fake API behaves like the real one", () => {
  it("answers /meta without a session", async () => {
    const response = await new FakeApi().fetch("/api/v1/meta", { credentials: "include" });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ login_required: true, needs_setup: false });
  });

  it("answers 401 not_logged_in to a route that is not public, before anything else", async () => {
    const fake = new FakeApi();
    const response = await fake.fetch("/api/v1/auth/me", { credentials: "include" });
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "not_logged_in", message: "Log in first." });
  });

  it("logs in with the session's CSRF token, and answers me with the same token", async () => {
    const fake = new FakeApi();
    const response = await login(fake);
    const session = (await response.json()) as Record<string, unknown>;
    expect(Object.keys(session).sort()).toEqual(["csrf_token", "idle_seconds_left", "role", "session_seconds_left", "username"]);
    const me = await (await fake.fetch("/api/v1/auth/me", { credentials: "include" })).json();
    expect(me).toMatchObject({ username: "demo", csrf_token: session["csrf_token"] });
  });

  it("gives the same 401 for a wrong password, an unknown user and a disabled one", async () => {
    const fake = new FakeApi({ accounts: [{ username: "off", password: "pw-pw-pw", role: "viewer", disabled: true }] });
    const bodies = [];
    for (const credentials of [
      { username: "off", password: "pw-pw-pw" },
      { username: "nobody", password: "x" },
    ]) {
      const response = await post(fake, "/auth/login", credentials);
      expect(response.status).toBe(401);
      bodies.push(await response.json());
    }
    expect(bodies[0]).toEqual({ error: "invalid_credentials", message: "Invalid username or password." });
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("answers 422 invalid_parameter to a login body that is not {username, password}", async () => {
    const fake = new FakeApi();
    const response = await post(fake, "/auth/login", { username: "demo" });
    expect(response.status).toBe(422);
    expect(await response.json()).toEqual({ error: "invalid_parameter", message: "A parameter is not valid." });
  });

  it("refuses an unsafe request from another origin (403 csrf_origin) before it looks at the session", async () => {
    const fake = new FakeApi();
    fake.crossSite = true;
    const response = await login(fake);
    expect(response.status).toBe(403);
    expect((await response.json()).error).toBe("csrf_origin");
  });

  it("refuses an unsafe request whose body is not JSON (415)", async () => {
    const fake = new FakeApi();
    const response = await fake.fetch("/api/v1/auth/login", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "text/plain" },
      body: "username=demo",
    });
    expect(response.status).toBe(415);
    expect((await response.json()).error).toBe("unsupported_media_type");
  });

  it("needs the CSRF token on logout (403 csrf_token) and ends the session when it is right", async () => {
    const fake = new FakeApi();
    const session = (await (await login(fake)).json()) as { csrf_token: string };
    const refused = await post(fake, "/auth/logout");
    expect(refused.status).toBe(403);
    expect((await refused.json()).error).toBe("csrf_token");
    expect((await post(fake, "/auth/logout", undefined, { "X-CSRF-Token": "wrong" })).status).toBe(403);
    expect((await post(fake, "/auth/logout", undefined, { "X-CSRF-Token": session.csrf_token })).status).toBe(200);
    expect((await fake.fetch("/api/v1/auth/me", { credentials: "include" })).status).toBe(401);
  });

  it("does not send the cookie when the request says credentials: omit", async () => {
    const fake = new FakeApi();
    await login(fake);
    expect((await fake.fetch("/api/v1/auth/me", { credentials: "omit" })).status).toBe(401);
  });

  it("throttles guessing: three free failures, then 429 with Retry-After for every password, right or wrong", async () => {
    let now = 1_000_000;
    const fake = new FakeApi({ now: () => now });
    for (let attempt = 0; attempt < 3; attempt += 1) expect((await login(fake, "wrong")).status).toBe(401);
    expect((await login(fake, "wrong")).status).toBe(401); // the fourth failure is the first that costs a wait
    const waiting = await login(fake);
    expect(waiting.status).toBe(429);
    expect(waiting.headers.get("Retry-After")).toBe("2");
    expect(await waiting.json()).toEqual({
      error: "too_many_attempts",
      message: "Too many attempts. Try again in 2 seconds.",
      retry_after: 2,
    });
    now += 2100;
    expect((await login(fake)).status).toBe(200); // the wait ended and a success clears the counts
  });

  it("ends a session after the idle time, and a new login replaces the previous session", async () => {
    let now = 5_000_000;
    const fake = new FakeApi({ now: () => now, idleSeconds: 60 });
    await login(fake);
    now += 61_000;
    expect((await fake.fetch("/api/v1/auth/me", { credentials: "include" })).status).toBe(401);
    const first = (await (await login(fake)).json()) as { csrf_token: string };
    const second = (await (await login(fake)).json()) as { csrf_token: string };
    expect(second.csrf_token).not.toBe(first.csrf_token);
  });

  it("answers an unknown path with FastAPI's own 404, which is not the API's error shape", async () => {
    const response = await new FakeApi().fetch("/api/v1/nothing", { credentials: "include" });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ detail: "Not Found" });
  });

  it("checks the role: a viewer may not use a route that wants an administrator", async () => {
    const fake = new FakeApi();
    fake.route("GET", "/users", () => [], { role: "admin" });
    const viewer = (await (await post(fake, "/auth/login", { username: "viewer", password: "viewer pass" })).json()) as {
      role: string;
    };
    expect(viewer.role).toBe("viewer");
    const response = await fake.fetch("/api/v1/users", { credentials: "include" });
    expect(response.status).toBe(403);
    expect((await response.json()).error).toBe("forbidden");
  });

  it("uses an origin that a browser on this page would send", () => {
    expect(FAKE_ORIGIN).toBe("http://fake.test");
  });
});
