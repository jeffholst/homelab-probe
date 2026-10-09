import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { collectProblems, expectNoAxeViolations, expectNoHorizontalOverflow, isPhone, logIn, screenshot, settle } from "./support";

// The terminal panel against the mock backend (the build sets VITE_TERMINAL_MOCK=1): the panel, its layout and its safety
// in a real browser, behind the real server's content-security policy. Real execution is #277.

/**
 * xterm.js injects <style> elements, which the server's policy (`style-src 'self'`, no 'unsafe-inline') blocks, so with the
 * real policy the terminal's text loses its monospace layout and colours. Whether to allow that is the owner's decision
 * (see "The policy and the terminal" in docs/development.md); until it is made the terminal exists only in a mock build,
 * and this spec states the one relaxation it needs, for the page's styles only, instead of hiding the violation.
 */
async function allowInlineStyles(page: Page): Promise<void> {
  await page.route("**/*", async (route) => {
    if (route.request().resourceType() !== "document") {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const headers = response.headers();
    const policy = headers["content-security-policy"];
    if (policy) headers["content-security-policy"] = policy.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'");
    await route.fulfill({ response, headers });
  });
}

test.beforeEach(async ({ page }) => {
  await allowInlineStyles(page);
});

async function openTerminal(page: Page, testInfo: TestInfo): Promise<void> {
  if (isPhone(testInfo)) await page.getByRole("button", { name: "Menu" }).click();
  await page.getByRole("button", { name: "Terminal", exact: true }).click();
  await expect(page.getByRole("region", { name: "Terminal" })).toBeVisible();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  await settle(page);
}

const transcript = (page: Page) => page.getByRole("log", { name: "Terminal transcript" });
const screenText = (page: Page) => page.locator(".xterm-rows").innerText();

async function run(page: Page, command: string): Promise<void> {
  await page.locator(".xterm-helper-textarea").focus();
  await page.keyboard.type(command);
  await page.keyboard.press("Enter");
}

test.describe("terminal panel", () => {
  test("loads the terminal library only when the panel is opened", async ({ page }, testInfo) => {
    const chunks: string[] = [];
    page.on("request", (request) => {
      if (/XtermSurface/.test(request.url())) chunks.push(request.url());
    });
    await logIn(page);
    await expect(page.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible();
    expect(chunks).toEqual([]);
    await openTerminal(page, testInfo);
    expect(chunks.length).toBeGreaterThan(0);
  });

  test("runs a command, shows plain output and draws no errors or policy violations", async ({ page }, testInfo) => {
    const problems = collectProblems(page);
    await logIn(page);
    await openTerminal(page, testInfo);
    await run(page, "query clients");
    await expect(transcript(page)).toContainText("guest-phone", { timeout: 10_000 });
    await expect(page.locator(".xterm-rows")).toContainText("guest-phone");
    await expectNoHorizontalOverflow(page);
    expect(problems).toEqual([]);
  });

  test("shows report text that tries to control the terminal as plain text only", async ({ page }, testInfo) => {
    const problems = collectProblems(page);
    await logIn(page);
    const title = await page.title();
    await openTerminal(page, testInfo);
    await run(page, "wifi");
    await expect(transcript(page)).toContainText("forged status line", { timeout: 10_000 });
    const shown = await screenText(page);
    expect(shown).toContain("[CRITICAL] forged status line");
    await expect(transcript(page)).toContainText("normal line");
    expect(await page.title()).toBe(title);
    expect(shown).not.toContain("forged title\u0007");
    // The prompt is still on screen, drawn by the app after the report.
    await expect(page.locator(".xterm-rows")).toContainText("❯");
    expect(problems).toEqual([]);
  });

  test("says when the outcome is unknown or the command did not run", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page, testInfo);
    await run(page, "diagnose");
    await expect(transcript(page)).toContainText("Outcome unknown: The controller did not answer in time.", { timeout: 10_000 });
    await run(page, "wan");
    await expect(transcript(page)).toContainText("Did not run: That command is not available in the browser.", { timeout: 10_000 });
    await run(page, "events");
    await expect(transcript(page)).toContainText("The server cut the output: it is incomplete.", { timeout: 10_000 });
  });

  test("Ctrl+C while a command runs says that waiting stopped and keeps the prompt locked", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page, testInfo);
    await run(page, "slow");
    await expect(page.getByText("Running", { exact: true })).toBeVisible();
    await page.keyboard.press("Control+c");
    await expect(transcript(page)).toContainText("Stopped waiting. The command may still be running on the server.");
    await expect(page.getByText("Running", { exact: true })).toBeVisible();
    await expect(page.getByText("Ready", { exact: true })).toBeVisible({ timeout: 10_000 });
  });

  test("Tab completes and the shortcut buttons add text without running anything", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page, testInfo);
    await page.locator(".xterm-helper-textarea").focus();
    await page.keyboard.type("que");
    await page.keyboard.press("Tab");
    await expect(page.locator(".xterm-rows")).toContainText("query");
    await page.keyboard.type(" ");
    await page.getByRole("button", { name: "clients", exact: true }).click();
    await expect(page.locator(".xterm-rows")).toContainText("query clients");
    await expect(transcript(page)).not.toContainText("guest-phone");
  });

  test("Escape leaves the terminal and Tab moves on: there is no keyboard trap", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page, testInfo);
    await page.locator(".xterm-helper-textarea").focus();
    await page.keyboard.press("Escape");
    const dock = page.getByRole("region", { name: "Terminal" });
    await expect(dock).toBeFocused();
    // Focus is visible where it landed (a style sheet that hides it would leave the keyboard user lost).
    expect(await dock.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");
    await page.keyboard.press("Tab");
    const inTerminal = await page.evaluate(() => document.activeElement?.classList.contains("xterm-helper-textarea") ?? false);
    expect(inTerminal).toBe(false);
  });

  test("resizes with the keyboard, collapses, expands and closes", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page, testInfo);
    const handle = page.getByRole("slider", { name: "Resize the terminal" });
    const before = Number(await handle.getAttribute("aria-valuenow"));
    await handle.focus();
    await page.keyboard.press("ArrowUp");
    expect(Number(await handle.getAttribute("aria-valuenow"))).toBe(before + 24);
    // The rendered panel follows (on a phone too: nothing in the style sheet overrides the chosen height).
    await expect.poll(async () => Math.round((await page.getByRole("region", { name: "Terminal" }).boundingBox())?.height ?? 0)).toBe(before + 24);
    await page.getByRole("button", { name: "Collapse the terminal panel" }).click();
    await expect(page.getByRole("region", { name: "Terminal" })).toHaveAttribute("data-mode", "collapsed");
    await page.getByRole("button", { name: "Expand the terminal panel" }).click();
    await page.getByRole("button", { name: "Fill the page with the terminal" }).click();
    await expect(page.getByRole("region", { name: "Terminal" })).toHaveAttribute("data-mode", "expanded");
    await page.getByRole("button", { name: "Close the terminal panel" }).click();
    await expect(page.getByRole("region", { name: "Terminal" })).toHaveCount(0);
  });

  test("keeps the input and the shortcut toolbar on screen, with no overflow", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page, testInfo);
    const viewport = page.viewportSize();
    expect(viewport).not.toBeNull();
    for (const locator of [page.locator(".xterm-helper-textarea"), page.getByRole("toolbar", { name: "Shortcuts" })]) {
      const box = await locator.boundingBox();
      expect(box).not.toBeNull();
      expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual((viewport?.height ?? 0) + 1);
      expect(box?.y ?? -1).toBeGreaterThanOrEqual(0);
    }
    await expectNoHorizontalOverflow(page);
  });

  test("keeps filled terminal rows above the bottom inset after scrolling and resizing", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page, testInfo);
    const assertInset = async () => {
      await expect(page.getByText("Ready", { exact: true })).toBeVisible();
      await expect.poll(async () => page.locator(".terminal-surface").evaluate((surface) => {
        const screen = surface.querySelector(".xterm-screen")!;
        const rows = surface.querySelector(".xterm-rows")!;
        const bounds = surface.getBoundingClientRect();
        const style = getComputedStyle(surface);
        const bottom = bounds.bottom - parseFloat(style.borderBottomWidth);
        return bottom - Math.max(screen.getBoundingClientRect().bottom, rows.getBoundingClientRect().bottom);
      })).toBeGreaterThanOrEqual(12);
      // A stale, undersized grid would also satisfy the minimum inset after expanding.
      await expect.poll(async () => page.locator(".terminal-surface").evaluate((surface) => {
        const screen = surface.querySelector(".xterm-screen")!;
        const style = getComputedStyle(surface);
        return surface.getBoundingClientRect().bottom - parseFloat(style.borderBottomWidth)
          - parseFloat(style.paddingBottom) - screen.getBoundingClientRect().bottom;
      })).toBeLessThan(20);
      await expect(page.locator(".xterm-rows")).toContainText("❯");
      await expectNoHorizontalOverflow(page);
    };
    await run(page, "events");
    await expect(transcript(page)).toContainText("Garage AP changed state");
    await assertInset();
    await screenshot(page, testInfo, "terminal-bottom-inset-docked");
    const bottomText = await screenText(page);
    await page.locator(".xterm-screen").hover();
    await page.mouse.wheel(0, -500);
    await expect.poll(() => screenText(page)).not.toBe(bottomText);
    await page.mouse.wheel(0, 10000);
    await expect.poll(() => screenText(page)).toBe(bottomText);
    await assertInset();
    const handle = page.getByRole("slider", { name: "Resize the terminal" });
    await handle.focus();
    await page.keyboard.press("ArrowDown");
    await assertInset();
    await page.keyboard.press("ArrowUp");
    await assertInset();
    await page.getByRole("button", { name: "Fill the page with the terminal" }).click();
    await run(page, "events");
    await assertInset();
    await screenshot(page, testInfo, "terminal-bottom-inset");
  });

  for (const theme of ["dark", "light"] as const) {
    test(`has no accessibility violations and looks right in the ${theme} theme`, async ({ page }, testInfo) => {
      await logIn(page);
      if (theme === "light") {
        await page.evaluate(() => { document.documentElement.setAttribute("data-theme", "light"); });
      }
      await openTerminal(page, testInfo);
      await run(page, "query clients");
      await expect(transcript(page)).toContainText("guest-phone", { timeout: 10_000 });
      await expectNoAxeViolations(page);
      await screenshot(page, testInfo, `terminal-${theme}`);
    });
  }
});
