import { expect, test, type Page } from "@playwright/test";

import { collectProblems, demoCredentials, expectNoAxeViolations, expectNoHorizontalOverflow, isPhone, logIn, openAccount, screenshot } from "./support";

// Against the production build in front of `hlp --demo serve`: the real server, real login, real CSRF, synthetic data.

test.describe("login", () => {
  test("is the first page for nobody, with no overflow and no accessibility violations", async ({ page }, testInfo) => {
    const problems = collectProblems(page);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "Log in", level: 1 })).toBeVisible();
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.getByLabel("User name")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectNoAxeViolations(page);
    await screenshot(page, testInfo, "login");
    expect(problems).toEqual([]);
  });

  test("is accessible in the light theme too", async ({ page }, testInfo) => {
    await page.goto("/login");
    await page.getByRole("radio", { name: "Light" }).check();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.getByLabel("User name")).toBeVisible();
    await expectNoAxeViolations(page);
    await screenshot(page, testInfo, "login-light");
  });

  test("says why a wrong password failed, in the server's words, and stays on the page", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("User name").fill("demo");
    await page.getByLabel("Password").fill("not the password");
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page.getByRole("alert")).toContainText("Invalid username or password.");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByLabel("Password")).toHaveValue("");
  });

  test("returns to the page that was asked for", async ({ page }) => {
    await logIn(page, "/profile");
    await expect(page.getByRole("heading", { name: "Profile", level: 1 })).toBeVisible();
    await expect(page).toHaveURL(/\/profile$/);
  });

  test("does not follow a next= that leaves the site", async ({ page }) => {
    await logIn(page, "/login?next=https%3A%2F%2Fevil.example%2F");
    await expect(page.getByRole("heading", { name: "Home", level: 1 })).toBeVisible();
    expect(new URL(page.url()).origin).toBe(new URL(test.info().project.use.baseURL ?? page.url()).origin);
  });
});

test.describe("the shell", () => {
  test("has the landmarks and the navigation of its size, and passes the accessibility check", async ({ page }, testInfo) => {
    const problems = collectProblems(page);
    await logIn(page);
    await expect(page.getByRole("heading", { name: "Home", level: 1 })).toBeVisible();
    await expect(page.getByRole("banner")).toBeVisible();
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.getByRole("contentinfo")).toBeVisible();
    await expect(page.getByText("Demo data", { exact: true })).toBeVisible();

    const nav = page.getByRole("navigation", { name: "Main" });
    const menu = page.getByRole("button", { name: "Menu" });
    if (isPhone(testInfo)) {
      await expect(menu).toBeVisible();
      await expect(nav).toBeHidden(); // a drawer, closed
      await menu.click();
      await expect(nav).toBeVisible();
      await expect(page.getByRole("button", { name: "Close menu" })).toBeFocused();
      await screenshot(page, testInfo, "shell-menu-open");
      await expectNoAxeViolations(page);
      await page.keyboard.press("Escape");
      await expect(nav).toBeHidden();
      await expect(menu).toBeFocused();
    } else {
      await expect(menu).toBeHidden();
      await expect(nav).toBeVisible(); // a sidebar
      await expect(nav.getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
    }

    await expectNoHorizontalOverflow(page);
    await expectNoAxeViolations(page);
    await screenshot(page, testInfo, "shell-home");
    expect(problems).toEqual([]);
  });

  test("moves between pages by link and keyboard, and the heading takes focus", async ({ page }, testInfo) => {
    await logIn(page);
    if (isPhone(testInfo)) await page.getByRole("button", { name: "Menu" }).click();
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Profile" }).click();
    await expect(page).toHaveURL(/\/profile$/);
    await expect(page.getByRole("heading", { name: "Profile", level: 1 })).toBeFocused();
    await expect(page).toHaveTitle("Profile - Homelab Probe");
    await expectNoHorizontalOverflow(page);
    await expectNoAxeViolations(page);
    await screenshot(page, testInfo, "profile");
  });

  test("starts with the skip link, which jumps to the main region", async ({ page }) => {
    await logIn(page);
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to main content" });
    await expect(skip).toBeFocused();
    await expect(skip).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("main")).toBeFocused();
  });

  test("shows a focus ring on what the keyboard reaches", async ({ page }) => {
    await demoCredentials(page);
    await page.goto("/login");
    await page.getByLabel("User name").focus();
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Password")).toBeFocused();
    const outline = await page.getByLabel("Password").evaluate((element) => getComputedStyle(element).outlineStyle);
    expect(outline).not.toBe("none");
  });

  test("stops animating for people who ask for reduced motion", async ({ page }, testInfo) => {
    test.skip(!isPhone(testInfo), "the drawer exists on the phone layout only");
    await page.emulateMedia({ reducedMotion: "reduce" });
    await logIn(page);
    const duration = await page.getByRole("navigation", { name: "Main", includeHidden: true }).evaluate((element) => getComputedStyle(element).transitionDuration);
    expect(duration.split(",").every((part) => parseFloat(part) < 0.001)).toBe(true);
  });
});

