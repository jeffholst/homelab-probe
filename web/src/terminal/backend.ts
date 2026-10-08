import type { Argv, Capabilities, EngineApi } from "./engine/types";

/** What a command came back as, already reduced to what the panel may show. Report text is plain text, untrusted. */
export type ExecuteOutcome =
  | { readonly kind: "output"; readonly text: string; readonly truncated: boolean }
  /** `ran`: "no" when the command surely did not run (validation, permission), "unknown" after a timeout or a lost
   * connection (#277 decides which, the panel only displays it). */
  | { readonly kind: "failed"; readonly message: string; readonly ran: "no" | "unknown" };

/**
 * What the panel needs from the server. #276 runs against a mock of it; #277 provides the real one (capabilities,
 * execute with the session, CSRF, a single request per submission and no retries).
 */
export interface TerminalBackend {
  loadCapabilities(): Promise<Capabilities>;
  execute(argv: Argv): Promise<ExecuteOutcome>;
}

/** The engine and the backend the panel is built with; null (no provider) means the terminal does not exist. */
export interface TerminalServices {
  readonly backend: TerminalBackend;
  readonly engine: EngineApi;
}
