import { useQuery } from "@tanstack/react-query";
import { useId, useRef, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import type { DashboardAnswer, Site, SitesAnswer } from "../api/reports";
import { useServices } from "../app/services";
import { Banner, Empty, Loading } from "../components/DataStates";
import { DataView } from "../components/DataView";
import { Text } from "../components/Text";
import { Icon, type IconName } from "../components/ui/Icon";
import { formatAgo, formatDateTime } from "../lib/format";
import { safeText } from "../lib/safeText";
import { usePageTitle } from "../lib/usePageTitle";

type Tone = "danger" | "warning" | "info" | "success";

/**
 * The first page after the login: how the network is, and how much of that is known (`GET .../dashboard`).
 *
 * The rules of the document are the rules of the page: "healthy" only for `status: ok` (a complete, fresh read of a
 * controller that answered); a section that was not read says so and shows no number (never a zero); a number the
 * server did not know (`null`) is written "Unknown"; acknowledged and snoozed findings still count. The site comes
 * from `?site=`, else the first site the controller lists, else `default` when the list cannot be read (the
 * dashboard then says why the controller could not be read, which a failed site list cannot).
 */
export function DashboardPage() {
  usePageTitle("Dashboard");
  const { reports } = useServices();
  const [params, setParams] = useSearchParams();
  const sites = useQuery({ queryKey: ["sites"], queryFn: ({ signal }) => reports.sites(signal), staleTime: 5 * 60_000 });
  const site = chooseSite(params.get("site"), sites.data, sites.isError);
  const force = useRef(false);
  const dashboard = useQuery({
    queryKey: ["dashboard", site],
    queryFn: ({ signal }) => {
      const refresh = force.current;
      force.current = false;
      return reports.dashboard(site ?? "default", refresh, signal);
    },
    enabled: site !== null,
  });
  const pickerId = useId();

  const known = sites.data?.sites ?? [];
  const current = known.find((entry) => entry.ref === site);
  return (
    <>
      <div className="page-head dash-head">
        <div>
          <p className="eyebrow">{current !== undefined ? <Text value={current.name} fallback={current.ref} /> : "Overview"}</p>
          <h1>Dashboard</h1>
        </div>
        <div className="row">
          {known.length > 1 && (
            <div className="dash-site">
              <label htmlFor={pickerId} className="visually-hidden">
                Site
              </label>
              <select
                id={pickerId}
                className="input"
                value={site ?? ""}
                onChange={(event) => {
                  setParams({ site: event.target.value });
                }}
              >
                {known.map((entry) => (
                  <option key={entry.ref} value={entry.ref}>
                    {siteLabel(entry)}
                  </option>
                ))}
              </select>
            </div>
          )}
          <button
            type="button"
            className="button button--secondary"
            disabled={site === null || dashboard.isFetching}
            onClick={() => {
              force.current = true;
              void dashboard.refetch();
            }}
          >
            <Icon name="refresh" />
            Refresh
          </button>
        </div>
      </div>
      {site === null ? (
        sites.isPending ? (
          <Loading label="the sites" />
        ) : (
          <Empty title="No site to show">The controller lists no site.</Empty>
        )
      ) : (
        <DataView query={dashboard} label="dashboard data">
          {(data) => <DashboardView data={data} />}
        </DataView>
      )}
    </>
  );
}

/**
 * The site to show, as its internal reference. A site may be asked for by reference, id or name (the API takes any of
 * them), so a wanted value is turned into the listed site's reference: the selector, the heading and the query key
 * then agree, and one site is never read under two keys. A value that names no listed site is passed on as it is (the
 * server answers 404 for it). With no site asked for: the first listed, else `default` when the list could not be
 * read. Nothing is chosen while the list is still being read, so the first request is already for the final key.
 */
export function chooseSite(wanted: string | null, sites: SitesAnswer | undefined, failed: boolean): string | null {
  const asked = wanted !== null && wanted !== "" ? wanted : null;
  if (sites === undefined) return failed ? (asked ?? "default") : null;
  if (asked === null) return sites.sites[0]?.ref ?? null;
  const listed = sites.sites.find((site) => site.ref === asked) ?? sites.sites.find((site) => site.id === asked) ?? sites.sites.find((site) => site.name === asked);
  return listed?.ref ?? asked;
}

/** "Name (ref)" for the picker. An `<option>` cannot hold a component, so the controller's strings are cleaned here
 * exactly as `<Text>` cleans them (control, invisible and bidirectional characters removed, one line). */
function siteLabel(site: Site): string {
  const name = safeText(site.name);
  const ref = safeText(site.ref);
  return name === "" || name === ref ? ref : `${name} (${ref})`;
}

// -- the document ---------------------------------------------------------------------------------------------

const CONTROLLER: Record<string, string> = {
  unreachable: "The controller could not be reached",
  certificate: "The controller's certificate was not accepted",
  key_rejected: "The controller refused the API key",
};

function DashboardView({ data }: { data: DashboardAnswer }) {
  const read = Date.parse(data.generated_at);
  const down = data.controller.state !== "ok";
  return (
    <div className="stack">
      {down && (
        <Banner tone="danger" role="alert" title={CONTROLLER[data.controller.state] ?? "The controller could not be read"}>
          <p className="banner__text">
            {data.stale
              ? `What you see is the last answer that was read, ${formatAgo(read)}.`
              : "Nothing is known about the network until it answers again. Check that it is running and that this server can reach it."}
          </p>
        </Banner>
      )}
      {data.stale && !down && (
        <Banner tone="warning" title="Showing older data">
          <p className="banner__text">The latest read failed, so this is what was read {formatAgo(read)}.</p>
        </Banner>
      )}
      {!data.complete && !down && (
        <Banner tone="warning" title="Some data could not be read">
          <p className="banner__text">What you see may be incomplete, so the network is not called healthy.</p>
          <Warnings warnings={data.warnings} />
        </Banner>
      )}
      <StatusHero data={data} read={read} />
      {data.complete && data.warnings.length > 0 && (
        <details className="dash-notes">
          <summary>
            {data.warnings.length} {data.warnings.length === 1 ? "note" : "notes"} about this read
          </summary>
          <Warnings warnings={data.warnings} />
        </details>
      )}
      <div className="dash-grid">
        <AttentionCard data={data} />
        <DevicesCard data={data} />
        <ClientsCard data={data} />
        <InternetCard data={data} />
        <WifiCard data={data} />
        <EventsCard data={data} />
      </div>
    </div>
  );
}

function Warnings({ warnings }: { warnings: readonly string[] }) {
  if (warnings.length === 0) return null;
  return (
    <ul className="dash-warnings">
      {warnings.map((warning, index) => (
        <li key={index}>
          <Text value={warning} />
        </li>
      ))}
    </ul>
  );
}

const STATUS: Record<string, { tone: Tone; icon: IconName; title: string }> = {
  critical: { tone: "danger", icon: "xCircle", title: "Critical problems found" },
  warning: { tone: "warning", icon: "alert", title: "Some things need attention" },
  ok: { tone: "success", icon: "checkCircle", title: "All checks passed" },
  unknown: { tone: "info", icon: "info", title: "Health not known" },
};

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** Why `unknown`: the controller, an old answer or a partial read; never a guess that it is fine. */
function unknownReason(data: DashboardAnswer): string {
  if (data.controller.state !== "ok") return "The controller could not be read.";
  if (data.stale) return "The data is older than it should be.";
  if (!data.complete) return "Some data could not be read, so a healthy result cannot be claimed.";
  return "The checks could not say.";
}

function StatusHero({ data, read }: { data: DashboardAnswer; read: number }) {
  const status = STATUS[data.status] ?? STATUS["unknown"];
  const findings = data.findings;
  const severity = findings.available ? findings.by_severity : undefined;
  const summary =
    data.status === "unknown"
      ? unknownReason(data)
      : data.status === "ok"
        ? "Every check found nothing to report, on a complete and fresh read."
        : severity === undefined
          ? ""
          : [plural(severity.critical, "critical finding"), plural(severity.warning, "warning"), plural(severity.info, "note")].join(", ") + ".";
  return (
    <section className={`card dash-hero dash-hero--${status?.tone ?? "info"}`} aria-labelledby="status-heading">
      <span className="dash-hero__icon">
        <Icon name={status?.icon ?? "info"} />
      </span>
      <div className="dash-hero__body">
        <h2 id="status-heading">{status?.title}</h2>
        <p>{summary}</p>
        <p className="muted small">
          {findings.available && (
            <>
              {findings.by_state === null || findings.by_state === undefined
                ? "Triage state could not be read. "
                : `${findings.by_state.open} open, ${findings.by_state.acknowledged} acknowledged, ${findings.by_state.snoozed} snoozed. `}
              {(findings.ignored ?? 0) > 0 && `${plural(findings.ignored ?? 0, "finding")} hidden by the ignore list. `}
            </>
          )}
          {Number.isFinite(read) && (
            <>
              Read <time dateTime={data.generated_at}>{formatAgo(read)}</time> ({formatDateTime(read)}).
            </>
          )}
        </p>
      </div>
    </section>
  );
}

// -- the sections ---------------------------------------------------------------------------------------------

function Section({ title, icon, available, wide = false, children }: { title: string; icon: IconName; available: boolean; wide?: boolean; children: ReactNode }) {
  const id = useId();
  return (
    <section className={wide ? "card dash-card dash-card--wide" : "card dash-card"} aria-labelledby={id}>
      <div className="card__title dash-card__title">
        <span className="tile-icon">
          <Icon name={icon} />
        </span>
        <h2 id={id}>{title}</h2>
      </div>
      {available ? (
        children
      ) : (
        <p className="dash-unavailable">
          <Icon name="minusCircle" />
          <span>Not read: this could not be read, so nothing is shown here (not zero).</span>
        </p>
      )}
    </section>
  );
}

/** A number the server may not know: `null` (or absent) is "Unknown", never zero. */
function value(number: number | null | undefined, unit = ""): string {
  if (number === null || number === undefined || !Number.isFinite(number)) return "Unknown";
  const shown = Number.isInteger(number) ? String(number) : number.toFixed(1);
  return `${shown}${unit}`;
}

function Stat({ label, children, big = false }: { label: string; children: ReactNode; big?: boolean }) {
  return (
    <div className={big ? "dash-stat dash-stat--big" : "dash-stat"}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

const SEVERITY: Record<string, { label: string; className: string }> = {
  critical: { label: "Critical", className: "pill pill--danger" },
  warning: { label: "Warning", className: "pill pill--warning" },
  info: { label: "Info", className: "pill pill--info" },
  high: { label: "High", className: "pill pill--danger" },
  medium: { label: "Medium", className: "pill pill--warning" },
  low: { label: "Low", className: "pill pill--info" },
};

function SeverityPill({ severity }: { severity: string }) {
  const known = SEVERITY[severity];
  return known === undefined ? (
    <span className="pill">
      <Text value={severity} fallback="Unknown" />
    </span>
  ) : (
    <span className={known.className}>{known.label}</span>
  );
}

function AttentionCard({ data }: { data: DashboardAnswer }) {
  const findings = data.findings;
  const attention = findings.attention ?? [];
  return (
    <Section title="Needs attention" icon="alert" available={findings.available} wide>
      {attention.length === 0 ? (
        <p className="muted">
          {(findings.total ?? 0) === 0 ? "No findings." : "No open findings: the others are acknowledged or snoozed."}
        </p>
      ) : (
        <ol className="dash-list" aria-label="Open findings, most urgent first">
          {attention.map((finding) => (
            <li key={finding.id} className="dash-list__item">
              <SeverityPill severity={finding.severity} />
              <span className="dash-list__text">
                <strong>
                  <Text value={finding.subject} />
                </strong>{" "}
                <Text value={finding.message} />
                <span className="dash-list__meta mono">
                  <Text value={finding.code} />
                </span>
              </span>
            </li>
          ))}
        </ol>
      )}
      {findings.available && (findings.total ?? 0) > attention.length && (
        <p className="muted small">
          {attention.length === 0 ? "" : `The first ${attention.length} of `}
          {plural(findings.total ?? 0, "finding")} in all. Run <code>hlp diagnose</code> for the full list.
        </p>
      )}
    </Section>
  );
}

function DevicesCard({ data }: { data: DashboardAnswer }) {
  const devices = data.devices;
  return (
    <Section title="Devices" icon="server" available={devices.available}>
      <dl className="dash-stats">
        <Stat label="Online" big>
          {value(devices.online)}
          <span className="dash-of"> of {value(devices.total)}</span>
        </Stat>
        <Stat label="Offline">
          <span className={(devices.offline ?? 0) > 0 ? "status-warn" : undefined}>{value(devices.offline)}</span>
        </Stat>
        <Stat label="Other states">{value(devices.other)}</Stat>
      </dl>
      {devices.total !== undefined && devices.total > 0 && devices.online !== undefined && (
        <meter className="dash-meter" min={0} max={devices.total} value={devices.online} aria-label="Devices online" />
      )}
    </Section>
  );
}

function ClientsCard({ data }: { data: DashboardAnswer }) {
  const clients = data.clients;
  return (
    <Section title="Clients" icon="user" available={clients.available}>
      <dl className="dash-stats">
        <Stat label="Connected" big>
          {value(clients.connected)}
        </Stat>
        <Stat label="Wired">{value(clients.wired)}</Stat>
        <Stat label="Wireless">{value(clients.wireless)}</Stat>
        <Stat label="Seen before, not connected">{value(clients.offline)}</Stat>
      </dl>
    </Section>
  );
}

const NAT: Record<string, string> = {
  public: "Public address",
  private: "Private address (another router in front)",
  cgnat: "Carrier-grade NAT",
  link_local: "Link-local address",
  unknown: "Unknown",
};

function StateWord({ word }: { word: string | undefined }) {
  if (word === undefined || word === "") return <>Unknown</>;
  if (word === "ok") return <span className="status-ok">OK</span>;
  return (
    <span className="status-warn">
      <Text value={word} />
    </span>
  );
}

function InternetCard({ data }: { data: DashboardAnswer }) {
  const wan = data.wan;
  const speed = wan.last_speedtest;
  return (
    <Section title="Internet" icon="pulse" available={wan.available}>
      <dl className="dash-stats">
        <Stat label="Internet" big>
          <StateWord word={wan.internet_status} />
        </Stat>
        <Stat label="Gateway link">
          <StateWord word={wan.status} />
        </Stat>
        <Stat label="Latency">{value(wan.latency_ms, " ms")}</Stat>
        <Stat label="Availability (24 h)">{value(wan.availability_pct, "%")}</Stat>
        <Stat label="Address">{wan.nat === undefined ? "Unknown" : (NAT[wan.nat] ?? <Text value={wan.nat} />)}</Stat>
        <Stat label="Last speed test">
          {speed === null || speed === undefined ? (
            "None"
          ) : (
            <>
              {value(speed.download_mbps)} down / {value(speed.upload_mbps)} up Mbps
              {speed.time !== null && <span className="dash-of"> {formatAgo(speed.time)}</span>}
            </>
          )}
        </Stat>
      </dl>
    </Section>
  );
}

function WifiCard({ data }: { data: DashboardAnswer }) {
  const wifi = data.wifi;
  return (
    <Section title="Wi-Fi" icon="sparkle" available={wifi.available}>
      <dl className="dash-stats">
        <Stat label="Access points online" big>
          {value(wifi.access_points_online)}
          <span className="dash-of"> of {value(wifi.access_points)}</span>
        </Stat>
        <Stat label="Radios">{value(wifi.radios)}</Stat>
        <Stat label="Wireless clients">{value(wifi.wireless_clients)}</Stat>
        <Stat label="Lowest satisfaction">{value(wifi.lowest_satisfaction, "%")}</Stat>
        <Stat label="Busiest channel">{value(wifi.highest_utilization, "%")}</Stat>
      </dl>
    </Section>
  );
}

function EventsCard({ data }: { data: DashboardAnswer }) {
  const events = data.events;
  const notable = events.notable ?? [];
  const hours = Math.round((events.window_seconds ?? 86_400) / 3600);
  return (
    <Section title={`Events (last ${hours} h)`} icon="history" available={events.available} wide>
      <p className="muted small">
        {plural(events.total ?? 0, "event")}
        {events.truncated === true ? " or more" : ""}, {events.notable_total ?? 0} above low severity.
      </p>
      {notable.length > 0 && (
        <ol className="dash-list" aria-label="Newest notable events">
          {notable.map((event, index) => (
            <li key={index} className="dash-list__item">
              <SeverityPill severity={event.severity} />
              <span className="dash-list__text">
                <Text value={event.message} fallback={event.event} />
                {event.timestamp !== null && <span className="dash-list__meta">{formatAgo(event.timestamp)}</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
    </Section>
  );
}
