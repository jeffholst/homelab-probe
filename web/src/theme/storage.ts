/**
 * The only module that may touch the browser's storage (eslint.config.js forbids it everywhere else), and the only
 * things it stores are the theme preference (one of three words) and the height of the terminal panel (a number of
 * pixels). Never a token, a password, a name or anything read from
 * the server. Storage can be missing, full or blocked (a private window, a policy): every access is guarded and the
 * app works without it, it just forgets the choice on reload.
 *
 * The brand is dark first: "dark" is the default and sets no attribute; "light" and "system" set `data-theme`, which
 * tokens.css reads ("system" follows the operating system's light or dark preference).
 */
export type ThemePreference = "dark" | "light" | "system";

export const THEME_KEY = "hlp-theme";
export const DEFAULT_THEME: ThemePreference = "dark";
export const THEME_CHOICES: readonly ThemePreference[] = ["dark", "light", "system"];

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "dark" || value === "light" || value === "system";
}

export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return isThemePreference(stored) ? stored : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

export function writeThemePreference(preference: ThemePreference): void {
  try {
    if (preference === DEFAULT_THEME) window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, preference);
  } catch {
    // Not stored: the choice still applies to this page.
  }
}

/** Puts the preference on the root element, where tokens.css reads it (the default, dark, means no attribute). */
export function applyThemePreference(preference: ThemePreference, root: HTMLElement = document.documentElement): void {
  if (preference === DEFAULT_THEME) root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
}

/** The terminal panel's height in pixels: the one preference of the panel that is remembered (never its content). */
export const PANEL_HEIGHT_KEY = "hlp-terminal-height";
export const MIN_PANEL_HEIGHT = 160;
export const DEFAULT_PANEL_HEIGHT = 320;

export function clampPanelHeight(value: number, max = 4000): number {
  return Number.isFinite(value) ? Math.min(Math.max(Math.round(value), MIN_PANEL_HEIGHT), max) : DEFAULT_PANEL_HEIGHT;
}

export function readPanelHeight(): number {
  try {
    const stored = Number(window.localStorage.getItem(PANEL_HEIGHT_KEY));
    return window.localStorage.getItem(PANEL_HEIGHT_KEY) === null ? DEFAULT_PANEL_HEIGHT : clampPanelHeight(stored);
  } catch {
    return DEFAULT_PANEL_HEIGHT;
  }
}

export function writePanelHeight(height: number): void {
  try {
    if (height === DEFAULT_PANEL_HEIGHT) window.localStorage.removeItem(PANEL_HEIGHT_KEY);
    else window.localStorage.setItem(PANEL_HEIGHT_KEY, String(clampPanelHeight(height)));
  } catch {
    // Not stored: the height still applies to this page.
  }
}
