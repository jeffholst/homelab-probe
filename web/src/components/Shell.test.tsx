import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createMockBackend } from "../terminal/mock";
import { miniEngine } from "../terminal/testing/miniEngine";
import { renderApp } from "../test/render";
import { SESSION_KEY } from "../app/services";
import type { Session } from "../api/types";
import { FOOTER_LINKS } from "./SiteFooter";

/** A browser at least 56rem wide: `matchMedia` answers yes to the desktop query. */
function desktop() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query === "(min-width: 56rem)",
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

// The terminal library needs a real browser; the shell only has to mount the panel around it.
vi.mock("../terminal/XtermSurface", () => ({ default: ({ onInput }: { onInput: (data: string) => void }) => (
  <div data-testid="surface">
    <button type="button" onClick={() => { onInput("info"); onInput("\r"); }}>run info</button>
  </div>
) }));

async function logIn(terminal = false) {
  const user = userEvent.setup();
  const rendered = renderApp("/", terminal ? { terminal: { engine: miniEngine, backend: createMockBackend({ delay: 0 }) } } : {});
  await user.type(await screen.findByLabelText("User name"), "demo");
  await user.type(screen.getByLabelText("Password"), "correct horse");
  await user.click(screen.getByRole("button", { name: "Log in" }));
  await screen.findByRole("heading", { name: "Dashboard" });
  return { user, ...rendered };
}

describe("the shell on a wide screen", () => {
  beforeEach(desktop);
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the navigation in the header, without a menu button or a sheet", async () => {
    await logIn();
    const header = screen.getByRole("banner");
    const nav = within(header).getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("button", { name: "Menu" })).toBeNull();
    expect(screen.getAllByRole("navigation", { name: "Main" })).toHaveLength(1);
    const brand = within(header).getByRole("link", { name: "Homelab Probe, home" });
    expect(brand).toHaveAttribute("href", "/");
    expect(brand.textContent).toBe("HOMELAB PROBE");
    expect(brand.querySelector("img, svg")).toBeNull();
  });

  it("opens the account menu with who is signed in, the profile, the theme and logging out; Escape closes it", async () => {
    const { user } = await logIn();
    const button = screen.getByRole("button", { name: /Account:\s*demo/ });
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Administrator")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Theme" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveFocus();
  });

  it("closes the account menu on a click elsewhere and when a page is chosen in it", async () => {
    const { user } = await logIn();
    const button = screen.getByRole("button", { name: /Account:\s*demo/ });
    await user.click(button);
    await user.click(screen.getByRole("heading", { name: "Dashboard" }));
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    const panel = document.getElementById(button.getAttribute("aria-controls") ?? "");
    expect(panel).not.toBeNull();
    await user.click(within(panel as HTMLElement).getByRole("link", { name: "Profile" }));
    expect(await screen.findByRole("heading", { name: "Profile" })).toBeInTheDocument();
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("logs out from the account menu", async () => {
    const { user } = await logIn();
    await user.click(screen.getByRole("button", { name: /Account:\s*demo/ }));
    await user.click(screen.getByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });
});

describe("the footer", () => {
  it("links the project's pages in a new tab without giving them this page, and states the version and the promise", async () => {
    await logIn();
    const footer = screen.getByRole("contentinfo");
    const brand = footer.querySelector(".brand");
    expect(brand?.textContent).toBe("HOMELAB PROBE");
    expect(brand?.querySelector("img, svg")).toBeNull();
    const project = within(footer).getByRole("navigation", { name: "Project" });
    const links = within(project).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(FOOTER_LINKS.map((link) => link.href));
    for (const link of within(footer).getAllByRole("link")) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
      expect(link).toHaveTextContent("(opens in a new tab)");
    }
    expect(footer).toHaveTextContent("Homelab Probe 0.0.0-test. Read-only: nothing here changes your controller.");
    expect(footer).toHaveTextContent("Forked from ericfitz/unifi-clients-export (opens in a new tab).");
  });
});

describe("the terminal in the shell", () => {
  beforeEach(desktop);
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not exist without a provider: no button, no panel", async () => {
    await logIn();
    expect(screen.queryByRole("button", { name: "Terminal" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Terminal" })).toBeNull();
  });

  it("opens and closes from the header button, and keeps its state when the page changes", async () => {
    const { user } = await logIn(true);
    const button = screen.getByRole("button", { name: "Terminal" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    expect(await screen.findByRole("region", { name: "Terminal" })).toBeInTheDocument();
    expect(button).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("link", { name: "Profile" }));
    await screen.findByRole("heading", { name: "Profile" });
    expect(screen.getByRole("region", { name: "Terminal" })).toBeInTheDocument();
    await user.click(button);
    expect(screen.queryByRole("region", { name: "Terminal" })).toBeNull();
  });

  it("clears the terminal when the user changes without unmounting the shell", async () => {
    const { user, services } = await logIn(true);
    await user.click(screen.getByRole("button", { name: "Terminal" }));
    await screen.findByText("Ready");
    await user.click(await screen.findByRole("button", { name: "run info" }));
    await waitFor(() => { expect(screen.getByRole("log", { name: "Terminal transcript" })).toHaveTextContent("Application:"); });
    const previous = services.queryClient.getQueryData<Session>(SESSION_KEY);
    act(() => { services.queryClient.setQueryData(SESSION_KEY, { ...previous, username: "viewer", role: "viewer" }); });
    await waitFor(() => { expect(screen.getByRole("log", { name: "Terminal transcript" })).toBeEmptyDOMElement(); });
    await screen.findByText("Ready");
    expect(screen.getByRole("log", { name: "Terminal transcript" })).toBeEmptyDOMElement();
  });

  it.each(["logout", "expiry"])("removes the terminal on %s", async (reason) => {
    const { user, services, fake } = await logIn(true);
    await user.click(screen.getByRole("button", { name: "Terminal" }));
    await screen.findByText("Ready");
    await user.click(await screen.findByRole("button", { name: "run info" }));
    if (reason === "logout") {
      await user.click(screen.getByRole("button", { name: /Account:\s*demo/ }));
      await user.click(screen.getByRole("button", { name: "Log out" }));
    } else {
      fake.restartServer();
      await act(async () => { await services.client.get("/platforms").catch(() => undefined); });
    }
    await screen.findByRole("heading", { name: "Log in" });
    expect(screen.queryByRole("region", { name: "Terminal" })).toBeNull();
    expect(screen.queryByRole("log", { name: "Terminal transcript" })).toBeNull();
  });
});
