import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { setupApi, parseFinish, parseSetupStatus } from "../api/setup";
import { FakeSetup, SETUP_TOKEN, UNVERIFIED_PHRASE, emptySetup } from "../test/fakeSetup";
import { renderApp } from "../test/render";

async function authorize(user: ReturnType<typeof userEvent.setup>, token = SETUP_TOKEN) {
  await user.type(await screen.findByLabelText("Setup token from the server log"), token);
  await user.click(screen.getByRole("button", { name: "Continue" }));
}
async function configure(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText("Controller HTTPS address"), "https://controller.example.test");
  await user.type(screen.getByLabelText("API key", { exact: true }), "synthetic-api-key");
  await user.click(screen.getByRole("button", { name: "Save draft" }));
  await screen.findByLabelText("Replace API key (already set)");
  await user.click(screen.getByRole("button", { name: "Test connection" }));
  await screen.findByText("Connection test passed");
}
async function administrator(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Administrator user name"), "owner");
  await user.type(screen.getByLabelText("Administrator password", { exact: true }), "synthetic-password");
  await user.type(screen.getByLabelText("Confirm administrator password"), "synthetic-password");
  await user.click(screen.getByRole("checkbox", { name: "Create this administrator and finish setup" }));
}

describe("setup routing and authorization", () => {
  it.each(["/", "/login", "/profile", "/setup/controller", "/setup-other"])("enters setup before session reads at %s", async (path) => {
    const server = new FakeSetup();
    renderApp(path, { fake: server.fake });
    await screen.findByRole("heading", { name: "Set up Homelab Probe" });
    expect(server.fake.calls.map((call) => call.path)).toEqual(["/meta"]);
  });
  it("redirects configured setup deep links to normal login", async () => {
    renderApp("/setup/controller");
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });
  it("warns about unencrypted network setup and refuses unknown modes without setup calls", async () => {
    const { fake } = renderApp("/", { meta: { needs_setup: true, setup_mode: "future", loopback: false } });
    expect(await screen.findByText("Unknown setup mode")).toBeInTheDocument();
    expect(screen.getByText("This connection is not encrypted")).toBeInTheDocument();
    expect(fake.calls.map((call) => call.path)).toEqual(["/meta"]);
  });
  it.each([{ read_only: true }, { demo: true }])("refuses restricted setup %j", async (meta) => {
    const server = new FakeSetup("setup", { meta });
    renderApp("/", { fake: server.fake });
    expect(await screen.findByText("Setup is unavailable")).toBeInTheDocument();
    expect(screen.queryByLabelText("Setup token from the server log")).toBeNull();
    expect(server.fake.calls).toHaveLength(1);
  });
  it("clears rejected tokens and never renders a poisoned server error", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    server.fake.failWith("/setup/status", 401, "invalid_setup_token", "POISON_SECRET <img onerror=alert(1)>");
    const { services } = renderApp("/", { fake: server.fake });
    await authorize(user, "POISON_SECRET");
    expect(await screen.findByRole("alert")).toHaveTextContent("expired or wrong");
    expect(screen.getByLabelText("Setup token from the server log")).toHaveValue("");
    expect(document.body.textContent).not.toContain("POISON_SECRET");
    expect(JSON.stringify(services.queryClient.getQueryCache().getAll().map((query) => query.state))).not.toContain("POISON_SECRET");
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0);
  });
  it("uses an administrator session and CSRF when an administrator already exists", async () => {
    const user = userEvent.setup(), server = new FakeSetup("setup", { accounts: [{ username: "owner", password: "synthetic-password", role: "admin" }] });
    renderApp("/", { fake: server.fake });
    await authorize(user);
    await screen.findByLabelText("Administrator user name");
    await user.type(screen.getByLabelText("Administrator user name"), "owner");
    await user.type(screen.getByLabelText("Administrator password"), "synthetic-password");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await configure(user);
    expect(server.fake.calls.find((call) => call.path === "/setup/draft")?.csrf).toMatch(/^csrf-/);
    expect(screen.queryByLabelText("Confirm administrator password")).toBeNull();
  });
  it("honors token throttling and clears the attempted token", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    server.fake.route("GET", "/setup/status", () => new Response(JSON.stringify({ error: "too_many_attempts", message: "Wait", retry_after: 2 }), {
      status: 429, headers: { "Retry-After": "2", "Content-Type": "application/json" },
    }), { setup: true });
    renderApp("/", { fake: server.fake });
    await authorize(user);
    expect(await screen.findByRole("button", { name: /Wait 2 s/ })).toBeDisabled();
    expect(screen.getByLabelText("Setup token from the server log")).toHaveValue("");
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled(), { timeout: 4000 });
  });
  it("forgets authorization on reload and cancel", async () => {
    const user = userEvent.setup(), server = new FakeSetup("admin");
    const first = renderApp("/", { fake: server.fake });
    await authorize(user);
    await screen.findByRole("heading", { name: "First administrator" });
    await user.click(screen.getByRole("button", { name: "Clear browser credentials" }));
    expect(await screen.findByLabelText("Setup token from the server log")).toHaveValue("");
    await authorize(user);
    await screen.findByRole("heading", { name: "First administrator" });
    first.unmount();
    renderApp("/setup", { fake: server.fake });
    expect(await screen.findByLabelText("Setup token from the server log")).toHaveValue("");
  });
});

