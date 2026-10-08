import type { TerminalCapabilitiesV1, TerminalCompleteRequestV1, TerminalExecuteRequestV1 } from "../generated";
import type { ApiClient } from "../api/client";
import { isApiError } from "../api/errors";
import { bad, flag, record, text } from "../api/types";
import type { TerminalBackend, ExecuteOutcome } from "./backend";
import { cleanLines } from "./clean";
import type { Capabilities, OptionMeta, Candidate } from "./engine/types";

const WHAT = "the terminal";
const clean = (value: string) => cleanLines(value).join("\n");

function strings(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item: unknown) => typeof item === "string")) throw bad(WHAT);
  return value.map((item: string) => item);
}

function list<T>(value: unknown, parse: (value: unknown) => T): T[] {
  if (!Array.isArray(value)) throw bad(WHAT);
  return value.map(parse);
}

function status(fields: Record<string, unknown>): OptionMeta["status"] {
  const value = fields["status"];
  if (value !== "supported" && value !== "restricted" && value !== "unavailable") throw bad(WHAT);
  return value;
}

function option(value: unknown): OptionMeta {
  const f = record(value, WHAT);
  return {
    flags: strings(f["flags"]), description: text(f, "description", WHAT), reason: text(f, "reason", WHAT),
    choices: strings(f["choices"]), status: status(f), takesValue: flag(f, "takesValue", WHAT),
    repeatable: flag(f, "repeatable", WHAT), commaList: flag(f, "commaList", WHAT),
  };
}

function positive(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) throw bad(WHAT);
  return value;
}

/** Validate and copy the published shape; malformed metadata never becomes a fallback command list. */
export function parseCapabilities(value: unknown): Capabilities {
  const f = record(value, WHAT);
  if (f["version"] !== 1) throw bad(WHAT);
  const l = record(f["limits"], WHAT);
  const limits: TerminalCapabilitiesV1.TerminalLimits = {
    bodyBytes: positive(l["bodyBytes"]), tokenCount: positive(l["tokenCount"]), tokenChars: positive(l["tokenChars"]),
    outputBytes: positive(l["outputBytes"]), perUserConcurrency: positive(l["perUserConcurrency"]),
    perUserPerMinute: positive(l["perUserPerMinute"]), globalConcurrency: positive(l["globalConcurrency"]),
    eventRows: positive(l["eventRows"]), eventWindowSeconds: positive(l["eventWindowSeconds"]), wanDays: positive(l["wanDays"]),
    completionCandidates: positive(l["completionCandidates"]), completionDescriptionChars: positive(l["completionDescriptionChars"]),
  };
  return {
    version: 1, cancellation: text(f, "cancellation", WHAT), staticOperations: strings(f["staticOperations"]), limits,
    globalOptions: list(f["globalOptions"], option),
    commands: list(f["commands"], (value) => {
      const c = record(value, WHAT);
      return { name: text(c, "name", WHAT), description: text(c, "description", WHAT), reason: text(c, "reason", WHAT),
        status: status(c), choices: strings(c["choices"]), options: list(c["options"], option) };
    }),
  };
}

function correlated(value: unknown, requestId: string): Record<string, unknown> {
  const f = record(value, WHAT);
  if (text(f, "requestId", WHAT) !== requestId || !text(f, "correlationId", WHAT)) throw bad(WHAT);
  return f;
}

function exact(fields: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(fields).some((key) => !keys.includes(key))) throw bad(WHAT);
}

function failure(error: unknown): ExecuteOutcome {
  if (!isApiError(error)) return { kind: "failed", ran: "unknown", message: "The request failed or waiting timed out. The command may still be running." };
  const executionStatus = error.details["executionStatus"];
  const ran = executionStatus === "did_not_run" ? "no" : executionStatus === "failed" ? "failed"
    : executionStatus === "outcome_unknown" ? "unknown" : [401, 403, 422, 429].includes(error.status) ? "no" : "unknown";
  const retry = error.status === 429 && error.retryAfter ? ` Wait ${error.retryAfter} second${error.retryAfter === 1 ? "" : "s"} before trying again.` : "";
  return { kind: "failed", ran, message: clean(error.message).slice(0, 1000) + retry };
}

/** Direct client calls only: no query/mutation cache, automatic retry, or execution queue. */
export function createTerminalBackend(client: ApiClient, options: { requestId?: () => string; timeoutMs?: number } = {}): TerminalBackend {
  const id = options.requestId ?? (() => crypto.randomUUID());
  async function request<T>(run: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
    const controller = new AbortController();
    const abort = () => { controller.abort(); };
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    const timer = setTimeout(abort, options.timeoutMs ?? 35_000);
    try { return await run(controller.signal); }
    finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
  }
  return {
    loadCapabilities: (signal) => request((signal) => client.get("/terminal/capabilities", { signal, parse: parseCapabilities }), signal),
    async execute(argv, signal) {
      try {
        const requestId = id();
        return await request((signal) => client.post<ExecuteOutcome>("/terminal/execute", {
          signal, body: { argv, requestId } satisfies TerminalExecuteRequestV1.ExecuteBody, parse: (value) => {
            const f = correlated(value, requestId);
            exact(f, ["correlationId", "requestId", "operation", "status", "output", "warnings", "truncated"]);
            if (f["status"] !== "completed") throw bad(WHAT);
            text(f, "operation", WHAT);
            const output = text(f, "output", WHAT);
            const warnings = strings(f["warnings"]);
            if (new TextEncoder().encode(output).length > 262144 || warnings.length > 32 || warnings.some((w) => [...w].length > 500)) throw bad(WHAT);
            return { kind: "output", text: clean(output), truncated: flag(f, "truncated", WHAT), warnings: warnings.map(clean) };
          },
        }), signal);
      } catch (error) { return failure(error); }
    },
    async complete(input, signal) {
      const requestId = id();
      return request((signal) => client.post("/terminal/complete", {
        signal, body: { argv: input.argv, tokenIndex: input.tokenIndex, cursor: input.cursor, requestId } satisfies TerminalCompleteRequestV1.CompleteBody,
        parse: (value) => {
          const f = correlated(value, requestId);
          exact(f, ["correlationId", "requestId", "tokenIndex", "candidates", "truncated"]);
          if (f["tokenIndex"] !== input.tokenIndex) throw bad(WHAT);
          const candidates = list<Candidate>(f["candidates"], (value) => {
            const c = record(value, WHAT);
            exact(c, ["label", "description", "kind"]);
            const kind = c["kind"];
            const label = text(c, "label", WHAT), description = text(c, "description", WHAT);
            if ([...label].length > 256 || [...description].length > 500) throw bad(WHAT);
            if (kind !== "command" && kind !== "option" && kind !== "choice") throw bad(WHAT);
            return { label, description, kind };
          });
          if (candidates.length > 64) throw bad(WHAT);
          return { candidates, truncated: flag(f, "truncated", WHAT) };
        },
      }), signal);
    },
  };
}
