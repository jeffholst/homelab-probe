# UniFi Sentinel

A command-line tool for querying, troubleshooting and inventorying a UniFi Network controller. It is **read-only**: it never changes anything on the controller. Every request is a GET, with one exception: the event log can only be queried with a POST, so `events` sends a read-only query to that one endpoint (see [Event history](#event-history)).

> **Status: early development.** Tested against one live controller (Network 10.6.106); other versions and hardware may differ. See [open issues](https://github.com/jeffholst/unifi-sentinel/issues) for the roadmap.

## Credits

UniFi Sentinel is a fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export) by Eric Fitzgerald, whose CSV export is the foundation of the `export` command. It is licensed under the Apache License 2.0, as is the original.

## Commands

| Command  | Description                                                    |
| -------- | -------------------------------------------------------------- |
| `export` | Export connected clients, UniFi devices and switch ports to CSV |
| `query`  | List and filter devices, clients, DHCP reservations and switch ports (table or `--json`) |
| `events` | Event history from the controller log: disconnects, roams, IP conflicts, device outages |
| `client` | Troubleshoot one client by name, MAC or IP: where it attaches, link quality and related findings |
| `new-clients` | List clients that are in no client group, to spot new devices |
| `diagnose` | Read-only health checks with 🛑 critical, ⚠️ warning and ℹ️ info findings |
| `info`   | Show the controller application info and available sites       |

Planned: richer inventory and troubleshooting reports.

## Features

- **Client and device inventory**: connected clients (wired and wireless) and all UniFi devices (switches, access points, gateways) in one CSV
- **Switch port mapping**: per-switch CSVs with port status, speed, duplex, PoE, connected client or device, and traffic counters
- **Network topology**: which switch and port each client or device is attached to
- **DHCP reservations**: list every fixed IP reservation, including offline clients, with network and VLAN
- **Querying**: list and filter devices, clients, DHCP reservations and switch ports from the command line (table or JSON)
- **Event history**: what happened and when (disconnects, roams, IP conflicts, device outages, admin changes) from the controller's log, filterable by time, severity, category, client and device, with a summary of the noisiest clients
- **Single-client troubleshooting**: `client <name|mac|ip>` shows where a client attaches (the full uplink chain to the gateway with port numbers and link speeds), its link quality, addressing and the `diagnose` findings that concern it
- **New client detection**: list every known client that is in no client group, newest first, to spot new devices
- **Health checks**: read-only diagnostics with severity levels, exit codes for scripts and cron, and a TOML file for thresholds and an ignore list
- **Official API first**: uses the UniFi Network Integration API (`/proxy/network/integration/v1`). Legacy endpoints are used only for data the Integration API does not expose (per-port counters, client-to-port mapping, DHCP reservations, network config and client groups) and degrade gracefully with a warning if unavailable
- **Environment-based configuration**: credentials live in a `.env` file

## Requirements

- Python 3.9 or higher
- A UniFi Network Application recent enough to support the Integration API and API keys (9.5.21+ recommended)
- An API key from your controller (read-only access is sufficient, and recommended)

## Installation

```bash
git clone https://github.com/jeffholst/unifi-sentinel
cd unifi-sentinel
cp example.env .env
```

### Configure

Edit `.env`:

```env
CONTROLLER_URL=https://your-controller-ip:443
API_KEY=your-api-key-here
SITE_ID=default
VERIFY_SSL=false
```

| Variable         | Required | Default   | Description                                                        |
| ---------------- | -------- | --------- | ------------------------------------------------------------------ |
| `CONTROLLER_URL` | Yes      | -         | Controller URL (include protocol and port)                         |
| `API_KEY`        | Yes      | -         | API key from the controller                                        |
| `SITE_ID`        | No       | `default` | Site name, internal reference (e.g. `default`) or UUID             |
| `VERIFY_SSL`     | No       | `true`    | Set to `false`, `0` or `no` for self-signed certificates           |

Any other `VERIFY_SSL` value (or none) enables verification.

### Getting an API key

1. Log in to your UniFi Network Application
2. Go to **Settings > Control Plane > Integrations**
3. Click **Create API Key** and give it a descriptive name
4. Copy the key into `.env`

### Install dependencies

**With [uv](https://docs.astral.sh/uv/) (recommended):** nothing to install; dependencies are resolved on first run.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**With pip:**

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install .
```

This installs a `unifi-sentinel` command.

## Usage

```bash
uv run unifi-sentinel.py info
uv run unifi-sentinel.py export
uv run unifi-sentinel.py export -o ./out   # write CSVs to a directory
uv run unifi-sentinel.py export --include-offline   # also list previously seen clients
uv run unifi-sentinel.py query devices               # UniFi devices with firmware and uptime
uv run unifi-sentinel.py query clients -s printer --json   # filter, JSON output
uv run unifi-sentinel.py query clients --include-offline   # also previously seen clients
uv run unifi-sentinel.py query reservations          # DHCP fixed IP reservations
uv run unifi-sentinel.py query ports                 # every switch port
uv run unifi-sentinel.py query ports --down --switch rack   # down ports on matching switches
uv run unifi-sentinel.py query ports --errors        # ports with rx/tx errors
uv run unifi-sentinel.py events                      # the last 24 hours, newest first
uv run unifi-sentinel.py events --since 7d --severity high   # recent serious events
uv run unifi-sentinel.py events --client phone --event disconnected   # one client's drops
uv run unifi-sentinel.py events --summary --since 7d   # counts and the noisiest clients
uv run unifi-sentinel.py client desktop              # one client: attachment, link, findings
uv run unifi-sentinel.py client aa:bb:cc:dd:ee:ff --json   # by MAC (any format) or IP, as JSON
uv run unifi-sentinel.py new-clients                 # clients in no client group
uv run unifi-sentinel.py diagnose                    # health checks
```

`query` takes an optional kind (`all` by default, `devices`, `clients`, `reservations` or `ports`). Run these from the project root (uv uses `pyproject.toml`). After `pip install .` use `unifi-sentinel <command>` instead. Run `--help` on the tool or any command for options.

### Diagnose

`diagnose` prints findings sorted by severity:

| Level | Examples |
| ----- | -------- |
| 🛑 critical | an AP radio's channel utilization at or above `radio_util_critical_pct` (default 90%); a switch's PoE budget at or above `poe_critical_pct` (default 95%); the controller reports a WAN, internet or VPN subsystem in `error`; a LAN/WLAN `error` with no disconnected device to explain it; gateway offline; an offline switch or device that other devices uplink through; CPU or memory at or above `resource_critical_pct` (default 98%) |
| ⚠️ warning | a Wi-Fi client with signal at or below `wifi_weak_signal_dbm` (default -75 dBm), `wifi_retry_pct` (default 30%) or more of its transmissions retried, or satisfaction below `wifi_satisfaction_warn` (default 50%); an AP radio with channel utilization at or above `radio_util_warn_pct` (default 70%), or retries or satisfaction past the same limits; a port whose link has gone down `link_flap_count` or more times since boot (default 5); a port dropping `port_drop_pct` or more of its packets (default 0.1%); an up port whose STP state is not forwarding; a switch's PoE budget at or above `poe_warn_pct` (default 80%); an uplink negotiated below what both ends support; a subsystem in `warning` the same way; internet latency at or above `wan_latency_warn_ms` (default 100 ms) or drops at or above `wan_drops_warn` (default 10); other offline devices; port rx/tx errors; half-duplex links; CPU or memory at or above `resource_warn_pct` (default 90%) but below the critical level; connected clients with no IP address or a link-local (169.254.x.x) address, shown with where they attach; DHCP reservation problems: an online client whose current IP differs from its reservation, the same IP reserved for several clients, or a reserved IP outside its network's subnet; the same IP in use by several clients or UniFi devices on any VLAN, or a reserved IP currently used by a different client or UniFi device |
| ℹ️ info | the controller's LAN/WLAN status when it is only caused by disconnected devices (they are reported individually); devices waiting to be adopted; a failed speedtest; ports negotiated at or below `slow_link_mbps` (default 100 Mbps); legacy data unavailable (port checks skipped) |

The reservation checks read the legacy `stat/alluser` and `rest/networkconf` endpoints (the same data as `query reservations`); offline clients are only checked for duplicate and out-of-subnet reservations, and a reservation whose network cannot be resolved is skipped for the subnet check.

Emoji labels are used on a UTF-8 terminal. When output is piped or redirected, or with `--no-emoji`, it prints text labels (`[CRITICAL]`, `[WARNING ]`, `[INFO    ]`) instead.

#### Controller health

`diagnose` also reads the controller's own subsystem health (`stat/health`: `wlan`, `lan`, `wan`, `www`, `vpn`) so it agrees with the UniFi dashboard. The controller sets `lan`/`wlan` to `error` or `warning` whenever any device is disconnected, which the per-device findings already report, so that case is a single info line and does not raise severity or the exit code. A `lan`/`wlan` status with no disconnected device to explain it, and any `wan`, `www` or `vpn` status, use the controller's severity (`error` is critical, `warning` is warning). The `www` subsystem also gives internet latency and drops; the drops default is a heuristic because the controller does not document whether the counter is cumulative, so tune `wan_drops_warn`. If `stat/health` cannot be read, the tool warns and skips these checks.

#### Port health

For each switch port `diagnose` also checks the controller's port counters (the legacy `stat/device` port tables; skipped with a warning if unavailable):
- **Flapping links:** `link_down_count` is cumulative since the switch booted, so the finding says how long the switch has been up. Several ports sharing one count usually mean a single switch-wide event, such as a reboot or power loss, not a bad cable on each.
- **Dropped packets:** judged as a percentage of the port's packets in that direction (and only with at least `min_packets_for_drop_pct` packets, default 1,000), because a raw count means little on a busy port. Only ports that are up are checked.
- **STP:** an up port whose state is not `forwarding` (for example `blocking`).
- **PoE budget:** used power as a percentage of the switch's total PoE budget; switches without PoE are skipped. The per-port `poe_good` flag is deliberately not used: it is false on every PoE-capable port that simply has no PoE device attached.
- **Uplink speed:** an uplink negotiated below what both ends support (the device's own maximum and the parent's port maximum). A gigabit device on a 2.5G port is at its own maximum and is not flagged.

#### Wi-Fi quality

`diagnose` checks every connected Wi-Fi client and every AP radio (from the legacy `stat/sta` and `stat/device` data; skipped with a warning if unavailable). Each client finding names the band and the AP it is on.
- **Weak signal:** the client's signal at or below `wifi_weak_signal_dbm`.
- **Retries:** the share of the client's transmissions that were retried, only once it has made at least `wifi_min_attempts` transmissions, because a percentage over a few packets is noise. The 30% default is deliberately high: on a busy 2.4 GHz band many clients retry 20 to 30% of the time because of neighboring networks, which is an environmental condition more than a per-client fault. Lower `wifi_retry_pct` to see more.
- **Satisfaction:** the controller's own 0-100 score, flagged below `wifi_satisfaction_warn`.
- **AP radios:** channel utilization (warning at `radio_util_warn_pct`, critical at `radio_util_critical_pct`), and the same retry and satisfaction limits per radio.

Clients without signal or satisfaction data (the controller omits it for some) and radios that report satisfaction as unknown (`-1`) are never flagged. The controller's `anomalies` field is not used, because it is present on nearly every client.

#### Configuration: thresholds and ignore list

Thresholds and an ignore list live in an optional TOML file, read from `./unifi-sentinel.toml` or given with `diagnose --config FILE` (copy [unifi-sentinel.example.toml](unifi-sentinel.example.toml); the real file is git-ignored because it may name your devices).

```toml
[thresholds]                 # all optional; these are the defaults
resource_warn_pct = 90       # CPU or memory: warning
resource_critical_pct = 98   # CPU or memory: critical
slow_link_mbps = 100         # ports negotiated at or below this: info
wan_latency_warn_ms = 100    # internet latency at or above this: warning
wan_drops_warn = 10          # internet drops at or above this: warning (heuristic)
link_flap_count = 5          # port link-down count since boot at or above this: warning
port_drop_pct = 0.1          # dropped packets, % of a port's packets, at or above this: warning
min_packets_for_drop_pct = 1000 # minimum packets before evaluating drop percentage
poe_warn_pct = 80            # switch PoE budget used at or above this: warning
poe_critical_pct = 95        # switch PoE budget used at or above this: critical
wifi_weak_signal_dbm = -75   # Wi-Fi client signal at or below this (dBm): warning
wifi_retry_pct = 30          # client or radio TX retries at or above this (%): warning
wifi_min_attempts = 1000     # client TX attempts needed before its retries are judged
wifi_satisfaction_warn = 50  # client or radio satisfaction below this (%): warning
radio_util_warn_pct = 70     # AP radio channel utilization at or above this: warning
radio_util_critical_pct = 90 # AP radio channel utilization at or above this: critical

[[ignore]]
subject = "Garage AP"        # case-insensitive name; * and ? wildcards
message = "offline"          # case-insensitive substring; both must match if both given
reason = "spare AP, kept unplugged on purpose"   # required
```

Ignored findings are left out of the output, counted in the summary (`3 warnings (2 ignored)`), and excluded from exit codes, so a known-okay finding cannot fail a cron job. `diagnose --show-ignored` lists them with each rule's reason, so ignores do not hide problems forever. A rule needs a `reason` and a `subject` and/or `message`. A missing, unreadable or invalid file (unknown keys, bad values, rules without a reason) stops `diagnose` with exit code 3 before it contacts the controller. Other commands do not read this file. On Python 3.9 and 3.10 the `tomli` package (installed automatically) reads it.

#### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success; for `diagnose`, no findings at or above the `--fail-on` threshold |
| 1 | `diagnose` found at least one non-critical finding at or above the `--fail-on` threshold |
| 2 | `diagnose` found at least one critical finding |
| 3 | error: bad configuration, or the controller could not be reached or returned an error |
| 4 | `client` found no client, or several (it lists them) |
| 64 | command-line usage error |

`--fail-on {info,warning,critical}` sets the lowest severity that gives a non-zero code (default `warning`). Critical always exits 2. Example cron entry that only alerts on outages:

```bash
*/15 * * * * cd /path/to/unifi-sentinel && uv run unifi-sentinel.py diagnose --fail-on critical || notify-me
```

Tool errors used to exit 1 for every command; they now exit 3 so that 1 and 2 only ever mean findings.

### Devices

`query devices` shows each UniFi device with its firmware version, whether a firmware update is available, and its uptime (for example `2d 7h`). Offline devices have no uptime. `--json` adds `Uptime (s)` with the raw seconds. These columns come from the Integration API and appear only for `query devices`; the `export` CSV columns are unchanged.

### Event history

`events` reads the controller's event log, so it can answer "why did the Wi-Fi drop at 3 pm?", which none of the other commands can because they show the network as it is now. The controller keeps about three months.

```text
uv run unifi-sentinel.py events --client phone --since 6h
Time                 Severity  Category        Event                         Message
-------------------  --------  --------------  ----------------------------  -------------------------------------------
2026-01-01 10:10:00  Low       CLIENT_DEVICES  CLIENT_DISCONNECTED_WIRELESS  phone disconnected from Home. Time Connected: 25s.
...
```

Options (the filters combine with AND; the first group is done by the controller, the second by this tool):
- `--since DURATION`: how far back, such as `90m`, `24h`, `7d` or `2w` (default `24h`)
- `--category NAME` (repeatable): for example `CLIENT_DEVICES`, `UNIFI_DEVICES`, `INTERNET_AND_WAN` or `AUDIT`
- `--severity low|medium|high` (repeatable) and `-s TEXT` (text search)
- `--event TEXT`: event types containing TEXT, such as `roam`, `disconnected` or `ip_conflict`
- `--client NAME|MAC|IP` and `--device NAME|IP`: events about that client or UniFi device (MAC fragments of six or more hex digits work)
- `--limit N`: the newest N events (default 100; `0` for all). At most 20,000 events are read from the controller per run.
- `--summary`: instead of a list, counts by severity and event type over the whole window (it ignores `--limit`) and the noisiest client or device per event type, which is where a flapping device or client shows up
- `--json`: the events as JSON

Some audit events have no value for part of their message; those parts show as `<setting name>` and similar.

#### The one POST, and why it is safe

The event log has no GET endpoint. The controller only answers a POST that carries the time range and filters, and the request only *reads*: it returns events and changes nothing (reading does not mark events as read, and two identical queries return identical data). To keep the read-only promise checkable:
- the POST is sent only by `UniFiClient.system_log`, to the one fixed `system-log/all` path, and the request body may only contain the documented query keys (anything else is rejected before anything is sent);
- `UniFiClient` has no general-purpose POST, PUT, PATCH or DELETE method;
- the test suite fails if any other code sends a POST, PUT, PATCH or DELETE, or if a second POST appears in `client.py`.

### Client view

`client <name|mac|ip>` answers "why is this device slow or offline?" in one place:

```text
desktop
  MAC:        BB:00:00:00:00:01
  Status:     Online, connected since 2026-01-01 09:00:00
  Connection: Wired
  IP:         10.0.0.10  (reserved 10.0.0.10, matches)
  Network:    Main (VLAN 1)
  Groups:     Desktops
  First seen: 2020-09-13 12:26:40
  Last seen:  connected now

Attached: desktop -> Office Switch port 3 (1000 Mbps) -> Gateway port 2 (100 Mbps)
Link:     1000 Mbps, full duplex, 0 errors, 60 dropped packets on its port

Related findings:
[WARNING ] Office Switch: CPU utilization 95%
[WARNING ] Office Switch: PoE budget 41.6 W of 52 W used (80%)
[WARNING ] Office Switch: uplink to Gateway negotiated at 100 Mbps but both ends support 1000 Mbps

3 warnings
```

- **Finding the client:** an exact MAC (any separator or case), an exact IP, a single exact name, then a case-insensitive part of a name or hostname (or a MAC fragment of six or more hex digits). It looks across every client the controller knows, connected or not, but never UniFi devices. If several clients match it lists up to 20 of them and exits with code 4 instead of guessing; no match also exits 4.
- **Attached:** the switch port (or AP, with band, channel and SSID) and each parent up to the gateway, with the parent's port and the negotiated link speed. Offline devices on the path are marked `OFFLINE`. An offline client shows the last uplink the controller recorded.
- **Link:** for a wired client, its port's speed, duplex, errors and dropped packets; for Wi-Fi, signal, noise, rates, retries and satisfaction. Offline clients have none.
- **Addressing:** the DHCP reservation and whether it matches the current IP, the network and VLAN, and the client groups by name (or that it is in none).
- **Related findings:** the `diagnose` findings about this client, its IP, or the devices and ports on its path (not unrelated ports on the same switch). It uses the same thresholds and ignore list as `diagnose` (`--config FILE`, or `./unifi-sentinel.toml`).
- `--json` prints the same data as JSON, and `--no-emoji` forces text severity labels.

### New clients

`new-clients` lists every known client, connected or not, that has not been added to at least one client group (Network > Client Groups), so newly seen devices stand out. Add a client to a group in the controller and it drops off the report. Columns: Name, MAC Address, IP Address, Vendor, Connection Type, Where (switch and port, or AP), First Seen, Last Seen, Status. Newest first-seen comes first, with no age cutoff. `-s TEXT` filters and `--json` prints JSON.

Group membership comes from the legacy `stat/alluser` client records and the legacy v2 `network-members-groups` definitions; the Integration API has no client groups. A group that has been deleted does not count as membership. If the group definitions cannot be read, the tool warns and trusts each client's own group list.

### Switch ports

`query ports` lists every port on every switch: status, speed, duplex, PoE power, the connected client or device, and rx/tx errors (`--json` includes every column, such as traffic counters). Filters, which combine with AND:

- `--switch NAME`: switches whose name contains NAME (case-insensitive)
- `--down`: only ports that are down
- `--errors`: only ports with rx/tx errors
- `-s TEXT`: text match on any field, for example a connected client's name

`--switch`, `--down` and `--errors` are only valid with `query ports`. Port data comes from the legacy `stat/device` and `stat/sta` endpoints, so it is empty (with a warning) if those are unavailable.

### DHCP reservations

`query reservations` lists every enabled fixed IP reservation, including clients that are currently offline. Columns: Name, MAC Address, Reserved IP, Network, VLAN, Current IP, Status, Last Seen. It reads the legacy `stat/alluser` and `rest/networkconf` endpoints, since the Integration API does not expose reservations. Only clients with the reservation enabled are listed; disabled reservations keep a stale IP on the controller and are ignored.

### Output files

1. **`unifi_clients.csv`**: master inventory of connected clients and UniFi devices. Columns: Type, Name, MAC Address, IP Address, Model, Connection Type, Switch, Port, Last Seen, Status. By default only currently connected clients are listed; pass `--include-offline` to add previously seen clients with Status `Offline` (from the legacy `stat/alluser` endpoint).
2. **`switch_<name>.csv`**: one file per switch with port status, speed, duplex, PoE, connected client or device, and traffic counters.

CSV files are ignored by git.

## Example Output

### unifi_clients.csv

```csv
Type,Name,MAC Address,IP Address,Model,Connection Type,Switch,Port,Last Seen,Status
Client,iPhone,C2:88:E5:F2:CC:D4,192.168.1.225,,Wireless,,,2025-11-17 10:40:50,Online
Client,homeassistant,2C:CF:67:10:44:CC,192.168.1.254,,Wired,Switch - Den,6,2025-11-17 10:41:23,Online
Device - Switch,Switch - Den,6C:63:F8:AC:65:96,192.168.1.137,USPM16P,Wired,Switch - 24 Port,22,2025-11-17 10:40:32,Online
Device - Access Point,AP - Media Room,94:2A:6F:2C:85:52,192.168.1.228,U7PROMAX,Wired,Switch - Media Room,1,2025-11-17 10:41:22,Online
```

### diagnose

Sample from synthetic data (text labels are used when output is piped; a UTF-8 terminal shows emojis):

```text
[WARNING ] Garage AP: device is offline
[WARNING ] Office Switch: CPU utilization 95%
[WARNING ] Office Switch port 2: 4 rx/tx errors
[WARNING ] Office Switch port 2: link is half duplex
[WARNING ] old-printer: reserved IP 10.0.0.50 is outside network IoT (10.0.20.1/24)
[INFO    ] Office Switch port 2: negotiated at 100 Mbps

5 warnings, 1 info
```

### new-clients

```text
Name         MAC Address        IP Address  Vendor  Connection Type  Where                        First Seen           Last Seen            Status
-----------  -----------------  ----------  ------  ---------------  ---------------------------  -------------------  -------------------  -------
old-tablet   BB:00:00:00:00:04  10.0.0.51           Wireless                                      2025-06-15 10:06:40  2025-12-05 23:46:40  Offline
old-printer  BB:00:00:00:00:03  10.0.0.50           Wired            Wired, Office Switch port 6  2023-11-14 16:13:20  2025-12-17 13:33:20  Offline

2 client(s) in no group
```

### switch_Switch - Den.csv

```csv
Port,Port Index,Status,Speed,Full Duplex,PoE Enabled,PoE Power (W),PoE Class,Connected Type,Connected Name,Connected MAC,Connected Model,RX Bytes,TX Bytes,...
Port 1,1,Up,100 Mbps,Yes,No,0.00,Unknown,Client,Receiver,00:06:78:70:AD:80,,76680256,216506717,...
Port 4,4,Up,1000 Mbps,Yes,No,0.00,Unknown,Device - Switch,Switch - Front,70:A7:41:C8:BC:DE,USL8LP,30690781659,1322518689,...
Port 6,6,Up,1000 Mbps,Yes,Yes,4.95,Class 4,Client,homeassistant,2C:CF:67:10:44:CC,,195791846,11065229054,...
```

## API documentation

- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) on developer.ui.com, versioned by Network Application release (use the newest version listed). The copy matching your controller's version is also under **UniFi Network > Integrations** in the controller.
- [Getting Started with the Official UniFi API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API) in the Ubiquiti Help Center, including how to create API keys.

The official documentation covers the Integration API only. The legacy `stat/*`, `rest/*` and `v2/api/*` endpoints this tool also uses (port counters, client-to-port mapping, DHCP reservations, network config, client groups) are undocumented; their fields were determined from live controller responses.

## Troubleshooting

- **`CONTROLLER_URL is not set` / `API_KEY is not set`**: copy `example.env` to `.env` and fill it in.
- **`401 Unauthorized`**: the API key is invalid or was revoked; create a new one.
- **`TLS certificate verification failed`**: for a self-signed certificate set `VERIFY_SSL=false`, or install a valid certificate on the controller.
- **Connection errors or timeouts**: check `CONTROLLER_URL` and that the controller is reachable from this machine.
- **`Site '...' not found`**: run `info` to list site names, references and IDs.
- **`... unavailable` warnings**: the tool degrades instead of failing. The rest of the command still runs, with less data:
  - `legacy stat/... unavailable`: switch port mapping, port counters and offline clients are incomplete
  - `legacy rest/... unavailable`: network names and VLANs are missing (reservations, subnet checks)
  - `legacy v2 ... unavailable`: group names are missing; `new-clients` trusts each client's own group list
  - `legacy stat/health unavailable`: `diagnose` skips the controller health and WAN checks
  - `detail/statistics unavailable for N device(s)`: no uptime, heartbeat or CPU/memory for those devices (normal for offline devices)

## Development

```text
unifi-sentinel.py        thin launcher
unifi_sentinel/
  config.py              .env / environment loading
  client.py              UniFiClient: the only code that makes HTTP calls
  snapshot.py            collect_snapshot: one read of the controller, output-agnostic
  export.py              inventory rows and CSV export
  query.py               filtering and table/JSON rendering
  reservations.py        DHCP fixed IP reservations
  new_clients.py         clients in no client group
  client_view.py         single-client troubleshooting view
  events.py              event history from the controller's system log
  diagnose.py            read-only health checks
  settings.py            diagnose thresholds and ignore list (TOML)
  cli.py                 argparse subcommands
tests/
  conftest.py            FakeSession: a fake controller served from the fixture
  fixtures/controller.json   synthetic, sanitized controller data
```

New features are new subcommands in `cli.py` backed by modules that take a `Snapshot` (fetching stays in `snapshot.py` and `client.py`). Dependencies are declared once, in `pyproject.toml` (lockfile: `uv.lock`; regenerate with `uv lock`). Run the tests with `uv run pytest`; they use a synthetic fixture in `tests/fixtures/` and never contact a controller. See [CLAUDE.md](CLAUDE.md) for contributor and AI-assistant guidelines.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests are welcome. Work is tracked in [GitHub issues](https://github.com/jeffholst/unifi-sentinel/issues).

## Acknowledgments

- [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), the project this was forked from
- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) from Ubiquiti
- [uv](https://docs.astral.sh/uv/) for Python package management
