/**
 * Report documents for the fake API: a synthetic dashboard (the shape of `GET .../dashboard` from the demo network:
 * made-up names, documentation addresses) and the site list. Tests change a copy with `dashboardWith`.
 */
import type { Dashboard } from "../api/reports";

export const FAKE_SITES = [{ name: "Default", ref: "default", id: "site-1" }];

export function dashboardFixture(): Dashboard {
  return {
    version: 1,
    status: "critical",
    complete: true,
    stale: false,
    controller: { state: "ok" },
    site: { id: "site-1", name: "Default" },
    findings: {
      available: true,
      total: 16,
      ignored: 0,
      by_severity: { critical: 1, warning: 12, info: 3 },
      by_state: { open: 16, acknowledged: 0, snoozed: 0 },
      triage_available: true,
      attention: [
        { id: "a96cb5d0bf127d3a", rank: 1, severity: "critical", code: "device.overheating", subject: "Gateway", message: "reports that it is overheating" },
        { id: "06842c294b4ab630", rank: 2, severity: "warning", code: "device.cpu_high", subject: "Office Switch", message: "CPU utilization 95%" },
      ],
    },
    devices: { available: true, total: 4, online: 3, offline: 1, other: 0 },
    clients: { available: true, connected: 2, wired: 1, wireless: 1, offline: 2 },
    wan: {
      available: true,
      status: "ok",
      internet_status: "ok",
      latency_ms: 20,
      drops: 0,
      availability_pct: 99.5,
      nat: "public",
      last_speedtest: { time: Date.now() - 3 * 3_600_000, download_mbps: 880, upload_mbps: 40 },
    },
    wifi: { available: true, access_points: 2, access_points_online: 1, radios: 2, wireless_clients: 1, lowest_satisfaction: 99, highest_utilization: 30 },
    events: {
      available: true,
      window_seconds: 86_400,
      total: 10,
      truncated: false,
      notable_total: 1,
      notable: [
        { timestamp: Date.now() - 3_600_000, severity: "high", category: "UNIFI_DEVICES", event: "DEVICE_UNREACHABLE", message: "Garage AP is unreachable." },
      ],
    },
  };
}

/** The fixture with some top-level keys replaced. */
export function dashboardWith(changes: Partial<Dashboard>): Dashboard {
  return { ...dashboardFixture(), ...changes };
}

/** What a controller that cannot be reached gives: a 200 that says so. */
export function unreachableDashboard(state: "unreachable" | "certificate" | "key_rejected" = "unreachable"): Dashboard {
  const none = { available: false };
  return {
    version: 1,
    status: "unknown",
    complete: false,
    stale: false,
    controller: { state },
    site: null,
    findings: none,
    devices: none,
    clients: none,
    wan: none,
    wifi: none,
    events: none,
  };
}
