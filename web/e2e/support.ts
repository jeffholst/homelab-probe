import { mkdirSync } from "node:fs";
import path from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, type Locator, type Page, type TestInfo } from "@playwright/test";

export interface Credentials {
  username: string;
  password: string;
  /** The setup token of the server in its setup mode (the setup projects only). */
  setupToken?: string;
  /** The content-security policy of the real server, which the preview server repeats on every page. */
  policy: string;
}

export async function demoCredentials(page: Page): Promise<Credentials> {
  const response = await page.request.get("/__e2e/credentials");
  expect(response.ok()).toBe(true);
  return (await response.json()) as Credentials;
}

export async function logIn(page: Page, to = "/"): Promise<Credentials> {
  const credentials = await demoCredentials(page);
  await page.goto(to);
  await page.getByLabel("User name").fill(credentials.username);
  await page.getByLabel("Password").fill(credentials.password);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.locator('nav[aria-label="Main"]').or(page.getByRole("alert"))).toBeAttached();
  return credentials;
}

/** A 401 is the browser logging an answer the app expects (nobody is logged in yet, a password is wrong). */
const EXPECTED = /Failed to load resource: the server responded with a status of 401/;

/** What the browser reported as an error so far: a script error, a console error, a CSP violation. A test asserts it is empty. */
export function collectProblems(page: Page): string[] {
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !EXPECTED.test(message.text())) problems.push(`console: ${message.text()}`);
  });
  return problems;
}

/** Waits for running CSS transitions and animations (the drawer sliding in), so a check or a screenshot sees the settled page. */
export async function settle(page: Page): Promise<void> {
  await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished.catch(() => undefined))));
}

export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
    body: document.body.scrollWidth,
  }));
  expect(widths.scroll, "the page is wider than the window").toBeLessThanOrEqual(widths.client);
  expect(widths.body, "the body is wider than the window").toBeLessThanOrEqual(widths.client);
}

/** WCAG 2.x A and AA checks (axe); the violations are named in the failure. */
export async function expectNoAxeViolations(page: Page): Promise<void> {
  await settle(page);
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  const summary = results.violations.map((violation) => `${violation.id} (${violation.impact ?? "?"}): ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`);
  expect(summary, "accessibility violations").toEqual([]);
}

/** Saves a screenshot when E2E_SCREENSHOTS names a directory (how the pull request's screenshots were made). */
export async function screenshot(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  const directory = process.env["E2E_SCREENSHOTS"];
  if (!directory) return;
  await settle(page);
  mkdirSync(directory, { recursive: true });
  await page.screenshot({ path: path.join(directory, `${testInfo.project.name}-${name}.png`) });
}

export const isPhone = (testInfo: { project: { name: string } }) => testInfo.project.name.includes("phone");

/** Opens what holds the account controls (theme, log out): the menu sheet on a phone, the account menu on a desktop. */
export async function openAccount(page: Page, testInfo: TestInfo): Promise<Locator> {
  if (isPhone(testInfo)) {
    await page.getByRole("button", { name: "Menu" }).click();
    return page.getByRole("navigation", { name: "Main" });
  }
  await page.getByRole("button", { name: /^Account:/ }).click();
  return page.locator(".popover");
}
