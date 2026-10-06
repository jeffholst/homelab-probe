import { describe, expect, it, vi } from "vitest";

import { FakeApi } from "../test/fakeApi";
import { CSRF_HEADER, createApiClient, segment } from "./client";
import { createApi } from "./endpoints";
import { ApiError, errorFromResponse, isApiError } from "./errors";

function setup(fake = new FakeApi(), onUnauthorized = vi.fn()) {
  const client = createApiClient({ fetch: fake.fetch, onUnauthorized });
  return { fake, client, api: createApi(client), onUnauthorized };
}

describe("the API client", () => {
  it("sends the session cookie (credentials: include) and asks for JSON", async () => {
    const seen: RequestInit[] = [];
    const client = createApiClient({
      fetch: (_url, init) => {
        seen.push(init ?? {});
        return Promise.resolve(new Response("{}", { status: 200 }));
      },
    });
    await client.get("/meta");
    expect(seen[0]?.credentials).toBe("include");
    expect(new Headers(seen[0]?.headers).get("Accept")).toBe("application/json");
  });

  it("holds the CSRF token from the login in memory and sends it on unsafe requests only", async () => {
    const { fake, api, client } = setup();
    await api.login("demo", "correct horse");
    expect(client.hasCsrfToken()).toBe(true);
    await api.me();
    await api.logout();
    const byMethod = fake.calls.map((call) => `${call.method} ${call.path} ${call.csrf ?? "-"}`);
    expect(byMethod[0]).toBe("POST /auth/login -");
    expect(byMethod[1]).toBe("GET /auth/me -"); // a GET never carries the token
    expect(byMethod[2]).toMatch(/^POST \/auth\/logout csrf-/);
    expect(client.hasCsrfToken()).toBe(false);
  });

  it("never writes the token (or anything else) to browser storage or a cookie", async () => {
    const { api } = setup();
    await api.login("demo", "correct horse");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("restores the CSRF token from /auth/me, as after a page reload", async () => {
    const fake = new FakeApi();
    await setup(fake).api.login("demo", "correct horse");
    const reloaded = setup(fake); // a new page: a new client with no token
    expect(reloaded.client.hasCsrfToken()).toBe(false);
    expect((await reloaded.api.me())?.username).toBe("demo");
    expect(reloaded.client.hasCsrfToken()).toBe(true);
    await reloaded.api.logout(); // would be a 403 csrf_token without the restored token
    expect((await reloaded.api.me())).toBeNull();
  });

  it("turns an error answer into an ApiError with the server's code and sentence", async () => {
    const { api } = setup();
    const error = await api.login("demo", "wrong").catch((caught: unknown) => caught);
    expect(isApiError(error)).toBe(true);
    expect(error).toMatchObject({ status: 401, code: "invalid_credentials", message: "Invalid username or password." });
  });

  it("does not treat the login's own 401 as an ended session, but does for any other 401", async () => {
    const { api, onUnauthorized } = setup();
    await api.login("demo", "wrong").catch(() => undefined);
    expect(onUnauthorized).not.toHaveBeenCalled();
    await expect(api.platforms()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("drops the token on a 401, so a later request cannot send a dead one", async () => {
    const { fake, api, client } = setup();
    await api.login("demo", "correct horse");
    fake.restartServer();
    await expect(api.platforms()).rejects.toMatchObject({ status: 401 });
    expect(client.hasCsrfToken()).toBe(false);
  });

  it("reads Retry-After and retry_after of a 429", async () => {
    const { fake, api } = setup();
    for (let attempt = 0; attempt < 4; attempt += 1) await api.login("demo", "wrong").catch(() => undefined);
    const error = await api.login("demo", "correct horse").catch((caught: unknown) => caught);
    expect(error).toMatchObject({ status: 429, code: "too_many_attempts", retryAfter: 2 });
    expect(fake.calls.length).toBe(5);
  });

  it("reports an unreachable server as status 0 with a fixed sentence, not the text of the failure", async () => {
    const client = createApiClient({ fetch: () => Promise.reject(new TypeError("connect ECONNREFUSED 10.1.2.3:443")) });
    const error = await client.get("/meta").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, code: "network_error", message: "The server could not be reached." });
    expect(String((error as Error).message)).not.toContain("10.1.2.3");
  });

  it("lets an abort through unchanged, so a cancelled query is not shown as an error", async () => {
    const abort = new DOMException("aborted", "AbortError");
    const client = createApiClient({ fetch: () => Promise.reject(abort) });
    await expect(client.get("/meta")).rejects.toBe(abort);
  });

  it("uses a fixed sentence when the error body is not the API's {error, message} (a proxy's page, an empty body)", async () => {
    const answers = [new Response("<html>Bad gateway</html>", { status: 502 }), new Response("", { status: 503 })];
    const client = createApiClient({ fetch: () => Promise.resolve(answers.shift() as Response) });
    const first = await client.get("/meta").catch((caught: unknown) => caught);
    expect(first).toMatchObject({ status: 502, code: "bad_response", message: "The server could not read the controller." });
    expect(String((first as Error).message)).not.toContain("html");
    expect(await client.get("/meta").catch((caught: unknown) => caught)).toMatchObject({ status: 503, code: "bad_response" });
  });

  it("refuses an answer that is not what the app expects instead of passing it on", async () => {
    const client = createApiClient({ fetch: () => Promise.resolve(new Response('{"version": 3}', { status: 200 })) });
    await expect(createApi(client).meta()).rejects.toMatchObject({ code: "bad_response" });
  });

  it("builds the query string, repeating a key for a list and skipping undefined", async () => {
    let requested = "";
    const client = createApiClient({
      fetch: (url) => {
        requested = String(url);
        return Promise.resolve(new Response("{}"));
      },
    });
    await client.get("/x", { query: { only: ["wan", "wifi"], days: 7, empty: undefined, flag: true } });
    expect(requested).toBe("/api/v1/x?only=wan&only=wifi&days=7&flag=true");
  });

  it("sends a body as JSON and only then sets the content type", async () => {
    const seen: RequestInit[] = [];
    const client = createApiClient({
      fetch: (_url, init) => {
        seen.push(init ?? {});
        return Promise.resolve(new Response("{}"));
      },
    });
    await client.post("/x", { body: { a: 1 } });
    await client.post("/y");
    expect(seen[0]?.body).toBe('{"a":1}');
    expect(new Headers(seen[0]?.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(seen[1]?.headers).has("Content-Type")).toBe(false);
  });

  it("encodes a path segment", () => {
    expect(segment("a/b c?")).toBe("a%2Fb%20c%3F");
    expect(CSRF_HEADER).toBe("X-CSRF-Token");
  });
});

describe("errorFromResponse", () => {
  it("keeps the extra fields of the body as details, untrusted", () => {
    const error = errorFromResponse(409, null, { error: "client_ambiguous", message: "More than one.", candidates: ["a"] });
    expect(error.details).toEqual({ candidates: ["a"] });
  });

  it("ignores a Retry-After that is not a positive number", () => {
    expect(errorFromResponse(429, "soon", { error: "e", message: "m" }).retryAfter).toBeUndefined();
    expect(errorFromResponse(429, "0", { error: "e", message: "m" }).retryAfter).toBeUndefined();
    expect(errorFromResponse(429, "1.2", { error: "e", message: "m" }).retryAfter).toBe(2);
  });

  it("does not trust a body whose error or message is not text", () => {
    expect(errorFromResponse(500, null, { error: 1, message: {} }).code).toBe("bad_response");
    expect(errorFromResponse(500, null, [1]).code).toBe("bad_response");
    expect(errorFromResponse(418, null, null).message).toBe("The server answered with something unexpected.");
  });
});
