import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { safeNext } from "../auth/session";
import { FakeApi } from "../test/fakeApi";
import { dashboardFixture, dashboardWith } from "../test/fakeReports";
import { FakeSetup } from "../test/fakeSetup";

const DASHBOARD = ["dashboard", "default"];
import { renderApp } from "../test/render";

async function logIn(user: ReturnType<typeof userEvent.setup>, password = "correct horse", username = "demo") {
  await user.type(await screen.findByLabelText("User name"), username);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Log in" }));
}

describe("the route guard", () => {
  it("sends nobody to the login page and remembers the page they asked for", async () => {
    const user = userEvent.setup();
    renderApp("/profile");
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    await logIn(user);
    expect(await screen.findByRole("heading", { name: "Profile" })).toBeInTheDocument();
  });

  it("goes straight to the page for someone who is logged in", async () => {
    const fake = new FakeApi();
    const user = userEvent.setup();
    const first = renderApp("/", { fake });
    await logIn(user);
    await screen.findByRole("heading", { name: "Dashboard" });
    first.unmount();
    renderApp("/profile", { fake }); // a reload: no token in memory, the cookie is still there
    expect(await screen.findByRole("heading", { name: "Profile" })).toBeInTheDocument();
  });

  it("shows an error, not a login form, when the session cannot be asked about", async () => {
    const fake = new FakeApi();
    fake.failWith("/auth/me", 503, "not_configured", "The server is not set up yet: finish the setup first.");
    renderApp("/", { fake });
    expect(await screen.findByRole("alert")).toHaveTextContent("The server is not set up yet");
    expect(screen.queryByLabelText("User name")).toBeNull();
  });

  it("goes back to the login page when the session ends on the server", async () => {
    const fake = new FakeApi();
    const user = userEvent.setup();
    const { services } = renderApp("/", { fake });
    await logIn(user);
    await screen.findByRole("heading", { name: "Dashboard" });
    fake.restartServer();
    await act(async () => {
      await services.queryClient.refetchQueries({ queryKey: DASHBOARD });
    });
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });

  it("forgets what the previous session read when it ends on the server, so the next person to log in does not see it", async () => {
    const fake = new FakeApi();
    const user = userEvent.setup();
    const { services } = renderApp("/", { fake });
    await logIn(user);
    expect(await screen.findByText("Office Switch")).toBeInTheDocument();
    expect(services.queryClient.getQueryData(DASHBOARD)).toBeDefined();

    fake.restartServer(); // the session ends; the next request is a 401
    const findings = dashboardFixture().findings;
    fake.dashboard = dashboardWith({
      findings: { ...findings, attention: [{ id: "x", rank: 1, severity: "warning", code: "device.offline", subject: "Other subject", message: "is offline" }] },
    });
    await act(async () => {
      await services.queryClient.refetchQueries({ queryKey: DASHBOARD }).catch(() => undefined);
    });
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(services.queryClient.getQueryData(DASHBOARD)).toBeUndefined();

    await logIn(user, "viewer pass", "viewer");
    expect(await screen.findByText("Other subject")).toBeInTheDocument();
    expect(screen.queryByText("Office Switch")).toBeNull();
  });

  it("does not follow a next= that leaves the app", () => {
    for (const hostile of ["//evil.example", "https://evil.example/", "/\\evil.example", "javascript:alert(1)", "/a\u0000b", "", null, "/login"]) {
      expect(safeNext(hostile)).toBe("/");
    }
    expect(safeNext("/profile?x=1")).toBe("/profile?x=1");
  });
});

