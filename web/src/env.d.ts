/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" in a build that includes the terminal with its mock backend (development and the browser tests). */
  readonly VITE_TERMINAL_MOCK?: string;
}
