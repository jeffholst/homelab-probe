import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import { SETUP_TOKEN } from "../src/test/fakeSetup";

test("first administrator is persisted by the real backend in an isolated data directory", async ({ page }) => {
  test.setTimeout(90_000);
  const directory = await mkdtemp(path.join(os.tmpdir(), "hlp-bootstrap-e2e-"));
  const port = await new Promise<number>((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      probe.close(() => {
        if (address !== null && typeof address === "object") resolve(address.port);
        else reject(new Error("No free port"));
      });
    });
  });
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  // A minimal environment and a new data directory: never inherit a developer's controller or notification settings.
  const server = spawn("uv", ["run", "--frozen", "--extra", "web", "hlp.py", "serve", "--port", String(port), "--data-dir", directory], {
    cwd: repo,
    env: { PATH: process.env["PATH"], HOME: process.env["HOME"], TMPDIR: process.env["TMPDIR"],
      UNIFI_URL: "https://127.0.0.1:9", UNIFI_API_KEY: "synthetic-unused-key", HLP_SETUP_TOKEN: SETUP_TOKEN },
    stdio: "ignore",
  });
  const exited = new Promise<void>((resolve) => { server.once("exit", () => resolve()); server.once("error", () => resolve()); });
  const target = `http://127.0.0.1:${port}`;
  try {
    await expect.poll(async () => {
      if (server.exitCode !== null) return "exited";
      try {
        const response = await page.request.get(`${target}/api/v1/meta`);
        return ((await response.json()) as { setup_mode: string }).setup_mode;
      } catch { return "starting"; }
    }, { timeout: 60_000 }).toBe("admin");
    await page.route("**/api/v1/**", async (route) => {
      const url = new URL(route.request().url());
      const response = await route.fetch({ url: target + url.pathname + url.search,
        headers: { ...route.request().headers(), origin: target, host: `127.0.0.1:${port}` } });
      await route.fulfill({ response });
    });
    await page.goto("/setup/admin");
    await page.getByLabel("Setup token from the server log").fill(SETUP_TOKEN);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("heading", { name: "First administrator" })).toBeVisible();
    await expect(page.getByLabel("Controller HTTPS address")).toHaveCount(0);
    await page.getByLabel("Administrator user name").fill("owner");
    await page.getByLabel("Administrator password", { exact: true }).fill("synthetic-password");
    await page.getByLabel("Confirm administrator password").fill("synthetic-password");
    await page.getByRole("checkbox", { name: "Create this administrator and finish setup" }).check();
    await page.getByRole("button", { name: "Finish setup" }).click();
    await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();
    await page.getByLabel("User name").fill("owner"); await page.getByLabel("Password").fill("synthetic-password");
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
    // The backend succeeded without a reachable controller; setup must not perform an implicit connection test here.
    const meta = await page.request.get(`${target}/api/v1/meta`);
    expect(((await meta.json()) as { needs_setup: boolean }).needs_setup).toBe(false);
  } finally {
    server.kill("SIGTERM");
    await exited;
    await rm(directory, { recursive: true, force: true });
  }
});