describe("the login page", () => {
  it("says what is wrong with the credentials, with the server's sentence, and clears the password", async () => {
    const user = userEvent.setup();
    renderApp("/login");
    await logIn(user, "wrong password");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Invalid username or password.");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("explains a throttle, counts down and blocks the button", async () => {
    const fake = new FakeApi();
    const user = userEvent.setup();
    renderApp("/login", { fake });
    for (let attempt = 0; attempt < 4; attempt += 1) {
      await logIn(user, "wrong");
      await screen.findByRole("alert");
    }
    await user.type(screen.getByLabelText("Password"), "correct horse");
    await user.click(screen.getByRole("button", { name: /Log in|Wait/ }));
    expect(await screen.findByText("Too many attempts. Try again in 2 seconds.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Wait \d s/ })).toBeDisabled();
  });

  it("warns that a network login without HTTPS sends the password in clear text, and not on this machine", async () => {
    renderApp("/login", { meta: { https: false, loopback: false } });
    expect(await screen.findByText("This connection is not encrypted")).toBeInTheDocument();
  });

  it("does not warn on loopback or HTTPS", async () => {
    const { unmount } = renderApp("/login", { meta: { https: false, loopback: true } });
    await screen.findByLabelText("User name");
    await waitFor(() => { expect(screen.getByText(/Homelab Probe 0\.0\.0-test/)).toBeInTheDocument(); });
    expect(screen.queryByText("This connection is not encrypted")).toBeNull();
    unmount();
    renderApp("/login", { meta: { https: true, loopback: false } });
    await screen.findByText(/Homelab Probe 0\.0\.0-test/);
    expect(screen.queryByText("This connection is not encrypted")).toBeNull();
  });

  it("sends a server that needs its setup to the setup, from the login and from any page", async () => {
    const fake = new FakeApi({ accounts: [] });
    new FakeSetup(fake);
    const first = renderApp("/login", { fake });
    expect(await screen.findByRole("heading", { name: "Welcome to Homelab Probe" })).toBeInTheDocument();
    first.unmount();
    renderApp("/profile", { fake });
    expect(await screen.findByRole("heading", { name: "Welcome to Homelab Probe" })).toBeInTheDocument();
  });

  it("shows a failure to reach the server with a way to retry", async () => {
    const fake = new FakeApi();
    fake.failWith("/meta", 502, "controller_error", "The controller's answer could not be used.");
    renderApp("/login", { fake });
    expect(await screen.findByText("Could not load the server's details")).toBeInTheDocument();
  });
});

describe("the shell", () => {
  async function shell(options: ConstructorParameters<typeof FakeApi>[0] = {}) {
    const user = userEvent.setup();
    const rendered = renderApp("/", options);
    await logIn(user);
    await screen.findByRole("heading", { name: "Dashboard" });
    return { user, ...rendered };
  }

  it("has the landmarks, a skip link as the first thing to tab to, and a labelled navigation", async () => {
    const { user } = await shell();
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toBeInTheDocument();
    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveFocus();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute("href", "#main");
  });

  it("marks the current page in the navigation and moves focus to the new page's heading when it changes", async () => {
    const { user } = await shell();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    await user.click(within(nav).getByRole("link", { name: "Profile" }));
    expect(await screen.findByRole("heading", { name: "Profile" })).toHaveFocus();
    expect(within(nav).getByRole("link", { name: "Profile" })).toHaveAttribute("aria-current", "page");
    expect(document.title).toBe("Profile - Homelab Probe");
  });

  it("opens the menu on a phone, makes the rest of the page inert, closes it with Escape and returns focus", async () => {
    const { user } = await shell();
    const open = screen.getByRole("button", { name: "Menu" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await user.click(open);
    expect(open).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Close menu" })).toHaveFocus();
    expect(screen.getByRole("main")).toHaveAttribute("inert");
    await user.keyboard("{Escape}");
    expect(open).toHaveAttribute("aria-expanded", "false");
    expect(open).toHaveFocus();
    expect(screen.getByRole("main")).not.toHaveAttribute("inert");
  });

  it("closes the menu when a page is chosen", async () => {
    const { user } = await shell();
    await user.click(screen.getByRole("button", { name: "Menu" }));
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Profile" }));
    expect(screen.getByRole("button", { name: "Menu" })).toHaveAttribute("aria-expanded", "false");
  });

  it("shows who is logged in, as text", async () => {
    const user = userEvent.setup();
    const evil = "<img src=x onerror=alert(1)>\u202egpj";
    renderApp("/", { accounts: [{ username: evil, password: "pw-pw-pw-pw", role: "viewer" }] });
    await logIn(user, "pw-pw-pw-pw", evil);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(nav).toHaveTextContent("<img src=x onerror=alert(1)>gpj");
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect([...document.querySelectorAll("img")].every((img) => img.className.startsWith("brand"))).toBe(true);
    expect(within(nav).getByText("Viewer")).toBeInTheDocument();
  });

  it("badges demo data", async () => {
    await shell({ meta: { demo: true } });
    expect(await screen.findByText("Demo data")).toBeInTheDocument();
  });

  it("logs out with the CSRF token, forgets the cached data and returns to the login page", async () => {
    const { user, fake } = await shell();
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    const logout = fake.calls.find((call) => call.path === "/auth/logout");
    expect(logout?.csrf).toMatch(/^csrf-/);
    expect(fake.currentCsrf()).toBeNull();
  });
});

describe("the pages", () => {
  it("keeps the dashboard on screen when a refresh fails, and says so", async () => {
    const fake = new FakeApi();
    const user = userEvent.setup();
    renderApp("/", { fake });
    await logIn(user);
    expect(await screen.findByText("Office Switch")).toBeInTheDocument();
    fake.failWith("/unifi/sites/default/dashboard", 502, "controller_error", "The controller's answer could not be used.");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("Showing older dashboard data")).toBeInTheDocument();
    expect(screen.getByText("Office Switch")).toBeInTheDocument();
  });

  it("shows the profile with the session limits and says the password change is not available yet", async () => {
    const user = userEvent.setup();
    renderApp("/profile");
    await logIn(user);
    expect(await screen.findAllByText("Administrator")).toHaveLength(2); // the account line and the profile
    expect(screen.getByText(/Changing your password here is not available yet/)).toBeInTheDocument();
    expect(screen.getByText(/after 30 minutes without activity/)).toBeInTheDocument();
  });

  it("has a page for an unknown address", async () => {
    const user = userEvent.setup();
    renderApp("/nowhere");
    await logIn(user);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});
