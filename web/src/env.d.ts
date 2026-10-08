/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" in a build that includes the terminal with its mock backend (development and the browser tests). */
  readonly VITE_TERMINAL_MOCK?: string;
  /** "1" in a preview build that connects the terminal to the authenticated API. */
  readonly VITE_TERMINAL_API_PREVIEW?: string;
}
