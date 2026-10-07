/**
 * The report routes of the web API (`/api/v1/unifi/...`): the controller's sites and the dashboard summary.
 *
 * Every report answer is the command's `--json` document (types generated from docs/schemas in src/generated) plus
 * `generated_at` (when it was read, ISO 8601 UTC) and `warnings` (what could not be read). The parsers check what the
 * page relies on (the version, the states it branches on, that every section says whether it was read) so an answer
 * of another shape is a `bad_response` error, never a page that looks healthy by accident. Names and messages in them
 * come from the controller: render them with `<Text>`.
 */
import { segment, type ApiClient } from "./client";
import { bad, flag, record, text } from "./types";
import type { Dashboard } from "../generated/dashboard.v1";

export type { Dashboard };

/** What every report answer adds to its document. */
export interface Envelope {
  generated_at: string;
  warnings: string[];
}

export interface Site {
  name: string;
  ref: string;
  id: string;
}

export interface SitesAnswer extends Envelope {
  sites: Site[];
}

export type DashboardAnswer = Dashboard & Envelope;

const STATUSES = new Set(["ok", "warning", "critical", "unknown"]);
const CONTROLLER_STATES = new Set(["ok", "unreachable", "certificate", "key_rejected"]);
const SECTIONS = ["findings", "devices", "clients", "wan", "wifi", "events"] as const;

function envelope(fields: Record<string, unknown>, what: string): Envelope {
  const warnings = fields["warnings"];
  if (!Array.isArray(warnings) || warnings.some((item) => typeof item !== "string")) throw bad(what);
  return { generated_at: text(fields, "generated_at", what), warnings: warnings as string[] };
}

export function parseSites(value: unknown): SitesAnswer {
  const what = "the sites";
  const fields = record(value, what);
  const sites = fields["sites"];
  if (!Array.isArray(sites)) throw bad(what);
  return {
    ...envelope(fields, what),
    // A site with no reference cannot be asked for: it is left out rather than guessed.
    sites: sites
      .map((entry: unknown) => record(entry, what))
      .filter((site) => typeof site["ref"] === "string" && site["ref"] !== "")
      .map((site) => ({
        ref: site["ref"] as string,
        name: typeof site["name"] === "string" ? site["name"] : "",
        id: typeof site["id"] === "string" ? site["id"] : "",
      })),
  };
}

export function parseDashboard(value: unknown): DashboardAnswer {
  const what = "the dashboard";
  const fields = record(value, what);
  if (fields["version"] !== 1) throw bad(what);
  if (typeof fields["status"] !== "string" || !STATUSES.has(fields["status"])) throw bad(what);
  flag(fields, "complete", what);
  flag(fields, "stale", what);
  const controller = record(fields["controller"], what);
  if (typeof controller["state"] !== "string" || !CONTROLLER_STATES.has(controller["state"])) throw bad(what);
  const site = fields["site"];
  if (site !== null) {
    const known = record(site, what);
    text(known, "id", what);
    text(known, "name", what);
  }
  for (const name of SECTIONS) flag(record(fields[name], what), "available", what);
  envelope(fields, what);
  return value as DashboardAnswer;
}

export interface ReportsApi {
  sites(signal?: AbortSignal): Promise<SitesAnswer>;
  /** `refresh` reads the controller again instead of the cache (the server allows it every 5 s at most). */
  dashboard(site: string, refresh?: boolean, signal?: AbortSignal): Promise<DashboardAnswer>;
}

export function createReportsApi(client: ApiClient): ReportsApi {
  return {
    sites: (signal) => client.get("/unifi/sites", { signal, parse: parseSites }),
    dashboard: (site, refresh = false, signal) =>
      client.get(`/unifi/sites/${segment(site)}/dashboard`, {
        signal,
        query: refresh ? { refresh: true } : {},
        parse: parseDashboard,
      }),
  };
}
