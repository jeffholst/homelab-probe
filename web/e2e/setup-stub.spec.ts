import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { collectProblems, expectNoAxeViolations, expectNoHorizontalOverflow, screenshot } from "./support";

// The setup against real servers that talk to a stub controller (e2e/stub_controller.py: the synthetic demo network over
// HTTPS on 127.0.0.1, accepting one random API key). Nothing here reaches a real controller or sends a notification.
// The project's `metadata` names the servers: `stubURL` (the whole setup runs here), `fallbackURL` (its environment sets
// UNIFI_VERIFY_SSL, so finishing returns the files instead of saving) and `readonlyURL` (started with --read-only).

interface Controller {
  url: string;
  key: string;
  fingerprint: string;
  otherUrl: string;
  otherFingerprint: string;
}

const OWNER = { username: "setup-owner", password: "e2e setup owner password, long enough" };

const server = (testInfo: TestInfo, name: "stubURL" | "fallbackURL" | "readonlyURL") => String(testInfo.project.metadata[name]);

async function credentials(page: Page, base: string): Promise<{ setupToken: string; controller: Controller }> {
  const response = await page.request.get(`${base}/__e2e/credentials`);
  expect(response.ok()).toBe(true);
  return (await response.json()) as { setupToken: string; controller: Controller };
}

/** Opens the setup on `base`, enters the token and starts a new installation. */
async function start(page: Page, base: string): Promise<Controller> {
  const { setupToken, controller } = await credentials(page, base);
  await page.goto(`${base}/setup`);
  await page.getByLabel("Setup token").fill(setupToken);
  await page.getByRole("button", { name: "Start the setup" }).click();
  await page.getByRole("button", { name: /Set up a new installation/ }).click();
  return controller;
}

async function controllerStep(page: Page, url: string, key: string): Promise<void> {
  await page.getByLabel("Controller address").fill(url);
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Recognise the controller" })).toBeVisible();
}

/** The fingerprint shown on the page, back in its colon form. */
async function shownFingerprint(page: Page): Promise<string> {
  return ((await page.locator(".fingerprint").textContent()) ?? "").trim().replace(/\s+/g, ":");
}

/** Fetches the certificate, checks the fingerprint against the one the stub says it shows, and pins it. */
async function pin(page: Page, fingerprint: string): Promise<void> {
  await page.getByRole("button", { name: "Fetch the certificate" }).click();
  await expect(page.locator(".fingerprint")).toBeVisible();
  expect(await shownFingerprint(page)).toBe(fingerprint);
  await page.getByLabel("I compared the fingerprint and it is my controller's").check();
  await page.getByRole("button", { name: "Save and continue" }).click();
  await expect(page.getByRole("heading", { name: "Test the connection" })).toBeVisible();
}

async function testConnection(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Test the connection" }).click();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
}

/** From a passed connection test to the administrator step, skipping what is optional. */
async function toAdministrator(page: Page, withPreview = false): Promise<void> {
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Notifications" })).toBeVisible();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "A first health check" })).toBeVisible();
  if (withPreview) {
    await page.getByRole("button", { name: "Run the checks" }).click();
    await expect(page.getByRole("list", { name: "What was found" })).toBeVisible();
    await page.getByRole("button", { name: "Continue" }).click();
  } else {
    await page.getByRole("button", { name: "Skip" }).click();
  }
  await expect(page.getByRole("heading", { name: "Create the administrator" })).toBeVisible();
}

async function finish(page: Page): Promise<void> {
  await page.getByLabel("User name").fill(OWNER.username);
  await page.getByLabel("Password", { exact: true }).fill(OWNER.password);
  await page.getByLabel("Password again").fill(OWNER.password);
  await page.getByRole("button", { name: "Finish setup" }).click();
}

/** What a script on the page could read: nothing secret may be in it. */
async function readable(page: Page): Promise<string> {
  return page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), document.cookie, location.href, document.body.innerHTML]));
}

