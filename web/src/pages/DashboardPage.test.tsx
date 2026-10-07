import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { parseDashboard, parseSites } from "../api/reports";
import { formatAgo } from "../lib/format";
import { FakeApi } from "../test/fakeApi";
import { dashboardFixture, dashboardWith, unreachableDashboard } from "../test/fakeReports";
import { renderApp } from "../test/render";
import { chooseSite } from "./DashboardPage";

async function open(fake = new FakeApi(), route = "/") {
  const user = userEvent.setup();
  const rendered = renderApp(route, { fake });
  await user.type(await screen.findByLabelText("User name"), "demo");
  await user.type(screen.getByLabelText("Password"), "correct horse");
  await user.click(screen.getByRole("button", { name: "Log in" }));
  await screen.findByRole("heading", { name: "Dashboard", level: 1 });
  return { user, ...rendered };
}

const section = (name: string) => screen.getByRole("region", { name });

describe("the dashboard", () => {
  it("says how the network is, with the counts behind it and every severity written out", async () => {
    await open();
    expect(await screen.findByRole("heading", { name: "Critical problems found" })).toBeInTheDocument();
    expect(screen.getByText("1 critical finding, 12 warnings, 3 notes.")).toBeInTheDocument();
    expect(screen.getByText(/16 open, 0 acknowledged, 0 snoozed/)).toBeInTheDocument();
    const attention = within(section("Needs attention"));
    expect(attention.getByText("Critical")).toBeInTheDocument();
    expect(attention.getByText("Gateway")).toBeInTheDocument();
    expect(attention.getByText("device.overheating")).toBeInTheDocument();
    expect(attention.getByText(/The first 2 of 16 findings in all/)).toBeInTheDocument();
    expect(within(section("Devices")).getByText("Online").nextSibling).toHaveTextContent("3 of 4");
    expect(within(section("Clients")).getByText("Connected").nextSibling).toHaveTextContent("2");
    expect(within(section("Internet")).getByText("Latency").nextSibling).toHaveTextContent("20 ms");
    expect(within(section("Internet")).getByText("Availability (24 h)").nextSibling).toHaveTextContent("99.5%");
    expect(within(section("Wi-Fi")).getByText("Access points online").nextSibling).toHaveTextContent("1 of 2");
    expect(within(section("Events (last 24 h)")).getByText("Garage AP is unreachable.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
  });

  it("calls the network healthy only for status ok", async () => {
    const fake = new FakeApi();
    fake.dashboard = dashboardWith({ status: "ok" });
    await open(fake);
    expect(await screen.findByRole("heading", { name: "All checks passed" })).toBeInTheDocument();
    expect(screen.getByText(/on a complete and fresh read/)).toBeInTheDocument();
  });

  it("never calls a partial read healthy, and lists what could not be read", async () => {
    const fake = new FakeApi();
    fake.dashboard = dashboardWith({ status: "unknown", complete: false, wan: { available: false } });
    fake.reportWarnings = ["stat/health unavailable"];
    await open(fake);
    expect(await screen.findByRole("heading", { name: "Health not known" })).toBeInTheDocument();
    expect(screen.getByText("Some data could not be read, so a healthy result cannot be claimed.")).toBeInTheDocument();
    expect(screen.getByText("Some data could not be read")).toBeInTheDocument();
    expect(screen.getByText("stat/health unavailable")).toBeInTheDocument();
    expect(within(section("Internet")).getByText(/Not read: this could not be read/)).toBeInTheDocument();
    expect(within(section("Internet")).queryByText("Latency")).toBeNull();
    expect(screen.queryByText("All checks passed")).toBeNull();
  });

  it.each([
    ["unreachable", "The controller could not be reached"],
    ["certificate", "The controller's certificate was not accepted"],
    ["key_rejected", "The controller refused the API key"],
  ] as const)("shows a controller that cannot be read (%s) without a single number", async (state, title) => {
    const fake = new FakeApi();
    fake.dashboard = unreachableDashboard(state);
    fake.reportWarnings = [`the controller could not be read (${state}); nothing is known about the network`];
    await open(fake);
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Health not known" })).toBeInTheDocument();
    expect(screen.getByText("The controller could not be read.")).toBeInTheDocument();
    expect(screen.getAllByText(/Not read: this could not be read/)).toHaveLength(6);
    for (const name of ["Devices", "Clients", "Internet", "Wi-Fi"]) expect(within(section(name)).queryByText(/\d/)).toBeNull();
  });

  it("says when it shows an older answer because the latest read failed", async () => {
    const fake = new FakeApi();
    fake.dashboard = dashboardWith({ status: "unknown", stale: true, controller: { state: "unreachable" } });
    await open(fake);
    expect(await screen.findByText("The controller could not be reached")).toBeInTheDocument();
    expect(screen.getByText(/What you see is the last answer that was read/)).toBeInTheDocument();
    expect(screen.getByText("The controller could not be read.")).toBeInTheDocument();
  });

  it("says when an older answer is shown while the controller answers again", async () => {
    const fake = new FakeApi();
    fake.dashboard = dashboardWith({ status: "unknown", stale: true });
    await open(fake);
    expect(await screen.findByText("Showing older data")).toBeInTheDocument();
    expect(screen.getByText("The data is older than it should be.")).toBeInTheDocument();
  });

  it("writes Unknown for a number the server does not know, never zero", async () => {
    const fake = new FakeApi();
    const base = dashboardFixture();
    fake.dashboard = dashboardWith({
      clients: { ...base.clients, offline: null },
      wan: { ...base.wan, latency_ms: null, last_speedtest: null },
      findings: { ...base.findings, by_state: null, triage_available: false },
    });
    await open(fake);
    expect(await screen.findByText(/Triage state could not be read/)).toBeInTheDocument();
    expect(within(section("Clients")).getByText("Seen before, not connected").nextSibling).toHaveTextContent("Unknown");
    expect(within(section("Internet")).getByText("Latency").nextSibling).toHaveTextContent("Unknown");
    expect(within(section("Internet")).getByText("Last speed test").nextSibling).toHaveTextContent("None");
  });

  it("explains an empty attention list, and the notes of a complete read", async () => {
    const fake = new FakeApi();
    const base = dashboardFixture();
    fake.dashboard = dashboardWith({ findings: { ...base.findings, attention: [], by_state: { open: 0, acknowledged: 10, snoozed: 6 } } });
    fake.reportWarnings = ["detail/statistics unavailable for 1 device(s)"];
    await open(fake);
    expect(await screen.findByText("No open findings: the others are acknowledged or snoozed.")).toBeInTheDocument();
    expect(screen.getByText("1 note about this read")).toBeInTheDocument();
    expect(screen.queryByText("Some data could not be read")).toBeNull();
  });

  it("renders the controller's text as text", async () => {
    const fake = new FakeApi();
    const base = dashboardFixture();
    const evil = "<img src=x onerror=alert(1)>‮gpj";
    fake.dashboard = dashboardWith({
      findings: { ...base.findings, attention: [{ id: "e", rank: 1, severity: "<b>", code: "x", subject: evil, message: evil }] },
      events: { ...base.events, notable: [{ timestamp: null, severity: "high", category: "X", event: "E", message: evil }] },
    });
    await open(fake);
    expect((await screen.findAllByText("<img src=x onerror=alert(1)>gpj")).length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("<b>")).toBeInTheDocument();
    expect(document.querySelector('img[src="x"], b')).toBeNull();
  });

  it("reads the controller again only when Refresh is pressed", async () => {
    const { user, fake } = await open();
    await screen.findByText("Gateway");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => {
      expect(fake.dashboardRefresh).toEqual([false, true]);
    });
  });
});

describe("the site", () => {
  it("offers a picker when the controller has several sites, and keeps the choice in the address", async () => {
    const fake = new FakeApi();
    fake.sites = [
      { name: "Default", ref: "default", id: "site-1" },
      { name: "Cabin", ref: "cabin", id: "site-2" },
    ];
    const { user } = await open(fake);
    await screen.findByText("Gateway");
    fake.dashboard = dashboardWith({ status: "ok", site: { id: "site-2", name: "Cabin" } });
    await user.selectOptions(screen.getByLabelText("Site"), "cabin");
    expect(await screen.findByRole("heading", { name: "All checks passed" })).toBeInTheDocument();
    expect(fake.calls.some((call) => call.path === "/unifi/sites/cabin/dashboard")).toBe(true);
    expect(screen.getByText("Cabin", { selector: ".eyebrow *" })).toBeInTheDocument();
  });

  it("opens the site of a link, and says when the controller has no such site", async () => {
    await open(new FakeApi(), "/?site=nowhere");
    expect(await screen.findByText("Could not load dashboard data")).toBeInTheDocument();
    expect(screen.getByText("The controller has no such site.")).toBeInTheDocument();
  });

  it("asks for the default site when the site list cannot be read, so the dashboard can say why", async () => {
    const fake = new FakeApi();
    fake.failWith("/unifi/sites", 502, "controller_unavailable", "The controller could not be reached.", 1);
    fake.dashboard = unreachableDashboard();
    await open(fake);
    expect(await screen.findByText("The controller could not be reached")).toBeInTheDocument();
  });

  it("says when the controller lists no site", async () => {
    const fake = new FakeApi();
    fake.sites = [];
    await open(fake);
    expect(await screen.findByText("No site to show")).toBeInTheDocument();
  });

  it("chooses the asked site, else the first, else default only when the list failed", () => {
    const list = { generated_at: "", warnings: [], sites: [{ name: "A", ref: "a", id: "1" }] };
    expect(chooseSite("b", list, false)).toBe("b");
    expect(chooseSite("", list, false)).toBe("a");
    expect(chooseSite(null, undefined, false)).toBeNull();
    expect(chooseSite(null, undefined, true)).toBe("default");
    expect(chooseSite(null, { ...list, sites: [] }, false)).toBeNull();
  });
});

describe("the report parsers", () => {
  const good = { ...dashboardFixture(), generated_at: "2026-10-07T00:00:00Z", warnings: [] };

  it.each([
    ["another version", { ...good, version: 2 }],
    ["an unknown status", { ...good, status: "fine" }],
    ["an unknown controller state", { ...good, controller: { state: "asleep" } }],
    ["a section without available", { ...good, wifi: {} }],
    ["a site without an id", { ...good, site: { name: "x" } }],
    ["warnings that are not text", { ...good, warnings: [1] }],
    ["no complete flag", { ...good, complete: "yes" }],
  ])("refuse a dashboard with %s", (_name, value) => {
    expect(() => parseDashboard(value)).toThrowError(/was not what this app expects/);
  });

  it("keep only sites that can be asked for", () => {
    const answer = parseSites({ sites: [{ name: "A", ref: "a", id: "1" }, { name: "No ref" }, { ref: "b" }], generated_at: "x", warnings: [] });
    expect(answer.sites).toEqual([
      { name: "A", ref: "a", id: "1" },
      { name: "", ref: "b", id: "" },
    ]);
    expect(() => parseSites({ sites: {}, generated_at: "x", warnings: [] })).toThrowError(/was not what this app expects/);
  });

  it("word how long ago something was", () => {
    const now = 1_000_000_000_000;
    expect(formatAgo(now - 30_000, now)).toBe("just now");
    expect(formatAgo(now + 60_000, now)).toBe("just now");
    expect(formatAgo(now - 60_000, now)).toBe("1 minute ago");
    expect(formatAgo(now - 3 * 3_600_000, now)).toBe("3 hours ago");
    expect(formatAgo(now - 2 * 86_400_000, now)).toBe("2 days ago");
    expect(formatAgo(null, now)).toBe("");
  });
});