describe("configuration and finishing", () => {
  it("configures, creates the administrator and reaches normal login without storing secrets", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    const { services } = renderApp("/", { fake: server.fake });
    await authorize(user); await configure(user); await administrator(user);
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    const cache = JSON.stringify(services.queryClient.getQueryCache().getAll().map((query) => query.state));
    expect(cache).not.toMatch(/synthetic-password|synthetic-api-key|synthetic-setup-token/);
    expect(services.queryClient.getMutationCache().getAll()).toHaveLength(0);
    expect(services.client.hasCsrfToken()).toBe(false);
    await user.type(screen.getByLabelText("User name"), "owner");
    await user.type(screen.getByLabelText("Password"), "synthetic-password");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("heading", { name: "Home" })).toBeInTheDocument();
  });
  it("bootstraps only an administrator when the controller is already configured", async () => {
    const user = userEvent.setup(), server = new FakeSetup("admin");
    renderApp("/", { fake: server.fake });
    await authorize(user); await screen.findByRole("heading", { name: "First administrator" });
    expect(screen.queryByLabelText("Controller HTTPS address")).toBeNull();
    await administrator(user); await user.click(screen.getByRole("button", { name: "Finish setup" }));
    await screen.findByRole("heading", { name: "Log in" });
    expect(server.fake.calls.filter((call) => call.path.startsWith("/setup/")).map((call) => call.path)).toEqual(["/setup/status", "/setup/finish"]);
  });
  it("requires saving and retesting when the site changes", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    renderApp("/", { fake: server.fake }); await authorize(user); await configure(user); await administrator(user);
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeEnabled();
    await user.selectOptions(screen.getByLabelText("Available sites"), "lab");
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Test connection" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Test connection" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeDisabled();
  });
  it("requires deliberate certificate trust and invalidates the old connection test", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    renderApp("/", { fake: server.fake }); await authorize(user); await configure(user);
    await user.click(screen.getByRole("button", { name: "Fetch certificate" }));
    await screen.findByLabelText("Verified fingerprint");
    expect(server.status.draft.verify).toBe("true");
    await user.type(screen.getByLabelText("Verified fingerprint"), "AA:BB:CC:DD");
    await user.click(screen.getByRole("button", { name: "Trust this fingerprint" }));
    await screen.findByText("Pinned controller certificate");
    expect(screen.getByRole("button", { name: "Fetch certificate" })).toBeDisabled();
    expect(server.status.draft.connection_ok).toBeNull();
    expect(screen.getByRole("button", { name: "Preview health checks" })).toBeDisabled();
  });
  it("refuses unusable certificates and only disables verification after exact confirmation", async () => {
    const user = userEvent.setup(), server = new FakeSetup(); server.certificateUsable = false;
    renderApp("/", { fake: server.fake }); await authorize(user); await configure(user);
    await user.click(screen.getByRole("button", { name: "Fetch certificate" }));
    await screen.findByText("Certificate cannot be pinned");
    expect(screen.queryByRole("button", { name: "Trust this fingerprint" })).toBeNull();
    await user.click(screen.getByText("Connect without verifying the certificate"));
    await user.type(screen.getByLabelText("Type the confirmation sentence"), "not exact");
    expect(screen.getByRole("button", { name: "Disable verification" })).toBeDisabled();
    await user.clear(screen.getByLabelText("Type the confirmation sentence"));
    await user.type(screen.getByLabelText("Type the confirmation sentence"), UNVERIFIED_PHRASE);
    await user.click(screen.getByRole("button", { name: "Disable verification" }));
    await screen.findByText("Not verified");
    expect(screen.getByLabelText("Type the confirmation sentence")).toHaveValue("");
  });
  it("does not present partial health or failed connectivity as healthy", async () => {
    const user = userEvent.setup(), server = new FakeSetup(); server.previewWarnings = ["The optional history could not be read."];
    renderApp("/", { fake: server.fake }); await authorize(user); await configure(user);
    await user.click(screen.getByRole("button", { name: "Preview health checks" }));
    expect(await screen.findByText("The optional history could not be read.")).toBeInTheDocument();
    expect(screen.getByText(/not a complete health assessment/)).toBeInTheDocument();
    server.connectionOk = false;
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await screen.findByText("Connection test failed");
    expect(screen.getByRole("button", { name: "Preview health checks" })).toBeDisabled();
  });
  it("preserves safe rejected input while dropping keys and poisoned errors", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    renderApp("/", { fake: server.fake }); await authorize(user);
    await user.type(screen.getByLabelText("Controller HTTPS address"), "http://controller.example.test");
    await user.type(screen.getByLabelText("API key", { exact: true }), "POISON_SECRET");
    server.fake.failWith("/setup/draft", 422, "invalid_setting", "POISON_SECRET");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("settings were rejected");
    expect(screen.getByLabelText("Controller HTTPS address")).toHaveValue("http://controller.example.test");
    expect(screen.getByLabelText("API key", { exact: true })).toHaveValue("");
    expect(document.body.textContent).not.toContain("POISON_SECRET");
  });
  it("offers a notification dry run, clears secrets and never sends a real test", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    const { services } = renderApp("/", { fake: server.fake }); await authorize(user); await configure(user);
    await user.click(screen.getByText("Optional notification destinations"));
    await user.type(screen.getByLabelText("Webhook URL"), "https://hooks.example.test/POISON_SECRET");
    await user.click(screen.getByRole("button", { name: "Save notification draft" }));
    await waitFor(() => expect(screen.getByLabelText("Webhook URL")).toHaveValue(""));
    await user.click(screen.getByRole("button", { name: "Dry run notifications" }));
    expect(await screen.findByText("Nothing was sent.")).toBeInTheDocument();
    expect(server.fake.calls.some((call) => call.path === "/notifications/test")).toBe(false);
    expect(JSON.stringify(services.queryClient.getQueryCache().getAll().map((query) => query.state))).not.toContain("POISON_SECRET");
  });
  it("keeps fallback visibly unfinished and displays only placeholder templates", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    server.finishResult = { finished: false, written: false, reason: "environment", environment_names: ["UNIFI_URL"], placeholders: ["UNIFI_API_KEY"], env: "UNIFI_API_KEY=your-api-key-here", compose: "environment: placeholder", certificate: null };
    renderApp("/", { fake: server.fake }); await authorize(user); await configure(user); await administrator(user);
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    await screen.findByRole("heading", { name: "Settings were not saved" });
    expect(screen.getByText("Setup is not finished")).toBeInTheDocument();
    expect(server.accounts).toEqual([]);
    expect(screen.getByLabelText("Administrator password", { exact: true })).toHaveValue("");
    expect(document.body.textContent).not.toMatch(/synthetic-api-key|synthetic-password/);
  });
  it.each([500, 503, 504])("does not retry an uncertain finish (%s)", async (httpStatus) => {
    const user = userEvent.setup(), server = new FakeSetup("admin");
    server.fake.failWith("/setup/finish", httpStatus, "reload_failed", "POISON_SECRET");
    renderApp("/", { fake: server.fake }); await authorize(user); await screen.findByRole("heading", { name: "First administrator" }); await administrator(user);
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    await screen.findByText("Completion is uncertain");
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeDisabled();
    expect(server.fake.calls.filter((call) => call.path === "/setup/finish")).toHaveLength(1);
    expect(document.body.textContent).not.toContain("POISON_SECRET");
  });
  it("honors a backend not_tested conflict", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    renderApp("/", { fake: server.fake }); await authorize(user); await configure(user); await administrator(user);
    server.status.draft.connection_ok = null;
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Test the connection with the current settings");
    expect(screen.getByRole("button", { name: "Preview health checks" })).toBeDisabled();
  });
  it("blocks duplicate finish and state changes while a finish is pending", async () => {
    const user = userEvent.setup(), server = new FakeSetup("admin");
    const original = server.fake.fetch;
    let release: (() => void) | undefined;
    vi.spyOn(server.fake, "fetch").mockImplementation((url, init) => {
      const answer = original(url, init);
      if (url === "/api/v1/setup/finish") return answer.then((response) => new Promise<Response>((resolve) => { release = () => resolve(response); }));
      return answer;
    });
    renderApp("/", { fake: server.fake }); await authorize(user); await screen.findByRole("heading", { name: "First administrator" }); await administrator(user);
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByText("Finishing setup...")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Recheck server state" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(server.fake.calls.filter((call) => call.path === "/setup/finish")).toHaveLength(1);
    expect(release).toBeDefined();
    await act(async () => { release?.(); await Promise.resolve(); });
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });
  it("does not retry a lost finish response or repeat a poisoned network error", async () => {
    const user = userEvent.setup(), server = new FakeSetup("admin");
    const original = server.fake.fetch;
    vi.spyOn(server.fake, "fetch").mockImplementation((url, init) => url === "/api/v1/setup/finish"
      ? Promise.reject(new TypeError("POISON_SECRET")) : original(url, init));
    renderApp("/", { fake: server.fake }); await authorize(user); await screen.findByRole("heading", { name: "First administrator" }); await administrator(user);
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    await screen.findByText("Completion is uncertain");
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeDisabled();
    expect(document.body.textContent).not.toContain("POISON_SECRET");
    expect(screen.getByLabelText("Administrator password", { exact: true })).toHaveValue("");
  });
  it("keeps a confirmed save distinct from a failed metadata refresh", async () => {
    const user = userEvent.setup(), server = new FakeSetup("admin");
    const original = server.fake.fetch;
    vi.spyOn(server.fake, "fetch").mockImplementation((url, init) => {
      if (url === "/api/v1/setup/finish") server.fake.failWith("/meta", 502, "bad_response", "Unavailable.");
      return original(url, init);
    });
    renderApp("/", { fake: server.fake }); await authorize(user); await screen.findByRole("heading", { name: "First administrator" }); await administrator(user);
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    await screen.findByText("Setup was saved");
    expect(screen.queryByText("Completion is uncertain")).toBeNull();
    expect(screen.queryByRole("link", { name: "Go to login" })).toBeNull();
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Recheck server state" }));
    await screen.findByRole("heading", { name: "Log in" });
    expect(server.fake.calls.filter((call) => call.path === "/setup/finish")).toHaveLength(1);
  });
  it("associates rejected settings with their field without echoing the value", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    server.fake.route("POST", "/setup/draft", () => new Response(JSON.stringify({ error: "invalid_setting", message: "POISON_SECRET", setting: "UNIFI_URL" }), { status: 422 }), { setup: true });
    renderApp("/", { fake: server.fake }); await authorize(user);
    await user.type(screen.getByLabelText("Controller HTTPS address"), "https://controller.example.test");
    await user.type(screen.getByLabelText("API key", { exact: true }), "synthetic-api-key");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Controller HTTPS address")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Controller HTTPS address")).toHaveAccessibleDescription(/settings were rejected/);
    expect(document.body.textContent).not.toContain("POISON_SECRET");
  });
  it("returns to authorization when the token becomes invalid during setup", async () => {
    const user = userEvent.setup(), server = new FakeSetup();
    renderApp("/", { fake: server.fake }); await authorize(user);
    server.fake.failWith("/setup/draft", 401, "invalid_setup_token", "Wrong token.");
    await user.type(screen.getByLabelText("Controller HTTPS address"), "https://controller.example.test");
    await user.type(screen.getByLabelText("API key", { exact: true }), "synthetic-api-key");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByLabelText("Setup token from the server log")).toHaveValue("");
    expect(screen.queryByLabelText("Controller HTTPS address")).toBeNull();
  });
});

