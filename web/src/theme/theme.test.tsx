// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ThemeSwitch } from "../components/ThemeSwitch";
import { renderWithProviders } from "../test/render";
import { THEME_CHOICES, THEME_KEY, applyThemePreference, readThemePreference, writeThemePreference } from "./storage";

describe("the theme preference", () => {
  it("defaults to the brand's dark theme, which sets no attribute", () => {
    renderWithProviders(<ThemeSwitch />);
    expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked();
    expect(screen.getByRole("group", { name: "Theme" })).toBeInTheDocument();
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("applies light and system at once and remembers them (one word, nothing else, in storage)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ThemeSwitch />);
    await user.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("light");
    expect(window.localStorage.length).toBe(1);
    await user.click(screen.getByRole("radio", { name: "System" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "system");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("system");
    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(window.localStorage.length).toBe(0);
  });

  it("starts from the stored choice", () => {
    window.localStorage.setItem(THEME_KEY, "light");
    renderWithProviders(<ThemeSwitch compact />);
    expect(screen.getByRole("radio", { name: "Light" })).toBeChecked();
  });

  it("ignores a stored value that is not one of the three words", () => {
    window.localStorage.setItem(THEME_KEY, "<script>");
    expect(readThemePreference()).toBe("dark");
  });

  it("keeps working when storage is blocked", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    renderWithProviders(<ThemeSwitch />);
    expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(() => {
      writeThemePreference("light");
      writeThemePreference("dark");
    }).not.toThrow();
  });

  it("applies to any root element", () => {
    const root = document.createElement("div");
    applyThemePreference("system", root);
    expect(root.dataset["theme"]).toBe("system");
    applyThemePreference("dark", root);
    expect(root.dataset["theme"]).toBeUndefined();
  });

  it("uses the key and the words that public/theme-init.js (which runs before the first paint) uses", () => {
    const script = readFileSync(resolve(process.cwd(), "public/theme-init.js"), "utf8");
    expect(script).toContain(`getItem("${THEME_KEY}")`);
    for (const choice of THEME_CHOICES.filter((word) => word !== "dark")) expect(script).toContain(`"${choice}"`);
  });
});
