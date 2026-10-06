/**
 * The only module that may touch the browser's storage (eslint.config.js forbids it everywhere else), and the only
 * thing it stores is the theme preference: one of three words. Never a token, a password, a name or anything read from
 * the server. Storage can be missing, full or blocked (a private window, a policy): every access is guarded and the
 * app works without it, it just forgets the choice on reload.
 */
export type ThemePreference = "system" | "light" | "dark";

export const THEME_KEY = "hlp-theme";
export const THEME_CHOICES: readonly ThemePreference[] = ["system", "light", "dark"];

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function writeThemePreference(preference: ThemePreference): void {
  try {
    if (preference === "system") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, preference);
  } catch {
    // Not stored: the choice still applies to this page.
  }
}

/** Puts the preference on the root element, where tokens.css reads it ("system" means no attribute). */
export function applyThemePreference(preference: ThemePreference, root: HTMLElement = document.documentElement): void {
  if (preference === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
}
