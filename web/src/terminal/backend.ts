import type { Argv, Capabilities, EngineApi, Effect, Candidate } from "./engine/types";

/** What a command came back as, already reduced to what the panel may show. Report text is plain text, untrusted. */
export type ExecuteOutcome =
  | { readonly kind: "output"; readonly text: string; readonly truncated: boolean; readonly warnings?: readonly string[] }
  /** `ran`: "no" for a refusal before dispatch, "failed" for a documented dispatched failure,
   * "unknown" after a timeout, lost connection or untrustworthy reply. */
  | { readonly kind: "failed"; readonly message: string; readonly ran: "no" | "unknown" | "failed" };

export type CompletionRequest = Extract<Effect, { type: "complete" }>;
export interface CompletionOutcome {
  readonly candidates: readonly Candidate[];
  readonly truncated: boolean;
}

/**
 * What the panel needs from the server. `api.ts` provides the real implementation; mocks may omit remote completion.
 * Signals stop waiting only, not server execution. Calls bypass query caching and automatic retry machinery.
 */
export interface TerminalBackend {
  loadCapabilities(this: void, signal?: AbortSignal): Promise<Capabilities>;
  execute(this: void, argv: Argv, signal?: AbortSignal): Promise<ExecuteOutcome>;
  complete?(this: void, request: CompletionRequest, signal?: AbortSignal): Promise<CompletionOutcome>;
}

/** The engine and the backend the panel is built with; null (no provider) means the terminal does not exist. */
export interface TerminalServices {
  readonly backend: TerminalBackend;
  readonly engine: EngineApi;
}