test.describe("the whole setup, against the stub controller", () => {
  test.describe.configure({ mode: "serial" });

  test("will not pin a certificate that is not valid for the address, and says why", async ({ page }, testInfo) => {
    const controller = await start(page, server(testInfo, "stubURL"));
    await controllerStep(page, controller.otherUrl, controller.key);
    await page.getByRole("button", { name: "Fetch the certificate" }).click();
    await expect(page.getByText("This certificate cannot be pinned")).toBeVisible();
    expect(await shownFingerprint(page)).toBe(controller.otherFingerprint);
    await expect(page.getByRole("button", { name: "Save and continue" })).toBeDisabled();
    await expectNoHorizontalOverflow(page);
    await expectNoAxeViolations(page);
    await screenshot(page, testInfo, "stub-certificate-refused");
  });

  test("shows the controller's refusal of a wrong key, then sets up with the right one and logs in to the dashboard", async ({ page }, testInfo) => {
    const problems = collectProblems(page);
    const base = server(testInfo, "stubURL");
    const controller = await start(page, base);
    const wrongKey = `${controller.key}-not`;

    // A controller that refuses the key: the test fails, in the server's words, and the setup cannot go on.
    await controllerStep(page, controller.url, wrongKey);
    await pin(page, controller.fingerprint);
    await page.getByRole("button", { name: "Test the connection" }).click();
    await expect(page.getByText("The controller could not be used")).toBeVisible();
    await expect(page.getByRole("list", { name: "What was checked" })).toContainText("Failed");
    await expect(page.getByRole("button", { name: "Continue" })).toBeDisabled();
    await expectNoAxeViolations(page);
    await screenshot(page, testInfo, "stub-key-refused");

    // The right key: back to the first step (the saved key is replaced), then the same steps again.
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(page.getByText(/A key is saved on the server already/)).toBeVisible();
    await page.getByLabel("API key").fill(controller.key);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByRole("heading", { name: "Recognise the controller" })).toBeVisible();
    await page.getByRole("button", { name: "Save and continue" }).click();
    await testConnection(page);
    await expect(page.getByRole("radio", { name: /Default/ })).toBeVisible(); // the site picker, from the controller
    await expectNoHorizontalOverflow(page);
    await expectNoAxeViolations(page);
    await screenshot(page, testInfo, "stub-connected");

    await toAdministrator(page, true);
    await expect(page.getByText("Garage switch")).toHaveCount(0); // not a fake: the findings are the stub's own
    await expectNoAxeViolations(page);
    await finish(page);
    await expect(page.getByRole("heading", { name: "You're all set", level: 1 })).toBeVisible();

    // The settings were saved with the pinned certificate: the dashboard is read from the stub over verified TLS.
    await page.getByRole("button", { name: "Go to the login" }).click();
    await page.getByLabel("User name").fill(OWNER.username);
    await page.getByLabel("Password").fill(OWNER.password);
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page.getByRole("heading", { name: "Critical problems found" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Needs attention" }).getByText("Gateway", { exact: true })).toBeVisible();
    await screenshot(page, testInfo, "stub-dashboard");

    const text = await readable(page);
    for (const secret of [controller.key, wrongKey, OWNER.password]) expect(text).not.toContain(secret);
    expect(problems.filter((problem) => !/status of (401|503)/.test(problem))).toEqual([]);
  });
});

test("says nothing was saved, names what overrides it, and shows files with placeholders only", async ({ page }, testInfo) => {
  const controller = await start(page, server(testInfo, "fallbackURL"));
  await controllerStep(page, controller.url, controller.key);
  await pin(page, controller.fingerprint);
  await testConnection(page);
  await toAdministrator(page);
  await finish(page);

  await expect(page.getByRole("heading", { name: "Save the settings yourself" })).toBeVisible();
  await expect(page.getByText("Nothing was saved")).toBeVisible();
  await expect(page.getByText(/also set in the server's environment/)).toContainText("UNIFI_VERIFY_SSL");
  await expect(page.getByRole("figure", { name: ".env" })).toContainText("UNIFI_API_KEY=your-api-key-here");
  await expect(page.getByRole("figure", { name: "controller.pem" })).toContainText("BEGIN CERTIFICATE");
  await expect(page.getByRole("heading", { name: "You're all set" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "stub-fallback");

  const text = await readable(page);
  for (const secret of [controller.key, OWNER.password]) expect(text).not.toContain(secret);
});

test("a read-only server says so up front and refuses the last step", async ({ page }, testInfo) => {
  const base = server(testInfo, "readonlyURL");
  const { setupToken, controller } = await credentials(page, base);
  await page.goto(`${base}/setup`);
  await page.getByLabel("Setup token").fill(setupToken);
  await page.getByRole("button", { name: "Start the setup" }).click();
  await expect(page.getByText("This server is read-only")).toBeVisible();
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "stub-readonly");

  await page.getByRole("button", { name: /Set up a new installation/ }).click();
  await controllerStep(page, controller.url, controller.key);
  await pin(page, controller.fingerprint);
  await testConnection(page);
  await toAdministrator(page);
  await finish(page);
  await expect(page.getByText("This server is read-only").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "You're all set" })).toHaveCount(0);
  await expect(page.getByLabel("Password", { exact: true })).toHaveValue(""); // the typed password is dropped
});
