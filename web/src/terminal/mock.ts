/**
 * A mock backend for the panel (#276): canned capabilities and canned reports from synthetic data, so the panel can be
 * built, tested and previewed without a server. It is compiled into the app ONLY when the build sets
 * `VITE_TERMINAL_MOCK=1` (the browser tests do); a normal build does not contain it. #277 replaces it with the real API.
 */
import type { ExecuteOutcome, TerminalBackend } from "./backend";
import type { Argv, Capabilities, CommandMeta, OptionMeta } from "./engine/types";

const json: OptionMeta = { flags: ["--json"], description: "Output JSON instead of a table", takesValue: false, repeatable: false, commaList: false, status: "supported", reason: "Reviewed read-only report argument.", choices: [] };
const search: OptionMeta = { ...json, flags: ["-s", "--search"], description: "Case-insensitive substring match on any field", takesValue: true };

function command(name: string, description: string, choices: string[] = [], options: OptionMeta[] = [json]): CommandMeta {
  return { name, description, status: "supported", reason: "Reviewed read-only report.", choices, options };
}

export const MOCK_CAPABILITIES: Capabilities = {
  version: 1,
  commands: [
    command("diagnose", "Check the network and list what needs attention"),
    command("events", "List recent events from the event log"),
    command("info", "Show the controller version and sites", [], []),
    command("new-clients", "List new clients: first seen recently, or in no group", [], [json, search]),
    command("query", "List and filter devices, clients, reservations, switch ports, networks and Wi-Fi networks", ["all", "devices", "clients", "reservations", "ports", "networks", "wlans"], [json, search]),
    command("wan", "Internet health and speed history"),
    command("wifi", "Wi-Fi quality by client and access point"),
  ],
  globalOptions: [{ ...json, flags: ["--site"], description: "Which site to read", takesValue: true, status: "restricted" }],
  staticOperations: ["help", "version"],
  limits: { bodyBytes: 16384, tokenCount: 64, tokenChars: 256, outputBytes: 262144, perUserConcurrency: 1, perUserPerMinute: 30, globalConcurrency: 4, eventRows: 2000, eventWindowSeconds: 1209600, wanDays: 3650, completionCandidates: 64, completionDescriptionChars: 500 },
  cancellation: "Stopping waiting does not cancel controller work.",
};

const CLIENTS = [
  "Name         MAC Address        IP Address  Connection Type  Status",
  "-----------  -----------------  ----------  ---------------  -------",
  "desktop      BB:00:00:00:00:01  10.0.0.10   Wired            Online",
  "guest-phone  BE:00:00:00:00:06  10.0.0.52   Wireless         Offline",
  "old-printer  BB:00:00:00:00:03  10.0.0.50   Wired            Offline",
  "",
  "3 row(s)",
].join("\n");

function later<T>(value: T, milliseconds: number): Promise<T> {
  return new Promise((resolve) => setTimeout(() => { resolve(value); }, milliseconds));
}

/** Report text that tries every trick; the panel must show it as plain text and nothing more. */
const HOSTILE = [
  "normal line",
  "\u001b[2J\u001b[Hcleared screen?",
  "\u001b]0;forged title\u0007title?",
  "\u001b]52;c;aGVsbG8=\u0007clipboard?",
  "\u001b]8;;https://example.invalid\u0007link?\u001b]8;;\u0007",
  "\u001b[6n cursor report?",
  "\r\u001b[K[CRITICAL] forged status line",
  "bidi \u202eevil\u202c and zero\u200bwidth",
].join("\n");

export function createMockBackend(options: { delay?: number } = {}): TerminalBackend {
  const delay = options.delay ?? 250;
  return {
    loadCapabilities: () => later(MOCK_CAPABILITIES, 80),
    execute: (argv: Argv): Promise<ExecuteOutcome> => {
      const [name, ...rest] = argv;
      if (name === "query" && rest[0] === "clients") return later({ kind: "output", text: CLIENTS, truncated: false }, delay);
      if (name === "info") return later({ kind: "output", text: "Application: 10.6.106 (demo)\nSite: Default ref=default", truncated: false }, delay);
      if (name === "events") return later({ kind: "output", text: Array.from({ length: 40 }, (_, i) => `2026-10-08 10:${String(i).padStart(2, "0")}  device state  Garage AP changed state`).join("\n"), truncated: true }, delay);
      if (name === "diagnose") return later({ kind: "failed", message: "The controller did not answer in time.", ran: "unknown" }, delay);
      if (name === "wan") return later({ kind: "failed", message: "That command is not available in the browser.", ran: "no" }, delay);
      if (name === "wifi") return later({ kind: "output", text: HOSTILE, truncated: false }, delay);
      if (name === "slow") return later({ kind: "output", text: "done", truncated: false }, 4000);
      return later({ kind: "output", text: `(mock) ${argv.join(" ")}`, truncated: false }, delay);
    },
  };
}
