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
  it("defaults to the system theme, which sets no attribute", () => {
    renderWithProviders(<ThemeSwitch />);
    expect(screen.getByLabelText("Theme")).toHaveValue("system");
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("applies light and dark at once and remembers them (one word, nothing else, in storage)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ThemeSwitch />);
    await user.selectOptions(screen.getByLabelText("Theme"), "dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("dark");
    expect(window.localStorage.length).toBe(1);
    await user.selectOptions(screen.getByLabelText("Theme"), "light");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    await user.selectOptions(screen.getByLabelText("Theme"), "system");
    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(window.localStorage.length).toBe(0);
  });

  it("starts from the stored choice", () => {
    window.localStorage.setItem(THEME_KEY, "dark");
    renderWithProviders(<ThemeSwitch />);
    expect(screen.getByLabelText("Theme")).toHaveValue("dark");
  });

  it("ignores a stored value that is not one of the three words", () => {
    window.localStorage.setItem(THEME_KEY, "<script>");
    expect(readThemePreference()).toBe("system");
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
    expect(screen.getByLabelText("Theme")).toHaveValue("system");
    await user.selectOptions(screen.getByLabelText("Theme"), "dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(() => {
      writeThemePreference("light");
    }).not.toThrow();
  });

  it("applies to any root element", () => {
    const root = document.createElement("div");
    applyThemePreference("dark", root);
    expect(root.dataset["theme"]).toBe("dark");
    applyThemePreference("system", root);
    expect(root.dataset["theme"]).toBeUndefined();
  });

  it("uses the key and the words that public/theme-init.js (which runs before the first paint) uses", () => {
    const script = readFileSync(resolve(process.cwd(), "public/theme-init.js"), "utf8");
    expect(script).toContain(`getItem("${THEME_KEY}")`);
    for (const choice of THEME_CHOICES.filter((word) => word !== "system")) expect(script).toContain(`"${choice}"`);
  });
});
