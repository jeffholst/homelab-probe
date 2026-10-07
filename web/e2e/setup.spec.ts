import { expect, test, type Page } from "@playwright/test";

import { collectProblems, demoCredentials, expectNoAxeViolations, expectNoHorizontalOverflow, screenshot } from "./support";

// Against `hlp serve` with no settings at all: the real setup mode, its token and its draft. Nothing here makes the
// server contact a controller (no certificate fetch, no connection test): those are covered with the fake API.

/** A wrong setup token is throttled per address: only one test sends one, and the next good one resets the count. */
async function enterToken(page: Page, token?: string): Promise<void> {
  const credentials = await demoCredentials(page);
  await page.getByLabel("Setup token").fill(token ?? credentials.setupToken ?? "");
  await page.getByRole("button", { name: "Start the setup" }).click();
}

test.describe.configure({ mode: "serial" });

test("a server with no settings sends every page to the setup, which asks for the token", async ({ page }, testInfo) => {
  const problems = collectProblems(page);
  for (const path of ["/", "/login", "/profile"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/setup$/);
  }
  await expect(page.getByRole("heading", { name: "Welcome to Homelab Probe", level: 1 })).toBeVisible();
  await expect(page.getByRole("img", { name: "Homelab Probe" })).toBeVisible();
  await expect(page.getByRole("contentinfo")).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-token");
  expect(problems.filter((problem) => !problem.includes("status of 503"))).toEqual([]);
});

test("refuses a wrong token in the server's words, then opens with the right one", async ({ page }, testInfo) => {
  await page.goto("/setup");
  await enterToken(page, "not-the-setup-token-at-all");
  await expect(page.getByText("The setup token is missing or wrong.")).toBeVisible();
  await expectNoAxeViolations(page);
  await page.getByLabel("Setup token").fill("");
  await enterToken(page);
  await expect(page.getByRole("heading", { name: "How would you like to start?", level: 1 })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-choose");
});

test("the controller step keeps the key on the server and shows the server's refusal under its field", async ({ page }, testInfo) => {
  await page.goto("/setup");
  await enterToken(page);
  await page.getByRole("button", { name: /Set up a new installation/ }).click();
  await expect(page.getByRole("list", { name: "Setup progress" })).toBeVisible();
  await screenshot(page, testInfo, "setup-controller");
  await expectNoAxeViolations(page);

  await page.getByLabel("Controller address").fill("http://controller.example");
  await page.getByLabel("API key").fill("an-api-key-for-the-e2e-test");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByLabel("Controller address")).toHaveAttribute("aria-invalid", "true");

  await page.getByLabel("Controller address").fill("https://controller.example");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Recognise the controller" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-certificate");

  // The server has the key; the page and the status never repeat it.
  const credentials = await demoCredentials(page);
  const status = await page.request.get("/api/v1/setup/status", { headers: { "X-Setup-Token": credentials.setupToken ?? "" } });
  const draft = ((await status.json()) as { draft: Record<string, unknown> }).draft;
  expect(draft["api_key_set"]).toBe(true);
  expect(JSON.stringify(draft)).not.toContain("an-api-key-for-the-e2e-test");
  await page.getByRole("button", { name: "Back" }).click();
  await expect(page.getByLabel("API key")).toHaveValue("");
  expect(await page.content()).not.toContain("an-api-key-for-the-e2e-test");
});

test("the restore opens a file and says when it is not a backup", async ({ page }, testInfo) => {
  await page.goto("/setup");
  await enterToken(page);
  await page.getByRole("button", { name: /Restore from a backup/ }).click();
  await expect(page.getByRole("heading", { name: "Restore from a backup", level: 1 })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-restore");
  await page.getByLabel(/Choose a backup file/).setInputFiles({ name: "not.hlpbackup", mimeType: "application/octet-stream", buffer: Buffer.from("not a backup") });
  await page.getByLabel("Passphrase of the backup").fill("a passphrase long enough");
  await page.getByRole("button", { name: "Open the backup" }).click();
  await expect(page.getByText("This is not a Homelab Probe backup.")).toBeVisible();
  await expectNoAxeViolations(page);
});

test("puts the token nowhere a script or a later visitor could read it", async ({ page }) => {
  await page.goto("/setup");
  await enterToken(page);
  await expect(page.getByRole("heading", { name: "How would you like to start?", level: 1 })).toBeVisible();
  const credentials = await demoCredentials(page);
  const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), document.cookie, location.href]));
  expect(stored).not.toContain(credentials.setupToken);
  await page.reload();
  await expect(page.getByLabel("Setup token")).toHaveValue("");
});