test.describe("themes", () => {
  const background = (page: Page) => page.evaluate(() => getComputedStyle(document.body).backgroundColor);

  test("is dark by default, whatever the system prefers", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/login");
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.+/);
    await expect(page.getByRole("radio", { name: "Dark" })).toBeChecked();
    expect(await background(page)).toBe("rgb(7, 11, 22)");
  });

  test("switches to light from the account controls, and the choice survives a reload", async ({ page }, testInfo) => {
    await logIn(page);
    const dark = await background(page);
    let account = await openAccount(page, testInfo);
    await account.getByRole("radio", { name: "Light" }).check();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    expect(await background(page)).not.toBe(dark);
    await expectNoAxeViolations(page); // light, with the sheet or the menu open
    await screenshot(page, testInfo, "shell-light-menu");

    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    expect(await background(page)).toBe("rgb(244, 247, 252)");
    account = await openAccount(page, testInfo);
    await account.getByRole("radio", { name: "Dark" }).check();
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.+/);
    expect(await background(page)).toBe(dark);
  });

  test("follows the system only when asked", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/login");
    await page.getByRole("radio", { name: "System" }).check();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "system");
    expect(await background(page)).toBe("rgb(244, 247, 252)");
    await page.emulateMedia({ colorScheme: "dark" });
    expect(await background(page)).toBe("rgb(7, 11, 22)");
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "system");
  });
});

test.describe("sessions", () => {
  test("keeps the person logged in across a reload and still logs them out (the CSRF token comes back with /auth/me)", async ({ page }, testInfo) => {
    await logIn(page);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Home", level: 1 })).toBeVisible();
    await (await openAccount(page, testInfo)).getByRole("button", { name: "Log out" }).click();
    await expect(page.getByRole("heading", { name: "Log in", level: 1 })).toBeVisible();
    await page.goto("/profile");
    await expect(page).toHaveURL(/\/login\?next=%2Fprofile$/);
    const me = await page.request.get("/api/v1/auth/me");
    expect(me.status()).toBe(401);
  });

  test("puts nothing but the theme word in browser storage, and no secret anywhere script can reach", async ({ page }, testInfo) => {
    const credentials = await logIn(page);
    await expect(page.getByRole("heading", { name: "Home", level: 1 })).toBeVisible();
    const read = () =>
      page.evaluate(async () => ({
        local: Object.entries(window.localStorage),
        session: Object.entries(window.sessionStorage),
        cookie: document.cookie,
        databases: (await indexedDB.databases()).length,
      }));
    expect((await read()).local).toEqual([]); // logged in, and nothing stored
    await (await openAccount(page, testInfo)).getByRole("radio", { name: "Light" }).check();
    const stored = await read();
    expect(stored.local).toEqual([["hlp-theme", "light"]]);
    expect(stored.session).toEqual([]);
    expect(stored.cookie).toBe(""); // the session cookie is HttpOnly
    expect(stored.databases).toBe(0);
    const session = (await (await page.request.get("/api/v1/auth/me")).json()) as { csrf_token: string };
    expect(JSON.stringify(stored)).not.toContain(credentials.password);
    expect(JSON.stringify(stored)).not.toContain(session.csrf_token);
  });

  test("refuses an unsafe request without the token or without an Origin, as the real server does", async ({ page, request }) => {
    await logIn(page);
    const origin = new URL(page.url()).origin;
    const withoutToken = await page.request.post("/api/v1/auth/logout", { headers: { Origin: origin } });
    expect(withoutToken.status()).toBe(403);
    expect(((await withoutToken.json()) as { error: string }).error).toBe("csrf_token");
    // No Origin at all, as from a script: the server's own check refuses it before it looks at the session.
    const withoutOrigin = await page.request.post("/api/v1/auth/logout");
    expect(withoutOrigin.status()).toBe(403);
    expect(((await withoutOrigin.json()) as { error: string }).error).toBe("csrf_origin");
    expect((await request.get("/api/v1/auth/me")).status()).toBe(401); // a request without the cookie
  });

  test("agrees with the real server on the shape of /meta and /auth/me", async ({ page }) => {
    await logIn(page);
    const meta = (await (await page.request.get("/api/v1/meta")).json()) as Record<string, unknown>;
    expect(Object.keys(meta).sort()).toEqual(["demo", "https", "login_required", "loopback", "needs_setup", "read_only", "setup_mode", "version"]);
    const me = (await (await page.request.get("/api/v1/auth/me")).json()) as Record<string, unknown>;
    expect(Object.keys(me).sort()).toEqual(["can_change_password", "csrf_token", "idle_seconds_left", "role", "session_seconds_left", "username"]);
    const platforms = (await (await page.request.get("/api/v1/platforms")).json()) as Record<string, unknown>[];
    expect(Object.keys(platforms[0] ?? {}).sort()).toEqual(["configured", "id", "name"]);
  });

  test("lists the platforms the server reports, read through the proxy with the real policy in force", async ({ page }) => {
    const problems = collectProblems(page);
    await logIn(page);
    await expect(page.getByText("UniFi", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(page.getByText("UniFi", { exact: true })).toBeVisible();
    expect(problems).toEqual([]);
    const credentials = await demoCredentials(page);
    expect(credentials.policy).toContain("script-src 'self'");
  });
});
