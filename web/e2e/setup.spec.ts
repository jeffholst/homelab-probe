import { expect, test } from "@playwright/test";

import { FakeSetup, SETUP_TOKEN } from "../src/test/fakeSetup";
import { expectNoAxeViolations, expectNoHorizontalOverflow, screenshot } from "./support";

for (const theme of ["light", "dark"] as const) {
  test(`first-run configuration, deep links and login (${theme})`, async ({ page }, testInfo) => {
    const server = new FakeSetup();
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request(), url = new URL(request.url());
      const response = await server.fake.fetch(url.pathname + url.search, {
        method: request.method(), headers: request.headers(), body: request.postData(), credentials: "include",
      });
      await route.fulfill({ status: response.status, headers: Object.fromEntries(response.headers), body: await response.text() });
    });
    await page.goto("/setup/controller");
    await page.getByLabel("Theme").selectOption(theme);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Set up Homelab Probe" })).toBeVisible();
    await page.getByLabel("Setup token from the server log").fill(SETUP_TOKEN);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("Controller HTTPS address").fill("https://controller.example.test");
    await page.getByLabel("API key", { exact: true }).fill("synthetic-api-key");
    await page.getByRole("button", { name: "Save draft" }).click();
    await page.getByRole("button", { name: "Fetch certificate" }).click();
    await page.getByLabel("Verified fingerprint").fill("AA:BB:CC:DD");
    await page.getByRole("button", { name: "Trust this fingerprint" }).click();
    await expect(page.getByText("Pinned controller certificate")).toBeVisible();
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(page.getByText("Connection test passed")).toBeVisible();
    await expectNoHorizontalOverflow(page); await expectNoAxeViolations(page);
    await screenshot(page, testInfo, `setup-${theme}`);
    const stored = await page.evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage }, url: location.href }));
    expect(JSON.stringify(stored)).not.toMatch(/synthetic-api-key|synthetic-setup-token/);
    await page.getByLabel("Administrator user name").fill("owner");
    await page.getByLabel("Administrator password", { exact: true }).fill("synthetic-password");
    await page.getByLabel("Confirm administrator password").fill("synthetic-password");
    await page.getByRole("checkbox", { name: "Create this administrator and finish setup" }).check();
    await page.getByRole("button", { name: "Finish setup" }).click();
    await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();
    await page.getByLabel("User name").fill("owner"); await page.getByLabel("Password").fill("synthetic-password");
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
  });

  test(`admin bootstrap failures and unfinished fallback (${theme})`, async ({ page }, testInfo) => {
    const server = new FakeSetup("admin");
    let reject = true;
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname === "/api/v1/setup/status" && reject) {
        reject = false;
        await route.fulfill({ status: 401, json: { error: "invalid_setup_token", message: "POISON_SECRET" } });
        return;
      }
      const response = await server.fake.fetch(url.pathname + url.search, {
        method: request.method(), headers: request.headers(), body: request.postData(), credentials: "include",
      });
      await route.fulfill({ status: response.status, headers: Object.fromEntries(response.headers), body: await response.text() });
    });
    await page.goto("/profile"); await page.getByLabel("Theme").selectOption(theme);
    await page.getByLabel("Setup token from the server log").fill(SETUP_TOKEN);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("expired or wrong");
    await expect(page.getByLabel("Setup token from the server log")).toHaveValue("");
    await expect(page.locator("body")).not.toContainText("POISON_SECRET");
    await page.getByLabel("Setup token from the server log").fill(SETUP_TOKEN);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("heading", { name: "First administrator" })).toBeVisible();
    await expect(page.getByLabel("Controller HTTPS address")).toHaveCount(0);
    await expectNoHorizontalOverflow(page); await expectNoAxeViolations(page);
    await screenshot(page, testInfo, `setup-admin-${theme}`);
    await page.getByRole("button", { name: "Clear browser credentials" }).click();
    server.fake.meta.setup_mode = "setup"; server.status.mode = "setup";
    await page.getByRole("button", { name: "Recheck server state" }).click();
    await page.getByLabel("Setup token from the server log").fill(SETUP_TOKEN);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("Controller HTTPS address").fill("https://controller.example.test");
    await page.getByLabel("API key", { exact: true }).fill("synthetic-api-key");
    await page.getByRole("button", { name: "Save draft" }).click();
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(page.getByText("Connection test passed")).toBeVisible();
    server.finishResult = { finished: false, written: false, reason: "env_file_named", environment_names: [], placeholders: ["UNIFI_API_KEY"],
      env: "UNIFI_API_KEY=your-api-key-here", compose: "environment: placeholder", certificate: null };
    await page.getByLabel("Administrator user name").fill("owner");
    await page.getByLabel("Administrator password", { exact: true }).fill("synthetic-password");
    await page.getByLabel("Confirm administrator password").fill("synthetic-password");
    await page.getByRole("checkbox", { name: "Create this administrator and finish setup" }).check();
    await page.getByRole("button", { name: "Finish setup" }).click();
    await expect(page.getByRole("heading", { name: "Settings were not saved" })).toBeVisible();
    await expect(page.getByText("Setup is not finished")).toBeVisible();
    await expectNoHorizontalOverflow(page); await expectNoAxeViolations(page);
    await screenshot(page, testInfo, `setup-fallback-${theme}`);
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download environment template" }).click();
    expect((await download).suggestedFilename()).toBe("env.example");
  });
}
