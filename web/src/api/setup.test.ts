import { describe, expect, it, vi } from "vitest";

import { FakeApi } from "../test/fakeApi";
import { FakeSetup, SETUP_TOKEN } from "../test/fakeSetup";
import { fileToBase64, parseBackupPreview, parseRestore } from "./backup";
import { SETUP_TOKEN_HEADER, createApiClient } from "./client";
import { isApiError } from "./errors";
import { createSetupApi, parseConnection, parseFinish, parsePreview, parseSetupStatus, tokenHeaders } from "./setup";

function wired() {
  const fake = new FakeApi({ accounts: [] });
  const setup = new FakeSetup(fake);
  const onUnauthorized = vi.fn();
  const client = createApiClient({ fetch: fake.fetch, onUnauthorized });
  return { fake, setup, onUnauthorized, api: createSetupApi(client) };
}

const STATUS = {
  mode: "setup",
  reason: "no_config",
  unverified_phrase: "p",
  draft: { url: "", site: "default", api_key_set: false, verify: "true", certificate: null, connection_ok: null, notify: [] },
};

async function refusal(promise: Promise<unknown>) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("no refusal");
}

describe("the setup calls", () => {
  it("send the token in its header, and only when there is one", async () => {
    const seen: Headers[] = [];
    const client = createApiClient({
      fetch: (_url, init) => {
        seen.push(new Headers(init?.headers));
        return Promise.resolve(new Response(JSON.stringify(STATUS), { status: 200 }));
      },
    });
    const api = createSetupApi(client);
    await api.status("the-token");
    await api.status(null);
    expect(seen[0]?.get(SETUP_TOKEN_HEADER)).toBe("the-token");
    expect(seen[1]?.has(SETUP_TOKEN_HEADER)).toBe(false);
    expect(tokenHeaders(null)).toEqual({});
  });

  it("treat a wrong token as an answer about the token, not the end of a session", async () => {
    const { api, onUnauthorized } = wired();
    const error = await refusal(api.status("wrong"));
    expect(isApiError(error) && error.code).toBe("invalid_setup_token");
    expect(onUnauthorized).not.toHaveBeenCalled();
    await expect(api.status(SETUP_TOKEN)).resolves.toMatchObject({ mode: "setup" });
  });

  it("finish without an administrator sends an empty object", async () => {
    const { api, setup } = wired();
    setup.draft = { ...setup.draft, url: "https://x.example", apiKey: "a-key-long-enough", connectionOk: true };
    const error = await refusal(api.finish(SETUP_TOKEN, null));
    expect(isApiError(error) && error.code).toBe("admin_required");
    expect(setup.bodies.at(-1)?.body).toEqual({});
  });
});

describe("the setup parsers", () => {
  it("accept the server's shapes", () => {
    expect(parseSetupStatus(STATUS).draft.verify).toBe("true");
    expect(parseConnection({ ok: true, sites: [{ name: "A", ref: "a", id: "1" }], checks: [] }).sites).toHaveLength(1);
    expect(parsePreview({ areas: [], summary: {}, findings: [{ severity: "info", subject: "s", message: "m" }], total: 1, warnings: [] }).findings[0]?.code).toBe("");
    expect(parseFinish({ finished: false, reason: "environment", detail: "", environment_names: [], placeholders: [], env: "", compose: "" }).finished).toBe(false);
  });

  it.each([
    ["a mode that is not one", () => parseSetupStatus({ ...STATUS, mode: "other" })],
    ["a verify that is not one", () => parseSetupStatus({ ...STATUS, draft: { ...STATUS.draft, verify: "maybe" } })],
    ["a connection_ok that is not a boolean", () => parseSetupStatus({ ...STATUS, draft: { ...STATUS.draft, connection_ok: "yes" } })],
    ["notify names that are not strings", () => parseSetupStatus({ ...STATUS, draft: { ...STATUS.draft, notify: [1] } })],
    ["a certificate without a fingerprint", () => parseSetupStatus({ ...STATUS, draft: { ...STATUS.draft, certificate: {} } })],
    ["sites that are not a list", () => parseConnection({ ok: true, sites: {}, checks: [] })],
    ["checks that are not a list", () => parseConnection({ ok: true, sites: [], checks: {} })],
    ["a check with an unknown status", () => parseConnection({ ok: true, sites: [], checks: [{ id: "a", section: "b", status: "great", title: "", message: "", fix: "" }] })],
    ["findings that are not a list", () => parsePreview({ areas: [], summary: {}, findings: {}, total: 0, warnings: [] })],
    ["a count that is not a number", () => parsePreview({ areas: [], summary: { critical: "1" }, findings: [], total: 0, warnings: [] })],
    ["a written list that is not a list", () => parseFinish({ finished: true, written: {}, admin_created: true })],
    ["a certificate that is not text", () => parseFinish({ finished: false, reason: "", detail: "", environment_names: [], placeholders: [], env: "", compose: "", certificate: 1 })],
  ])("refuse %s", (_name, parse) => {
    expect(parse).toThrowError(/was not what this app expects/);
  });
});

describe("the backup parsers and the file", () => {
  const PREVIEW = {
    created_at: "2026-09-30T08:15:00Z",
    app_version: "0.4.0",
    compatibility: { compatible: true, running_version: "0.4.0" },
    categories: [{ id: "accounts", included: true, files: 1, present_files: 0, restore: "replace" }],
    accounts: { total: 1, administrators: 1, message: "m", users: [{ username: "a", role: "admin", disabled: false }] },
    notes: 0,
    triage: 0,
    sites: 0,
    environment_overrides: ["UNIFI_URL"],
    warnings: [{ code: "c", message: "m" }],
    recovery: { required: false, keep: 3, folder: "recovery" },
  };

  it("read a preview and a restore", () => {
    expect(parseBackupPreview(PREVIEW).accounts.users[0]?.username).toBe("a");
    expect(parseRestore({ restored: true, created_at: "x", recovery_backup: null, message: "m" }).recovery_backup).toBeNull();
  });

  it.each([
    ["a count that is not a number", () => parseBackupPreview({ ...PREVIEW, notes: "3" })],
    ["categories that are not a list", () => parseBackupPreview({ ...PREVIEW, categories: {} })],
    ["an override that is not text", () => parseBackupPreview({ ...PREVIEW, environment_overrides: [1] })],
    ["a restore that did not restore", () => parseRestore({ restored: false, created_at: "x", message: "m" })],
    ["a recovery name that is not text", () => parseRestore({ restored: true, created_at: "x", recovery_backup: 1, message: "m" })],
  ])("refuse %s", (_name, parse) => {
    expect(parse).toThrowError(/was not what this app expects/);
  });

  it("encodes a file larger than one slice exactly", async () => {
    const bytes = new Uint8Array(100_000).map((_, index) => index % 251);
    const encoded = await fileToBase64(new Blob([bytes]));
    const decoded = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
    expect(decoded).toEqual(bytes);
  });
});
