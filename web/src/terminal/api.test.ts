import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createApi } from "../api/endpoints";
import { ApiRefused, FakeApi } from "../test/fakeApi";
import fixture from "../test/fixtures/terminal.v1.json";
import { createTerminalBackend, parseCapabilities } from "./api";
import type { CompletionRequest } from "./backend";

async function setup() {
  const fake = new FakeApi();
  const unauthorized = vi.fn();
  const client = createApiClient({ fetch: fake.fetch, onUnauthorized: unauthorized });
  await createApi(client).login("viewer", "viewer pass");
  let next = 0;
  const backend = createTerminalBackend(client, { requestId: () => `request-${++next}` });
  fake.route("GET", "/terminal/capabilities", () => fixture.capabilities);
  return { fake, client, backend, unauthorized };
}

const complete: CompletionRequest = { type: "complete", seq: 1, revision: 2, argv: ["query", "cl"], tokenIndex: 1, cursor: 2 };

describe("the real terminal backend", () => {
  it("uses authenticated contract routes and exact JSON bodies with CSRF, no URL command or cached mutation", async () => {
    const { fake, backend } = await setup();
    const bodies: unknown[] = [];
    fake.route("POST", "/terminal/execute", ({ body }) => {
      bodies.push(body);
      return { ...fixture.executeResponse, requestId: "request-1", warnings: ["partial\u001b[2J"] };
    });
    fake.route("POST", "/terminal/complete", ({ body }) => {
      bodies.push(body);
      return { ...fixture.completionResponse, requestId: "request-2" };
    });
    expect(await backend.loadCapabilities()).toEqual(fixture.capabilities);
    expect(await backend.execute(["query", "clients", "--json"])).toMatchObject({ kind: "output", warnings: ["partial[2J"] });
    expect(await backend.complete?.(complete)).toEqual({ candidates: fixture.completionResponse.candidates, truncated: false });
    expect(bodies).toEqual([
      { argv: ["query", "clients", "--json"], requestId: "request-1" },
      { argv: ["query", "cl"], tokenIndex: 1, cursor: 2, requestId: "request-2" },
    ]);
    expect(fake.calls.slice(1).map((c) => [c.method, c.path, Boolean(c.csrf)])).toEqual([
      ["GET", "/terminal/capabilities", false], ["POST", "/terminal/execute", true], ["POST", "/terminal/complete", true],
    ]);
  });

  it.each(fixture.errors)("maps $status safely and never retries execution", async ({ status, body, ...rest }) => {
    const { fake, backend, unauthorized } = await setup();
    fake.route("POST", "/terminal/execute", () => { throw new ApiRefused(status, body.error, body.message, body, "headers" in rest ? rest.headers : {}); });
    const outcome = await backend.execute(["query", "clients"]);
    expect(outcome).toMatchObject({ kind: "failed", ran: status === 504 ? "unknown" : "no" });
    if (status === 429 && outcome.kind === "failed") expect(outcome.message).toContain("Wait 1 second");
    expect(fake.calls.filter((c) => c.path === "/terminal/execute")).toHaveLength(1);
    expect(unauthorized).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
  });

  it("reports a dispatched failure as failed, not did-not-run", async () => {
    const { fake, backend } = await setup();
    fake.route("POST", "/terminal/execute", () => { throw new ApiRefused(502, "controller_failed", "Failed.\u001b[2J", { executionStatus: "failed" }); });
    expect(await backend.execute(["query", "clients"])).toEqual({ kind: "failed", ran: "failed", message: "Failed.[2J" });
  });

  it.each([
    { requestId: "other", output: "must not appear" }, { correlationId: "" }, { status: "running" },
    { warnings: [1] }, { truncated: "true" }, { output: null }, { output: "x".repeat(262145) },
  ])("rejects malformed or mismatched execution replies", async (patch) => {
    const { fake, backend } = await setup();
    fake.route("POST", "/terminal/execute", () => ({ ...fixture.executeResponse, requestId: "request-1", ...patch }));
    const outcome = await backend.execute(["query", "clients"]);
    expect(outcome).toMatchObject({ kind: "failed", ran: "unknown" });
    expect(JSON.stringify(outcome)).not.toContain("must not appear");
  });

  it("flags truncation and cleans hostile output before it reaches the panel", async () => {
    const { fake, backend } = await setup();
    fake.route("POST", "/terminal/execute", () => ({ ...fixture.executeResponse, requestId: "request-1", output: "safe\u001b]52;c;secret\u0007\u202eevil", truncated: true }));
    expect(await backend.execute(["query", "clients"])).toMatchObject({ kind: "output", text: "safe]52;c;secretevil", truncated: true });
  });

  it.each([{ requestId: "wrong" }, { tokenIndex: 0 }, { candidates: [{ label: "x", kind: "file", description: "" }] }, { candidates: Array(65).fill(fixture.completionResponse.candidates[0]) }])(
    "rejects malformed or mismatched completion replies", async (patch) => {
      const { fake, backend } = await setup();
      fake.route("POST", "/terminal/complete", () => ({ ...fixture.completionResponse, requestId: "request-1", ...patch }));
      await expect(backend.complete?.(complete)).rejects.toMatchObject({ code: "bad_response" });
    },
  );

  it.each([null, {}, { ...fixture.capabilities, version: 2 }, { ...fixture.capabilities, limits: {} }, { ...fixture.capabilities, commands: [{ name: "query" }] }])(
    "refuses malformed capabilities without a fallback", (value) => { expect(() => parseCapabilities(value)).toThrow(); },
  );

  it("network failure never leaks exception text or retries", async () => {
    const fetch = vi.fn(() => Promise.reject(new TypeError("secret controller address")));
    const backend = createTerminalBackend(createApiClient({ fetch }));
    const outcome = await backend.execute(["info"]);
    expect(outcome).toMatchObject({ kind: "failed", ran: "unknown" });
    expect(JSON.stringify(outcome)).not.toContain("secret");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("aborts waiting on a deadline and on session disposal, without claiming cancellation", async () => {
    const signals: AbortSignal[] = [];
    const fetch = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) throw new Error("missing signal");
      signals.push(signal);
      signal.addEventListener("abort", () => { reject(new DOMException("Aborted", "AbortError")); }, { once: true });
    }));
    const backend = createTerminalBackend(createApiClient({ fetch }), { timeoutMs: 5 });
    expect(await backend.execute(["info"])).toMatchObject({ kind: "failed", ran: "unknown" });
    const controller = new AbortController();
    const pending = backend.execute(["info"], controller.signal);
    controller.abort();
    expect(await pending).toMatchObject({ kind: "failed", ran: "unknown" });
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
