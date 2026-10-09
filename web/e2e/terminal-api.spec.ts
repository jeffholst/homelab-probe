import { expect, test, type Page } from "@playwright/test";

import { collectProblems, expectNoHorizontalOverflow, logIn, openAccount, screenshot } from "./support";

test.describe("preview-gated terminal API", () => {
  test.describe.configure({ mode: "serial" });

  async function openTerminal(page: Page): Promise<void> {
    await page.getByRole("button", { name: "Terminal", exact: true }).click();
    await expect(page.getByRole("region", { name: "Terminal" })).toBeVisible();
    await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  }

  async function run(page: Page, command: string): Promise<void> {
    await page.locator(".xterm-helper-textarea").focus();
    await page.keyboard.type(command);
    await page.keyboard.press("Enter");
  }

  const transcript = (page: Page) => page.getByRole("log", { name: "Terminal transcript" });

  test("renders all xterm styles under fresh style-only CSP nonces and blocks unrelated injection", async ({ page }, testInfo) => {
    const problems = collectProblems(page);
    const first = await page.goto("/login");
    const oldNonce = await page.locator("script[data-terminal-style-nonce]").evaluate((script) => (script as HTMLScriptElement).nonce);
    expect(first?.headers()["content-security-policy"]).toContain(`style-src-elem 'self' 'nonce-${oldNonce}'`);
    await logIn(page);
    await openTerminal(page);
    await run(page, "info");
    await expect(transcript(page)).toContainText("Application:");
    const nonce = await page.locator("script[data-terminal-style-nonce]").evaluate((script) => (script as HTMLScriptElement).nonce);
    expect(nonce).toMatch(/^[A-Za-z0-9_-]{32}$/);
    expect(nonce).not.toBe(oldNonce);
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 844 });
      for (const theme of ["light", "dark"] as const) {
        await page.evaluate((theme) => { document.documentElement.setAttribute("data-theme", theme); }, theme);
        await expect.poll(() => page.locator(".xterm style").evaluateAll((styles) => styles.length >= 3
          && styles.every((style) => Boolean((style as HTMLStyleElement).sheet) && (style as HTMLStyleElement).nonce.length === 32))).toBe(true);
        await expect(page.locator(".xterm-rows")).toContainText("Application:");
        await expect.poll(() => page.locator(".xterm-rows span").first().evaluate((span) => getComputedStyle(span).display)).toBe("inline-block");
        await expect(async () => { await expectNoHorizontalOverflow(page); }).toPass({ timeout: 5000 });
        await screenshot(page, testInfo, `terminal-real-csp-${width}-${theme}`);
      }
    }
    expect(problems).toEqual([]);
    await page.locator(".xterm-helper-textarea").focus();
    await page.keyboard.type("que");
    await page.keyboard.press("Tab");
    await expect(page.locator(".xterm-rows")).toContainText("query");
    const result = await page.evaluate(async ({ oldNonce, nonce }) => {
      const violations: string[] = [];
      document.addEventListener("securitypolicyviolation", (event) => violations.push(event.effectiveDirective));
      const sentinel = document.createElement("div");
      sentinel.id = "nonce-sentinel";
      document.body.append(sentinel);
      const before = getComputedStyle(sentinel).color;
      const styles = ["", "wrong", oldNonce].map((token) => {
        const style = document.createElement("style");
        style.nonce = token;
        style.textContent = "#nonce-sentinel { color: rgb(255, 0, 0) !important; }";
        document.head.append(style);
        return style;
      });
      sentinel.setAttribute("style", "color: rgb(0, 255, 0) !important");
      const script = document.createElement("script");
      script.nonce = nonce; // even the valid style token must not authorize scripts
      script.textContent = "document.documentElement.dataset.nonceScriptExecuted = 'yes'";
      document.head.append(script);
      await new Promise((resolve) => setTimeout(resolve, 100));
      return {
        colorUnchanged: getComputedStyle(sentinel).color === before,
        stylesBlocked: styles.every((style) => style.sheet === null),
        scriptExecuted: document.documentElement.dataset["nonceScriptExecuted"], violations,
      };
    }, { oldNonce, nonce });
    expect(result.colorUnchanged).toBe(true);
    expect(result.stylesBlocked).toBe(true);
    expect(result.scriptExecuted).toBeUndefined();
    expect(result.violations).toEqual(expect.arrayContaining(["style-src-elem", "style-src-attr", "script-src-elem"]));
    const asset = await page.request.get("/theme-init.js");
    expect(asset.headers()["content-security-policy"]).not.toContain("nonce-");
    const api = await page.request.get("/healthz");
    expect(api.headers()["content-security-policy"]).not.toContain("nonce-");
    const deep = await page.request.get("/profile");
    expect(deep.headers()["cache-control"]).toBe("no-store");
    expect(deep.headers()["etag"]).toBeUndefined();
    expect(deep.headers()["content-security-policy"]).not.toContain(`nonce-${nonce}`);
  });

  for (const token of ["missing", "wrong"] as const) {
    test(`blocks terminal styles when the bootstrap nonce is ${token}`, async ({ page }) => {
      await page.route("**/*", async (route) => {
        if (route.request().resourceType() !== "document") { await route.continue(); return; }
        const response = await route.fetch();
        const body = (await response.text()).replace(/nonce="[A-Za-z0-9_-]{32}"/,
          token === "missing" ? "" : `nonce="${"a".repeat(32)}"`);
        const headers = response.headers();
        delete headers["content-length"];
        await route.fulfill({ response, body, headers });
      });
      await logIn(page);
      await openTerminal(page);
      await run(page, "info");
      await expect(transcript(page)).toContainText("Application:");
      await expect.poll(() => page.locator(".xterm style").evaluateAll((styles) => styles.length >= 3
        && styles.every((style) => (style as HTMLStyleElement).sheet === null))).toBe(true);
    });
  }

  test("executes a supported report, refuses an unsupported one, and protects the API from anonymous requests", async ({ page, request }) => {
    await logIn(page);
    await openTerminal(page);
    await run(page, "query clients");
    await expect(transcript(page)).toContainText("phone");

    await run(page, "snapshot");
    await expect(transcript(page)).toContainText("Did not run: This capability is unavailable in the browser.");

    await page.evaluate(() => { document.documentElement.setAttribute("data-theme", "light"); });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole("button", { name: "Menu" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await run(page, "info");
    await expect(transcript(page)).toContainText("Application:");

    const origin = new URL(page.url()).origin;
    const denied = await request.post(new URL("/api/v1/terminal/execute", page.url()).toString(), {
      headers: { Origin: origin },
      data: { argv: ["info"], requestId: "unauthenticated" },
    });
    expect(denied.status()).toBe(401);
  });

  test("shows permission, timeout and truncation outcomes, and renders hostile output literally", async ({ page }) => {
    await page.route("**/api/v1/terminal/execute", async (route) => {
      const body = route.request().postDataJSON() as { argv: string[]; requestId: string };
      const command = body.argv[0] ?? "";
      if (command === "info") {
        await route.fulfill({
          status: 403,
          contentType: "application/json",
          body: JSON.stringify({
            error: "forbidden",
            message: "This operation is not permitted.",
            correlationId: "e2e-denial",
            executionStatus: "did_not_run",
          }),
        });
        return;
      }
      if (command === "query") {
        await route.fulfill({
          status: 504,
          contentType: "application/json",
          body: JSON.stringify({
            error: "terminal_timeout",
            message: "Stopped waiting; controller work may still be running.",
            correlationId: "e2e-timeout",
            executionStatus: "outcome_unknown",
          }),
        });
        return;
      }
      const output = command === "events"
        ? "partial event output"
        : "\u001b[2J[CRITICAL] forged status line\nnormal line\u001b]0;forged title\u0007\u202eevil";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          correlationId: "e2e-output",
          requestId: body.requestId,
          operation: command,
          status: "completed",
          output,
          warnings: [],
          truncated: command === "events",
        }),
      });
    });

    await logIn(page);
    await openTerminal(page);
    await run(page, "info");
    await expect(transcript(page)).toContainText("Did not run: This operation is not permitted.");
    await run(page, "query devices");
    await expect(transcript(page)).toContainText("Outcome unknown: Stopped waiting; controller work may still be running.");
    await run(page, "events");
    await expect(transcript(page)).toContainText("The server cut the output: it is incomplete.");
    await run(page, "wifi");
    await expect(transcript(page)).toContainText("[CRITICAL] forged status line");
    await expect(transcript(page)).toContainText("normal line");
    expect(await page.title()).not.toContain("forged title");
    await expect(page.locator(".xterm-rows")).toContainText("[CRITICAL] forged status line");
    await expect(page.locator(".xterm-rows")).toContainText("]0;forged title");
  });

  test("reports truncated completion suggestions in both the prompt and shortcut toolbar", async ({ page }) => {
    await page.route("**/api/v1/terminal/complete", async (route) => {
      const body = route.request().postDataJSON() as { tokenIndex: number; requestId: string };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          correlationId: "e2e-complete",
          requestId: body.requestId,
          tokenIndex: body.tokenIndex,
          candidates: [{ label: "clients", description: "Client list", kind: "choice" }],
          truncated: true,
        }),
      });
    });
    await logIn(page);
    await openTerminal(page);
    await page.locator(".xterm-helper-textarea").focus();
    await page.keyboard.type("query ");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "clients", exact: true })).toBeVisible();
    await expect(page.getByRole("toolbar", { name: "Shortcuts" })).toContainText("Some suggestions omitted.");
    await expect(page.locator(".xterm-rows")).toContainText("Some suggestions omitted.");
  });

  test("clears the transcript when a session ends and a new one starts", async ({ page }, testInfo) => {
    await logIn(page);
    await openTerminal(page);
    await run(page, "query clients");
    await expect(transcript(page)).toContainText("phone");

    await (await openAccount(page, testInfo)).getByRole("button", { name: "Log out" }).click();
    await expect(page.getByRole("heading", { name: "Log in", level: 1 })).toBeVisible();
    await logIn(page);
    await openTerminal(page);
    await expect(transcript(page)).not.toContainText("phone");
  });
});
