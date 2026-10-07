import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderApp } from "../test/render";
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

async function logIn() {
  const user = userEvent.setup();
  const rendered = renderApp("/");
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
    expect(within(header).getByRole("link", { name: "Homelab Probe, home" })).toHaveAttribute("href", "/");
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