describe("setup API boundary", () => {
  it("keeps a token scoped to its request, not subsequent ordinary requests", async () => {
    const seen: { url: string; token: string | null }[] = [];
    const client = createApiClient({ fetch: (url, init) => {
      seen.push({ url: typeof url === "string" ? url : url instanceof URL ? url.href : url.url, token: new Headers(init?.headers).get("X-Setup-Token") });
      return Promise.resolve(new Response(JSON.stringify(emptySetup())));
    } });
    await setupApi(client, SETUP_TOKEN).status(); await client.get("/meta");
    expect(seen).toEqual([{ url: "/api/v1/setup/status", token: SETUP_TOKEN }, { url: "/api/v1/meta", token: null }]);
  });
  it("does not end an administrator session just because a setup token was rejected", async () => {
    const onUnauthorized = vi.fn();
    const client = createApiClient({ onUnauthorized, fetch: () => Promise.resolve(new Response(JSON.stringify({ error: "invalid_setup_token", message: "Wrong token." }), { status: 401 })) });
    client.setCsrfToken("csrf-synthetic");
    await expect(setupApi(client, "wrong").status()).rejects.toMatchObject({ code: "invalid_setup_token" });
    expect(onUnauthorized).not.toHaveBeenCalled(); expect(client.hasCsrfToken()).toBe(true);
  });
  it("checks setup response shapes and discards undeclared fields", () => {
    expect(parseSetupStatus({ ...emptySetup(), token: "POISON_SECRET" })).toEqual(emptySetup());
    for (const value of [null, [], {}, { ...emptySetup(), mode: "future" }, { ...emptySetup(), draft: {} }]) {
      expect(() => parseSetupStatus(value)).toThrow();
    }
    expect(parseFinish({ finished: true, password: "POISON_SECRET" })).toEqual({ finished: true });
    expect(() => parseFinish({ finished: false, written: true })).toThrow();
  });
  it("enforces token, administrator, CSRF and read-only access in the fake", async () => {
    const server = new FakeSetup();
    const client = createApiClient({ fetch: server.fake.fetch });
    await expect(setupApi(client, "wrong").status()).rejects.toMatchObject({ code: "invalid_setup_token" });
    await expect(client.get("/auth/me")).rejects.toMatchObject({ code: "not_configured" });
    server.fake.meta.read_only = true;
    await expect(setupApi(client, SETUP_TOKEN).status()).rejects.toMatchObject({ code: "read_only" });
    const existing = new FakeSetup("setup", { accounts: [{ username: "owner", password: "synthetic-password", role: "admin" }] });
    const other = createApiClient({ fetch: existing.fake.fetch });
    await expect(setupApi(other, SETUP_TOKEN).status()).rejects.toMatchObject({ code: "not_logged_in" });
    await other.post("/auth/login", { body: { username: "owner", password: "synthetic-password" } });
    await expect(setupApi(other).draft({ site: "lab" })).rejects.toMatchObject({ code: "csrf_token" });
  });
  it("rechecks metadata after another browser completes setup", async () => {
    const server = new FakeSetup(), user = userEvent.setup();
    const { services } = renderApp("/setup", { fake: server.fake }); await authorize(user);
    await screen.findByRole("heading", { name: "Controller connection" });
    server.fake.meta.needs_setup = false; server.fake.meta.setup_mode = null;
    await act(async () => { await services.queryClient.refetchQueries({ queryKey: ["meta"] }); });
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });
});
