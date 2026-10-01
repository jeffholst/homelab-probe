# UniFi Sentinel

A command-line tool for querying, troubleshooting and inventorying a UniFi Network controller. It is **read-only**: it never changes anything on the controller. Every request is a GET, with one exception: the event log can only be queried with a POST, so `events`, and `diagnose` and `client` by default (`--no-events` skips it), send a read-only query to that one endpoint (see [Event history](#event-history)).

> **Status: early development.** Tested against one live controller (Network 10.6.106); other versions and hardware may differ. See [open issues](https://github.com/jeffholst/unifi-sentinel/issues) for the roadmap.

## Credits

UniFi Sentinel is a fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export) by Eric Fitzgerald, whose CSV export is the foundation of the `export` command. It is licensed under the Apache License 2.0, as is the original.

## Commands

| Command  | Description                                                    |
| -------- | -------------------------------------------------------------- |
| `export` | Export connected clients, UniFi devices and switch ports to CSV |
| `query`  | List and filter devices, clients, DHCP reservations and switch ports (table or `--json`) |
| `snapshot` | Save the current inventory to a local JSON file, to compare later |
| `diff` | What changed: compare saved snapshots, or a snapshot against the live network |
| `topology` | Draw the uplink tree from the gateway down: ports, link speeds, client counts and problems |
| `wifi` | Wireless report: each AP's radios and a channel plan from the neighboring networks |
| `wan` | Internet health: current state, 24-hour monitoring and speedtest history |
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
- **Snapshots and diff**: save the inventory to a file and see exactly what changed since: new or missing devices and clients, IP, firmware, state, location, reservation and group changes
- **Topology**: the uplink tree from the gateway down, with the port each device plugs into, negotiated link speeds (and links below what both ends support), client counts, and offline or flagged devices
- **Wireless report**: each AP's radios (channel, width, power, clients, utilization, retries) and a channel plan from the neighboring networks your APs hear, with overlap-aware counts and plain observations
- **Internet health**: `wan` shows the connection's state, the controller's own 24-hour availability and latency monitoring per target, and the speedtest history with the runs that fell well below normal, to tell an internet problem from a LAN problem
- **Event history**: what happened and when (disconnects, roams, IP conflicts, device outages, admin changes) from the controller's log, filterable by time, severity, category, client and device, with a summary of the noisiest clients
- **Single-client troubleshooting**: `client <name|mac|ip>` shows where a client attaches (the full uplink chain to the gateway with port numbers and link speeds), its link quality, addressing and the `diagnose` findings that concern it
- **New client detection**: list every known client that is in no client group, newest first, to spot new devices
- **Health checks**: read-only diagnostics with severity levels, exit codes for scripts and cron, and a TOML file for thresholds and an ignore list
- **Official API first**: uses the UniFi Network Integration API (`/proxy/network/integration/v1`). Legacy endpoints are used only for data the Integration API does not expose (per-port counters, client-to-port mapping, DHCP reservations, network config and client groups) and degrade gracefully with a warning if unavailable
- **Safe output**: names come from devices on your network, so text output has control characters, line breaks, text-direction overrides and invisible characters removed, and exported CSV cells that a spreadsheet would run as a formula are neutralized
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
chmod 600 .env
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
| `VERIFY_SSL`     | No       | `true`    | `true`/`yes`/`1`/`on` or `false`/`no`/`0`/`off` (any case)         |
| `ALLOW_INSECURE_HTTP` | No  | `false`   | Lab-only opt-in to an `http://` controller URL (same words as `VERIFY_SSL`) |

- **Where the `.env` file is found**, first match wins: the file given with `--env-file FILE` (before the command, for example `unifi-sentinel --env-file lab.env diagnose`); the file named by the `UNIFI_SENTINEL_ENV` environment variable; `.env` in the **current directory**. Parent directories and the installed package's directory are not searched, so an installed copy (`pip install .`) works from whichever directory holds your `.env`, an unrelated project's `.env` is never picked up, and running from a subdirectory of the project does not find the project's `.env` (use `--env-file` or run from the project root). A file named with `--env-file` or `UNIFI_SENTINEL_ENV` must exist. Real environment variables always take precedence over values in the file. The `unifi-sentinel.toml` settings file for `diagnose` is likewise read from the current directory.
- **`VERIFY_SSL`:** an unset or empty value verifies certificates. Any other word than the ones above is an error that lists the accepted words, so a typo such as `off-ish` can never silently mean "verify".
- **Protecting the API key:** the key is a credential for your controller, so keep `.env` private with `chmod 600 .env`. If the file that was read is accessible to your group or to other users (any of the group or other permission bits set), the command prints one warning that names the file and the `chmod 600` fix, and carries on. It is only a warning, and it is skipped on Windows where file modes mean little. A symlink is judged by the file it points to. The key is never printed: error messages, warnings and `repr()` of the configuration leave it out, and if a server or proxy echoes it back in an error body it is replaced with `***`.
- **`CONTROLLER_URL`:** it needs a scheme and a host (`https://host` or `https://host:port`; a trailing slash is removed). An `http://` URL is refused, because the API key is sent in a header of every request and would travel in clear text; use `https://` (with `VERIFY_SSL=false` for a self-signed certificate). For a lab network you trust you can opt in with `ALLOW_INSECURE_HTTP=true`; every run then prints a warning that the key travels in clear text. A URL containing a user name, password, query (`?`), fragment (`#`), space, backslash or control character is refused.
- **`SITE_ID`:** a site name may contain spaces and non-ASCII letters, but not `/`, `\`, `?`, `#` or control characters, and at most 128 characters; it is also percent-encoded wherever it appears in a URL.

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
uv run unifi-sentinel.py snapshot                    # save the inventory to ./snapshots/
uv run unifi-sentinel.py diff                        # what changed since the newest snapshot?
uv run unifi-sentinel.py topology                    # how the gateway, switches and APs are wired
uv run unifi-sentinel.py topology --clients          # ...with the wired clients under each device
uv run unifi-sentinel.py wifi                        # radios and a channel plan from the neighbors
uv run unifi-sentinel.py wifi --band 2.4 --ap hall   # one band, one AP
uv run unifi-sentinel.py wan                         # is it my internet or my LAN?
uv run unifi-sentinel.py wan --days 90               # a longer speedtest history
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
| ⚠️ warning | 24-hour internet availability below `wan_availability_warn_pct` (default 99%), overall or for one monitoring target; the last speedtest well below the 30-day median (`wan_speed_drop_pct`, default 70%); an IP conflict reported in the last 24 hours; a client that disconnected `event_flap_count` or more times in that window, or a device that was unreachable that often; a Wi-Fi client with signal at or below `wifi_weak_signal_dbm` (default -75 dBm), `wifi_retry_pct` (default 30%) or more of its transmissions retried, or satisfaction below `wifi_satisfaction_warn` (default 50%); an AP radio with channel utilization at or above `radio_util_warn_pct` (default 70%), or retries or satisfaction past the same limits; a port whose link has gone down `link_flap_count` or more times since boot (default 5); a port dropping `port_drop_pct` or more of its packets (default 0.1%); an up port whose STP state is not forwarding; a switch's PoE budget at or above `poe_warn_pct` (default 80%); an uplink negotiated below what both ends support; a subsystem in `warning` the same way; internet latency at or above `wan_latency_warn_ms` (default 100 ms) or drops at or above `wan_drops_warn` (default 10); other offline devices; port rx/tx errors; half-duplex links; CPU or memory at or above `resource_warn_pct` (default 90%) but below the critical level; connected clients with no IP address or a link-local (169.254.x.x) address, shown with where they attach; DHCP reservation problems: an online client whose current IP differs from its reservation, the same IP reserved for several clients, or a reserved IP outside its network's subnet; the same IP in use by several clients or UniFi devices on any VLAN, or a reserved IP currently used by a different client or UniFi device |
| ℹ️ info | a client that roamed `event_flap_count` or more times; a device that was unreachable earlier but is online now; high-latency events from the ISP monitor; the controller's LAN/WLAN status when it is only caused by disconnected devices (they are reported individually); devices waiting to be adopted; a failed speedtest; ports negotiated at or below `slow_link_mbps` (default 100 Mbps); legacy data unavailable (port checks skipped) |

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

#### Recent events

`diagnose` also reads the controller's event log (the last 24 hours by default) so it notices things that **happened and went away**, which a snapshot of the network right now cannot see (an IP conflict is usually over by the time `diagnose` runs):
- **IP conflicts:** a warning per IP that names the devices involved, the network, how many times it was reported and when last. When one of the devices has a DHCP reservation for that address it says so, and when a device is reserved a *different* address it says that too, which points at a stale lease or a static IP on the device. Over a longer window it also says on how many different days the conflict happened, so a recurring one stands out. For example:

  ```text
  [WARNING ] 10.0.0.50: IP conflict reported 1 time in the last 24h between Guest Laptop and old-printer on Main (most recent 2026-09-30 20:57:30); old-printer holds the reservation for 10.0.0.50
  ```

  The devices come from the event itself (merged across events and de-duplicated by MAC address); an event that does not list any gets the shorter message with just the address.
- **Flapping:** a client that disconnected `event_flap_count` (default 10) or more times in the window, wired and wireless together, is a warning. A device that was unreachable that often is a warning too.
- **Roaming** is normal for phones (one phone here roams about 30 times a day), so a client that roamed `event_flap_count` or more times is only info.
- **Unreachable earlier, online now:** info. A device that is offline right now is left to the existing offline finding.
- **ISP high latency** events: info with the count.

`--since DURATION` changes the window (for example `12h` or `7d`), and `--no-events` skips these checks and the request they need. If the log cannot be read, `diagnose` warns and carries on without them. An event-based warning stays in the output until its event leaves the window, so it keeps `diagnose` at exit code 1 for that long; use a shorter `--since` or `--no-events` for a cron job that should only react to what is wrong right now.

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
wan_availability_warn_pct = 99   # 24h internet availability below this (%): warning
wan_speed_drop_pct = 70      # last speedtest download below this % of the 30-day median: warning
event_flap_count = 10        # disconnects (or unreachable events) in the window at or above this: warning
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

Event-based warnings (above) count towards exit code 1 like any other warning.

Tool errors used to exit 1 for every command; they now exit 3 so that 1 and 2 only ever mean findings.

### Devices

`query devices` shows each UniFi device with its firmware version, whether a firmware update is available, and its uptime (for example `2d 7h`). Offline devices have no uptime. `--json` adds `Uptime (s)` with the raw seconds. These columns come from the Integration API and appear only for `query devices`; the `export` CSV columns are unchanged.

### Snapshots and diff

When something breaks, the first question is "what changed since it last worked?". `snapshot` saves the inventory, and `diff` compares.

```bash
uv run unifi-sentinel.py snapshot                         # now: ./snapshots/snapshot-20260930-201530.json
# ...later, when something is wrong...
uv run unifi-sentinel.py diff                             # the newest snapshot against the network right now
uv run unifi-sentinel.py diff --last-two                  # the two newest snapshots (no controller needed)
uv run unifi-sentinel.py diff snapshot-20260929-080000.json   # a named snapshot against now
uv run unifi-sentinel.py diff OLD.json NEW.json           # two files
```

```text
Comparing snapshot-20260930-201530.json (captured 2026-09-30 20:15) -> the network right now

Devices
  Firmware changed (1):
    Office Switch: 7.0.0 -> 7.1.0
  State changed (1):
    Garage AP: Online -> Offline

Clients
  New clients (1):
    newcomer (10.0.0.19, Wireless)
  IP changed (1):
    printer: 10.0.0.50 -> 10.0.0.77

3 change(s)
```

- **What is saved:** devices (name, IP, model, type, firmware, state, uplink and port), every client the controller knows (name, IP, wired or Wi-Fi, online status, network and VLAN, the device and port it is on, client groups by name) and DHCP reservations, plus the site and controller version. It is built from the same rows the other commands print, not raw API data, and leaves out values that change constantly (uptime, last-seen times, traffic), so a diff shows real changes.
- **What diff reports** (matching by MAC address): new and missing devices, clients and reservations; renamed items; IP, firmware, state, model and network or VLAN changes; devices and clients that **moved** (a different switch, port or AP; an unknown location, such as an offline Wi-Fi client, is never a move); group changes; reservation changes; and a controller version change. Clients that connected or disconnected are listed too, but only the first 15 of each (and of client IP changes); `--all` lists every one. `--json` prints everything.
- **Choosing what to compare:** `diff` with no arguments compares the newest saved snapshot with the live network, `diff OLD` compares a snapshot (a path, or a file name inside the snapshot directory) with the live network, and `diff OLD NEW` or `--last-two` compare two files without contacting the controller.
- **Files:** `snapshot` writes `snapshot-YYYYMMDD-HHMMSS.json` into `./snapshots/` (change it with `--dir DIR`), never overwriting an existing file. `-o FILE` picks the name; it refuses to replace an existing file unless you add `--force`. `--keep N` afterwards deletes the oldest snapshots in the directory beyond the newest N; it only touches files named like the ones this tool writes, and never the one just saved.
- **Privacy:** snapshots contain real MACs, IPs and device names. They are created readable only by you, and `snapshots/` is git-ignored. Do not commit or share them.
- A snapshot file has a format version. A file from a newer, incompatible version, a damaged file, or one that is not a snapshot stops with a clear message (exit code 3).
- Both commands only read from the controller; the files are written locally.

### Topology

`topology` draws how the network is wired, so a broken or slow path is visible at a glance:

```text
Gateway (UCG Max)
`-- port 2 -> Office Switch (100 Mbps, supports 1000)   1 client   [WARNING x8]
    +-- port 2 -> Office AP   1 client
    `-- port 5 -> Garage AP   [OFFLINE]   [WARNING]
```

Each line is `port N -> device`, where N is the **parent's** port the device plugs into, followed by the negotiated link speed, the number of connected clients (wired by switch port, wireless by AP), and flags.
- **Link speed:** shown when known. `supports 1000` means the link negotiated below what both ends support (the same check `diagnose` makes), so it points at a bad cable, port or device. A gateway's own uplink is its internet connection and is not drawn.
- **Flags:** `[OFFLINE]` for a device the controller reports as offline, and a warning or critical marker (`⚠️ 2`, or `[WARNING x2]` in plain text) when `diagnose` has findings about the device or one of its ports or radios. Those findings are listed under the tree. Info-level findings, such as ports at 100 Mbps, are left to `diagnose` so the flags mean something. It uses the same thresholds and ignore list as `diagnose` (`--config FILE`, or `./unifi-sentinel.toml`).
- **Order:** children are sorted by the parent's port number, then by name.
- **Unattached:** a device that cannot be reached from a gateway is listed separately with the reason: no uplink information, an uplink to an unknown device, or an uplink loop. Nothing silently disappears. An offline device's position is its last known one.
- `--clients` lists the wired clients under each device with their port. `--json` prints the nested tree (and `--clients` adds `wired_clients`). `--no-emoji` forces plain ASCII drawing and text labels, which is also used automatically when output is not a UTF-8 terminal.

The uplink and port data comes from the legacy `stat/device` and `stat/sta` data and the Integration API device detail, which is used for the parent when the legacy data has none (then no port number is shown).

### Wi-Fi

`wifi` describes your wireless side: each AP's radios, and who else is on the air near them.

```text
Access points
AP         Band     Channel  Width   Power   Clients  Utilization  Retries  Satisfaction
---------  -------  -------  ------  ------  -------  -----------  -------  ------------
Office AP  2.4 GHz  6        20 MHz  22 dBm  4        30%          5%       99%
Office AP  5 GHz    36       80 MHz  26 dBm  6        10%          3%
  Garage AP (offline): no radio data

Neighboring networks: 9 seen by your APs (8 stronger than -80 dBm, 1 open, 1 with a hidden name)

2.4 GHz
Channel  Neighbors  Strong  Your radios
-------  ---------  ------  -----------
1        1          0
4        1          1
6        3          3       Office AP
11       2          2

  Channel 4: strongest of 1 stronger than -80 dBm
    Adjacent Net  (-60 dBm, WPA2-Personal (AES/CCMP))

  Channel 6: strongest of 3 stronger than -80 dBm
    Neighbor One  (-45 dBm, WPA2-Personal (AES/CCMP), heard by 2 APs)
    (hidden, Acme Corp)  (-66 dBm, WPA2-Personal (AES/CCMP))
    Café Guest ☕  (-72 dBm, Open)  [OPEN]

  Channel 11: strongest of 2 stronger than -80 dBm
    Eleven Net  (-50 dBm, WPA2-Personal (AES/CCMP))
    LineBreak xxxxxxxxxxxxxxxxxxxxxxxxxxxxx…  (-70 dBm, WPA2-Personal (AES/CCMP))

...

Observations
  - Office AP 2.4 GHz (channel 6): 3 neighbors stronger than -80 dBm on the same channel, 1 overlapping it
  - Office AP 5 GHz (channel 36): 0 neighbors stronger than -80 dBm on the same channel, 1 overlapping it
  - Of the usual 2.4 GHz channels, channel 1 overlaps the fewest neighbors (channel 1: 1, channel 6: 4, channel 11: 2), stronger than -80 dBm
```

- **Access points:** one row per radio with the channel it is actually using (even when set to auto), width, transmit power, connected clients, channel utilization, retry rate and the controller's satisfaction score (blank when the controller reports it as unknown). An AP with no radio data, such as an offline one, is listed so it does not vanish.
- **Neighbors are counted once per network.** The controller's scan returns one row for every AP that hears a network, so counting rows would count one neighbor several times; `wifi` merges them by BSSID, keeps the strongest reading and says how many of your APs heard it. Your own networks never count as neighbors (they are recognized by their BSSIDs, which the AP data lists).
- **Strong:** neighbors at or above `--min-signal` (default -80 dBm). Weaker ones are still counted in the totals but are not named or compared, which is what keeps a list of dozens readable. At most 5 strong neighbors are named per channel; `--all` names every one. Hidden networks show as `(hidden)` with the equipment vendor when known, and open networks are marked `[OPEN]`.
- **Overlap, not just the channel number.** 2.4 GHz channels are 5 MHz apart but about 22 MHz wide, so a neighbor on channel 4 disturbs both channel 1 and channel 6; a 5 GHz radio with an 80 MHz width occupies a block of channels. For 40 MHz 2.4 GHz radios, the reported center or extension channel is used; without it, the frequency span is unknown. The observations count neighbors on your radio's channel and neighbors that merely overlap it. The observations also point out your own radios that compete with each other and which of the usual 2.4 GHz channels (1, 6, 11) overlaps the fewest strong neighbors. They only describe; they never tell you what to change.
- If the neighbor scan fails, text marks neighbor counts `n/a` and skips neighbor-based observations. JSON sets `neighbors.available` to `false` and the unavailable totals and per-channel counts to `null`.
- `--band 2.4|5|6` and `--ap NAME` filter (`--ap` also limits the neighbors to those that AP hears); `--json` prints everything.
- **6 GHz:** the controller's neighbor scan reports no 6 GHz networks, so those cells say `n/a` and no claim is made about that band.
- **Neighbor names are shown, as the controller reports them.** They identify other households' networks, so check the output before pasting it into an issue or sharing it. The names above are synthetic.

### WAN

`wan` answers "is it my internet or my LAN?" from data the controller already keeps (all read with GET):

```text
Internet: ok (Example ISP)
  WAN IP: 192.0.2.10
  Gateway: Gateway
  Now: latency 20 ms, 0 drops, status ok
  Link wan1: eth4 up, 1000 Mbps full duplex (port supports 2500 Mbps)
    live: 1.0 Mbps up, 2.0 Mbps down

Last 24h (controller monitoring, WAN): availability 100.0%, average latency 23 ms
Target       Type  Availability  Latency  Alerts
-----------  ----  ------------  -------  ------
192.0.2.53   dns   100.0%        22 ms    yes
example.com  icmp  100.0%        20 ms
example.org  icmp  100.0%        24 ms

Speedtests, last 30 days (11 runs), 12 stored
  Last: 2026-09-30 16:10 (6h ago): download 880 Mbps, upload 40 Mbps, latency 25 ms
  Download: min 500 Mbps, median 925 Mbps, max 940 Mbps
  Upload: min 31 Mbps, median 40 Mbps, max 41 Mbps
  Latency: min 23 ms, median 24 ms, max 41 ms

  Download below 70% of the median (1):
    2026-09-22 22:10  download 500 Mbps, upload 31 Mbps, latency 41 ms
```

- **Now:** the WAN and internet subsystems of the controller's health (status, ISP, WAN IP, latency, drops) and the gateway's WAN link: its negotiated speed against what the port supports (a 1 Gbps plan on a 2.5 Gbps port is normal, so that is only shown, never flagged) and the live traffic rate.
- **Last 24 hours:** the controller's own monitoring of the connection: overall availability and average latency, and each monitoring target (`icmp` ping or `dns`) with its availability and latency. `Alerts: yes` marks a target the controller is configured to alert on; it does not mean the target is failing.
- **Speedtests:** every stored result (the controller runs them on a schedule) over `--days N` (default 30), using the latest run's `wan_networkgroup` or `interface_name` so dual-WAN links are not mixed: the last one and how old it is, the minimum, median and maximum of download, upload and latency, and the runs whose download fell below `wan_speed_drop_pct` (default 70%) of the median, with their dates. That is where a degradation window shows up; try `--days 90`. At least 5 runs are needed before the median means anything.
- `--json` prints the same data, and `--config FILE` (or `./unifi-sentinel.toml`) sets the threshold. A missing piece (no speedtests stored, no monitoring data, a gateway with no WAN link data) is simply left out.
- **`diagnose` uses the same data:** a warning when 24-hour availability, overall or for any single monitoring target, is below `wan_availability_warn_pct` (default 99%), and a warning when the last speedtest (within 30 days) is below `wan_speed_drop_pct` of the 30-day median, with its age.
- **Not included:** an hourly traffic and latency history. The controller only returns that from a POST to its report endpoint, which is outside the one approved POST (the event log); a plain GET returns empty rows.

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
- the POST is sent only by `UniFiClient.system_log` (used by `events`, and by `diagnose` and `client` unless `--no-events`), to the one fixed `system-log/all` path, and the request body may only contain the documented query keys (anything else is rejected before anything is sent);
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

Recent events (last 24h, newest first):
  2026-10-01 01:31:49  CLIENT_CONNECTED_WIRED: desktop connected to Main on Office Switch Port 3.

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
- **Recent events:** the client's events from the controller's event log (the last 24 hours by default; `--since DURATION` changes it, for example `12h` or `7d`), newest first, up to 10, then how to see the rest with `events --client MAC`. The client is matched by its MAC address, so a similarly named device never mixes in. A second list shows events about the devices on its path, matched by device ID (name only when an ID is unavailable), but only device-state events (a switch or AP going unreachable or reconnecting), up to 5: not other clients connecting to the same AP, and not internet-latency events, which also name the gateway but do not explain why one client dropped. That is how a client's disconnect lines up with the switch outage that caused it.
- `--no-events` skips this section and the request it needs (the one approved read-only event-log query, see [Event history](#event-history)); with it, `client` sends no POST at all. If the log cannot be read, the rest of the view is shown with "Recent events: unavailable".
- `--json` prints the same data as JSON, with `events`, `device_events`, `events_window`, `events_omitted` (how many were left out), `events_truncated` (`true` if the 20,000-event read cap was reached; omission counts may then be incomplete, or `null` when events are unavailable/not requested) and `events_available` (`true`, `false` when the log could not be read, or `null` with `--no-events`). Text output says "at least" for omission counts and notes when counts are incomplete; it avoids claiming there were no matches when the log was truncated. `--no-emoji` forces text severity labels.

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

#### Names in exports and output

A device or client chooses its own hostname, and so does anyone who joins your network, so names are treated as untrusted:

- **CSV cells**: a text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return would run as a formula when the file is opened in Excel, Sheets or LibreOffice. Such cells are written with a leading apostrophe (`'=1+1`), so the spreadsheet treats it as text. Excel and LibreOffice show the apostrophe when they open a CSV; that is the price of safety, and it is the only change. Numbers and every other cell are written as they are (a number stored as text, such as `-67`, counts as text). If you read the CSV with a script, strip a leading `'` from text columns.
- **Text output** (tables, `diagnose`, `topology`, `client`, `wifi`, `wan`, `events`, `diff`, and messages): tabs and line breaks in a name become a space (so a name cannot add a fake line such as a forged `[CRITICAL]` finding), and other control characters, including terminal escape sequences, are removed, as are the text-direction override and isolate characters that can disguise a name, and invisible characters (zero-width space, word joiner, byte order mark) that make two different names look the same. Accented letters, emoji (including their joiners) and right-to-left scripts such as Hebrew and Arabic, with their direction marks, are kept.
- **`--json` output** is not changed: it carries the names as the controller reports them, with control characters escaped by JSON itself (`\u001b`). Treat them as untrusted data if you pass them on.

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

Sample from synthetic data with `diagnose --no-events` (text labels are used when output is piped; a UTF-8 terminal shows emojis):

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

- **`CONTROLLER_URL is not set` / `API_KEY is not set`**: copy `example.env` to `.env` and fill it in, in the directory you run the command from, or point to it with `--env-file FILE` or `UNIFI_SENTINEL_ENV`.
- **`CONTROLLER_URL uses http://`**: use `https://` (the API key would be sent in clear text), or set `ALLOW_INSECURE_HTTP=true` for a trusted lab network.
- **`... is accessible to other users`**: run the `chmod 600` command in the warning; the file holds your API key. On a file system that does not keep Unix permissions (a Windows drive mounted in WSL, some network shares) the mode cannot be changed and the warning stays; keep the file on a normal Linux or macOS file system, or in your home directory.
- **`env file not found`**: the file named with `--env-file` or `UNIFI_SENTINEL_ENV` does not exist.
- **`VERIFY_SSL must be one of ...` / `SITE_ID ... cannot be part of a site name`**: fix the value in `.env`; the message lists what is accepted.
- **`401 Unauthorized`**: the API key is invalid or was revoked; create a new one.
- **`TLS certificate verification failed`**: for a self-signed certificate set `VERIFY_SSL=false`, or install a valid certificate on the controller.
- **Connection errors or timeouts**: check `CONTROLLER_URL` and that the controller is reachable from this machine.
- **`Site '...' not found`**: run `info` to list site names, references and IDs.
- **`... unavailable` warnings**: the tool degrades instead of failing. The rest of the command still runs, with less data:
  - `legacy stat/... unavailable`: switch port mapping, port counters and offline clients are incomplete
  - `legacy rest/... unavailable`: network names and VLANs are missing (reservations, subnet checks)
  - `legacy v2 ... unavailable`: group names are missing; `new-clients` trusts each client's own group list
  - `legacy stat/health unavailable`: `diagnose` skips the controller health and WAN checks
  - `neighboring networks unavailable`: `wifi` shows the radios and channel table with neighbor counts marked unavailable; neighbor-based channel comparisons are skipped
  - `speedtest history unavailable`: `wan` shows no speedtests and `diagnose` skips the speedtest check
  - `event log unavailable`: `events` shows nothing, and `diagnose` and `client` skip their event parts
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
  wan.py                 internet health: state, 24h monitoring, speedtests
  wifi.py                wireless report: radios and a channel plan from neighbors
  topology.py            uplink tree: wiring, link speeds, client counts, flags
  history.py             saved inventories (snapshot) and the diff between them
  diagnose.py            read-only health checks
  settings.py            diagnose thresholds and ignore list (TOML)
  util.py                output safety: printable text for names, CSV formula neutralizing
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
