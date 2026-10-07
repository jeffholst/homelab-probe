import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { collectProblems, demoCredentials, expectNoAxeViolations, expectNoHorizontalOverflow, screenshot } from "./support";

// Against real servers: `hlp serve` with no settings at all (the setup mode, its token and its draft; the baseURL) and
// one with settings but no administrator (the admin mode; `metadata.adminURL`). Nothing here makes a server contact a
// controller (no certificate fetch, no connection test: those are covered with the fake API) or send a notification.
// The tests of this file run in order and change their servers: the first administrator is created on the admin
// server, a backup of it is exported through the API and restored on the setup server, which leaves the setup mode.

const OWNER = { username: "owner", password: "e2e owner password, long enough" };
const BACKUP_PASSPHRASE = "e2e backup passphrase";
let backup: Buffer | null = null;

const adminURL = (testInfo: TestInfo) => String(testInfo.project.metadata["adminURL"]);

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

test("creates the first administrator of a server configured from its environment, in the light theme", async ({ page }, testInfo) => {
  const problems = collectProblems(page);
  const base = adminURL(testInfo);
  await page.goto(`${base}/login`);
  await expect(page).toHaveURL(/\/setup$/);
  await page.getByRole("radio", { name: "Light" }).check();
  const token = ((await (await page.request.get(`${base}/__e2e/credentials`)).json()) as { setupToken: string }).setupToken;
  await page.getByLabel("Setup token").fill(token);
  await page.getByRole("button", { name: "Start the setup" }).click();
  await expect(page.getByRole("heading", { name: "One step left", level: 1 })).toBeVisible();
  await page.getByRole("button", { name: /Create the first administrator/ }).click();
  await page.getByLabel("User name").fill(OWNER.username);
  await page.getByLabel("Password", { exact: true }).fill(OWNER.password);
  await page.getByLabel("Password again").fill(OWNER.password);
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-admin-light");
  await page.getByRole("button", { name: "Finish setup" }).click();
  await expect(page.getByRole("heading", { name: "You're all set", level: 1 })).toBeVisible();
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-done-light");

  await page.getByRole("button", { name: "Go to the login" }).click();
  await page.getByLabel("User name").fill(OWNER.username);
  await page.getByLabel("Password").fill(OWNER.password);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible();
  expect(await page.content()).not.toContain(OWNER.password);
  expect(problems.filter((problem) => !/status of (401|503)/.test(problem))).toEqual([]);

  // A backup of this installation, made through the API with the new administrator's session, for the restore below.
  const me = (await (await page.request.get(`${base}/api/v1/auth/me`)).json()) as { csrf_token: string };
  const exported = await page.request.post(`${base}/api/v1/backup`, {
    headers: { Origin: base, "X-CSRF-Token": me.csrf_token },
    data: { passphrase: BACKUP_PASSPHRASE, confirm: BACKUP_PASSPHRASE, include: [] },
  });
  expect(exported.status()).toBe(200);
  backup = await exported.body();
});

test("restores that backup on the fresh installation, in the dark theme, and its administrator logs in", async ({ page }, testInfo) => {
  expect(backup, "the backup made by the test before").not.toBeNull();
  await page.goto("/setup");
  await expect(page.getByRole("radio", { name: "Dark" })).toBeChecked();
  await enterToken(page);
  await page.getByRole("button", { name: /Restore from a backup/ }).click();
  await page.getByLabel(/Choose a backup file/).setInputFiles({ name: "home.hlpbackup", mimeType: "application/octet-stream", buffer: backup ?? Buffer.from("") });
  await page.getByLabel("Passphrase of the backup").fill("not the passphrase of it");
  await page.getByRole("button", { name: "Open the backup" }).click();
  await expect(page.getByText("The passphrase is wrong, or the backup was modified or damaged.")).toBeVisible();
  await page.getByLabel("Passphrase of the backup").fill(BACKUP_PASSPHRASE);
  await page.getByRole("button", { name: "Open the backup" }).click();

  await expect(page.getByRole("heading", { name: "What the restore would do" })).toBeVisible();
  await expect(page.getByText(OWNER.username, { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-restore-review");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("A recovery backup comes first")).toHaveCount(0); // an empty installation has nothing to keep
  await page.getByLabel(/I understand that the accounts and passwords/).check();
  await expectNoAxeViolations(page);
  await screenshot(page, testInfo, "setup-restore-confirm");
  await page.getByRole("button", { name: "Restore now" }).click();
  await expect(page.getByRole("heading", { name: "Backup restored", level: 1 })).toBeVisible();
  await expectNoAxeViolations(page);

  await page.getByRole("button", { name: "Go to the login" }).click();
  await page.getByLabel("User name").fill(OWNER.username);
  await page.getByLabel("Password").fill(OWNER.password);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible();
  const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), document.cookie, location.href]));
  for (const secret of [BACKUP_PASSPHRASE, OWNER.password]) expect(stored).not.toContain(secret);
});
