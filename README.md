# UniFi Sentinel

A command-line tool for querying, troubleshooting and inventorying a UniFi Network controller. It is **read-only**: it never changes anything on the controller. Every request is a GET, with one exception: the event log can only be queried with a POST, so `events`, and `diagnose` and `client` by default (`--no-events` skips it), send a read-only query to that one endpoint (see [Event history](#event-history)). Nothing else is ever sent anywhere, with one opt-in exception: `diagnose --notify` can send a short message to a notification service you configure (see [Notifications](#notifications)).

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
| `diagnose` | Read-only health checks with 🛑 critical, ⚠️ warning and ℹ️ info findings (`--json` for scripts) |
| `info`   | Show the controller application info and available sites       |

The tool is still evolving; see open issues for planned reports, controller-version coverage, packaging, and release work.

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
- **Verbose logging**: `--verbose` shows every request (path, status, time, retries) and what was read on stderr, never the API key, to diagnose slow runs and undocumented endpoints
- **Randomized MAC detection**: clients that use a private (locally administered) Wi-Fi MAC address are flagged in `query clients`, `new-clients` and `client`, and `diagnose` notes reservations tied to one, because they stop applying when the device changes its address
- **New client detection**: list every known client that is in no client group, newest first, to spot new devices
- **Notifications**: `diagnose --notify` tells you through ntfy or a webhook when a problem is new, has got worse, or is fixed (critical ones are repeated daily), once instead of every run, opt-in, with a redaction option
- **Health checks**: read-only diagnostics with severity levels, exit codes for scripts and cron, `--json` output with a stable code per check, and a TOML file for thresholds and an ignore list
- **Official API first**: uses the UniFi Network Integration API (`/proxy/network/integration/v1`). Legacy endpoints are used only for data the Integration API does not expose (per-port counters, client-to-port mapping, DHCP reservations, network config and client groups) and degrade gracefully with a warning if unavailable
- **Safe output**: names come from devices on your network, so text output has control characters, line breaks, text-direction overrides and invisible characters removed, and exported CSV cells that a spreadsheet would run as a formula are neutralized
- **Environment-based configuration**: credentials live in a `.env` file

## Requirements

- Python 3.10 or higher
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
VERIFY_SSL=true
```

| Variable         | Required | Default   | Description                                                        |
| ---------------- | -------- | --------- | ------------------------------------------------------------------ |
| `CONTROLLER_URL` | Yes      | -         | Controller URL (include protocol and port)                         |
| `API_KEY`        | Yes      | -         | API key from the controller                                        |
| `SITE_ID`        | No       | `default` | Site name, internal reference (e.g. `default`) or UUID             |
| `VERIFY_SSL`     | No       | `true`    | `true`/`yes`/`1`/`on`, `false`/`no`/`0`/`off` (any case), or the path of a CA bundle |
| `TIMEOUT`        | No       | `15`      | Seconds to wait for each request, 1 to 600 (also `--timeout SECONDS` before the command) |
| `NOTIFY_NTFY_URL` | No      | -         | Full ntfy topic URL for `diagnose --notify` (a secret, `https://` only) |
| `NOTIFY_NTFY_TOKEN` | No    | -         | ntfy access token, sent as a bearer token |
| `NOTIFY_WEBHOOK_URL` | No   | -         | Generic webhook URL for `diagnose --notify` (a secret, `https://` only) |
| `NOTIFY_WEBHOOK_TOKEN` | No | -         | Webhook bearer token |
| `ALLOW_INSECURE_HTTP` | No  | `false`   | Lab-only opt-in to an `http://` controller URL (same words as `VERIFY_SSL`) |

- **Where the `.env` file is found**, first match wins: the file given with `--env-file FILE` (before the command, for example `unifi-sentinel --env-file lab.env diagnose`); the file named by the `UNIFI_SENTINEL_ENV` environment variable; `.env` in the **current directory**. Parent directories and the installed package's directory are not searched, so an installed copy (`pip install .`) works from whichever directory holds your `.env`, an unrelated project's `.env` is never picked up, and running from a subdirectory of the project does not find the project's `.env` (use `--env-file` or run from the project root). A file named with `--env-file` or `UNIFI_SENTINEL_ENV` must exist. Real environment variables always take precedence over values in the file. The `unifi-sentinel.toml` settings file for `diagnose` is likewise read from the current directory.
- **`VERIFY_SSL`:** the example file ships with `true`. **The better fix for a self-signed certificate is to trust it instead of turning checking off:** point `VERIFY_SSL` at the certificate file (or at the CA that signed it) in PEM format, for example `VERIFY_SSL=/home/me/unifi-ca.pem` (a `~` is expanded, and a relative path is relative to the directory you run from; a directory of certificates also works). The path must exist and be readable, or the command stops with a message naming it, and a word that is neither a yes/no word nor a path is an error. You can export the certificate from your browser's certificate viewer while looking at the controller's address. A UniFi controller usually has a self-signed certificate, so the first run may fail with `TLS certificate verification failed`; then trust that certificate as described next, install a trusted certificate on the controller, or as a last resort set `VERIFY_SSL=false`, which sends your API key without checking who answers (acceptable on a trusted home network, not elsewhere). An unset or empty value verifies certificates. Any other word than the ones above is an error that lists the accepted words, so a typo such as `off-ish` can never silently mean "verify".
- **Protecting the API key:** the key is a credential for your controller, so keep `.env` private with `chmod 600 .env`. If the file that was read is accessible to your group or to other users (any of the group or other permission bits set), the command prints one warning that names the file and the `chmod 600` fix, and carries on. It is only a warning, and it is skipped on Windows where file modes mean little. A symlink is judged by the file it points to. The key is never printed: error messages, warnings and `repr()` of the configuration leave it out, and if a server or proxy echoes it back in an error body it is replaced with `***`.
- **`CONTROLLER_URL`:** it needs a scheme and a host (`https://host` or `https://host:port`; a trailing slash is removed). An `http://` URL is refused, because the API key is sent in a header of every request and would travel in clear text; use `https://` (with `VERIFY_SSL=false` for a self-signed certificate). For a lab network you trust you can opt in with `ALLOW_INSECURE_HTTP=true`; every run then prints a warning that the key travels in clear text. A URL containing a user name, password, query (`?`), fragment (`#`), space, backslash or control character is refused.
- **Timeouts and retries:** every request waits at most `TIMEOUT` seconds (default 15; `--timeout SECONDS` before the command overrides `.env`; a slow gateway may need more). A `GET` that fails with a connection error, a timeout or an HTTP 502, 503 or 504 is **retried twice** with a growing pause (0.5 s, then 1 s), so one brief blip no longer fails the command; anything else (a bad key, a 403, a 404 or 500, a certificate failure, a malformed answer) is reported at once, because trying again cannot change it. The single event-log `POST` is never retried. A message that says `(after 3 attempts)` means the retries were used up. A `403 Forbidden` means the API key is valid but not allowed to make that request.
- **When a read fails:** required data stops the command with exit code 3; optional data warns and the command carries on with less. The list of connected clients and devices is required everywhere. Every legacy read is optional, **including the client history (`stat/alluser`)** for `query`, `client`, `diagnose` and the other reports (they warn that offline clients and reservations are unavailable), except for the two commands whose answer would be wrong without it: `new-clients` and `snapshot`/`diff` stop with exit code 3, so they never print a misleading list or save an incomplete snapshot.
- **`SITE_ID`:** a site name may contain spaces and non-ASCII letters, but not `/`, `\`, `?`, `#` or control characters, and at most 128 characters; it is also percent-encoded wherever it appears in a URL.

### Seeing what the tool does: `--verbose`

`--verbose` (or `--debug`), given before the command, logs to **stderr** what the tool does, so the normal output on stdout is unchanged and can still be piped. It is the tool for diagnosing a slow run, a failing request or an undocumented endpoint:

```text
$ unifi-sentinel --verbose wan --json > wan.json
[verbose] unifi-sentinel 0.1.0: settings from /home/me/unifi-sentinel/.env; controller https://192.168.1.1:443, site default, timeout 15 s, TLS verification off
[verbose] GET /proxy/network/integration/v1/sites?offset=0&limit=200 -> 200 (41 ms)
[verbose] GET /proxy/network/api/s/default/stat/health -> 503 (35 ms)
[verbose] GET /proxy/network/api/s/default/stat/health -> retrying in 0.5 s (attempt 2 of 3)
[verbose] GET /proxy/network/api/s/default/stat/health -> 200 (38 ms)
[verbose] read 10 devices, 83 connected clients, 5 health subsystems, 91 speedtests
[verbose] 27 request(s), 1 retried, 0.7 s waiting for the controller
```

- **The first line** shows which `.env` was read (or that only environment variables were used), the controller, site, timeout and how TLS is verified (`on`, `off`, or the CA bundle in use), so a command that talks to the wrong controller is obvious.
- **One line per request attempt:** method, path (with the paging parameters), the HTTP status or what went wrong (`timed out`, `connection error`, `TLS certificate verification failed`), and the time in milliseconds. A retry shows the wait and the attempt number. The event-log `POST` shows the names of the query fields, never their values.
- **`read ...`** lists what a snapshot collected (counts only), and the last line is the total number of requests, retries and time spent waiting for the controller. The last line is also printed when a request fails.
- **Never logged:** the API key, response bodies, and query values. The paths do contain the site and device IDs, and the first line your controller's address, so **redact them before pasting the output into an issue**.

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
uv run unifi-sentinel.py export -o ./out   # write CSVs to a directory (long form: --output-dir)
uv run unifi-sentinel.py export --include-offline   # also list previously seen clients
uv run unifi-sentinel.py query devices               # UniFi devices with firmware and uptime
uv run unifi-sentinel.py query clients -s printer --json   # filter (long form: --search), JSON output
uv run unifi-sentinel.py query clients --include-offline   # also previously seen clients
uv run unifi-sentinel.py query reservations          # DHCP fixed IP reservations
uv run unifi-sentinel.py query reservations --offline   # reserved clients that have been offline for a day or more
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
uv run unifi-sentinel.py diagnose --json             # the same, as JSON with a stable code per finding
```

`query` takes an optional kind (`all` by default, `devices`, `clients`, `reservations` or `ports`). Run these from the project root (uv uses `pyproject.toml`). After `pip install .` use `unifi-sentinel <command>` instead. Run `--help` on the tool or any command for options, and `--version` for the version.

### Diagnose

`diagnose` prints findings sorted by severity:

| Level | Examples |
| ----- | -------- |
| 🛑 critical | an AP radio's channel utilization at or above `radio_util_critical_pct` (default 90%); a switch's PoE budget at or above `poe_critical_pct` (default 95%); the controller reports a WAN, internet or VPN subsystem in `error`; a LAN/WLAN `error` with no disconnected device to explain it; gateway offline; an offline switch or device that other devices uplink through; CPU or memory at or above `resource_critical_pct` (default 98%); a client with a DHCP reservation that has been offline for `reserved_offline_critical_days` (default 7) or more; a reserved IP inside its network's DHCP pool that another client is using right now |
| ⚠️ warning | 24-hour internet availability below `wan_availability_warn_pct` (default 99%), overall or for one monitoring target; the last speedtest well below the 30-day median (`wan_speed_drop_pct`, default 70%); an IP conflict reported in the last 24 hours; a client that disconnected `event_flap_count` or more times in that window, or a device that was unreachable that often; a Wi-Fi client with signal at or below `wifi_weak_signal_dbm` (default -75 dBm), `wifi_retry_pct` (default 30%) or more of its transmissions retried, or satisfaction below `wifi_satisfaction_warn` (default 50%); an AP radio with channel utilization at or above `radio_util_warn_pct` (default 70%), or retries or satisfaction past the same limits; a port whose link has gone down `link_flap_count` or more times since boot (default 5); a port dropping `port_drop_pct` or more of its packets (default 0.1%); an up port whose STP state is not forwarding; a switch's PoE budget at or above `poe_warn_pct` (default 80%); an uplink negotiated below what both ends support; a subsystem in `warning` the same way; internet latency at or above `wan_latency_warn_ms` (default 100 ms) or drops at or above `wan_drops_warn` (default 10); other offline devices; port rx/tx errors; half-duplex links; CPU or memory at or above `resource_warn_pct` (default 90%) but below the critical level; connected clients with no IP address or a link-local (169.254.x.x) address, shown with where they attach; DHCP reservation problems: an online client whose current IP differs from its reservation, the same IP reserved for several clients, or a reserved IP outside its network's subnet; the same IP in use by several clients or UniFi devices on any VLAN, or a reserved IP currently used by a different client or UniFi device; a client with a DHCP reservation that has been offline for `reserved_offline_warn_days` (default 1 day) or more; a reserved IP inside its network's DHCP pool; a private, carrier-grade NAT or link-local WAN address (double NAT or no address from the ISP) |
| ℹ️ info | a client that roamed `event_flap_count` or more times; a device that was unreachable earlier but is online now; high-latency events from the ISP monitor; the controller's LAN/WLAN status when it is only caused by disconnected devices (they are reported individually); devices waiting to be adopted; a failed speedtest; ports negotiated at or below `slow_link_mbps` (default 100 Mbps); legacy data unavailable (port checks skipped); a reservation whose client has no last-seen time; a reservation tied to a randomized (private) MAC address, and a count of the connected clients that use one; a network whose DHCP range is missing or invalid, so its reservations cannot be checked against it |

The reservation checks read the legacy `stat/alluser` and `rest/networkconf` endpoints (the same data as `query reservations`); offline clients are checked for duplicate and out-of-subnet reservations (a reservation whose network cannot be resolved is skipped for the subnet check) and for **being offline for too long**: a reserved client that is not connected and was last seen `reserved_offline_warn_days` (default 1) days ago or more is a warning, and `reserved_offline_critical_days` (default 7) or more is critical, so a server or appliance that went quiet does not stay invisible. A reservation with no last-seen time is reported once as info. UniFi devices are left to the device checks. A client that is meant to be off (a laptop, a seasonal device) belongs in the ignore list: `subject = "travel-laptop"`, `message = "is offline"`. `query reservations --offline` lists exactly the reservations this check reports, so you can inspect them before relying on the alerts.

**Reservations inside the DHCP pool.** For each network where the controller itself serves DHCP (`dhcpd_enabled` true and no DHCP relay), a reserved IP between the pool's first and last address (both ends included) is a warning: `reserved IP 10.0.20.150 is inside the DHCP pool 10.0.20.100-10.0.20.200 of IoT`. The gateway honours the reservation, but an address in the dynamic range can also be offered to other clients, and a device with a static address in the range collides with them, so the safe layout keeps reservations outside the range. It is critical when another client or UniFi device is using that address right now (the message names it). Networks where DHCP is off or relayed (and WAN or VPN networks, which can carry a range without serving client DHCP) are skipped; if DHCP is on but the range is missing or invalid, that network is one info finding (only if it has reservations) instead of a guess. A reservation placed in the pool on purpose belongs in the ignore list (`subject = "media-box"`, `message = "DHCP pool"`). The range comes from the same `rest/networkconf` data as the subnet check; `query reservations` has no in-pool column, so the table stays readable.

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
reserved_offline_warn_days = 1      # a reserved client offline this many days: warning
reserved_offline_critical_days = 7  # a reserved client offline this many days: critical

[[ignore]]
subject = "Garage AP"        # case-insensitive name; * and ? wildcards
message = "offline"          # case-insensitive substring; both must match if both given
reason = "spare AP, kept unplugged on purpose"   # required
```

Ignored findings are left out of the output, counted in the summary (`3 warnings (2 ignored)`), and excluded from exit codes, so a known-okay finding cannot fail a cron job. `diagnose --show-ignored` lists them with each rule's reason, so ignores do not hide problems forever. A rule needs a `reason` and a `subject` and/or `message`. A missing, unreadable or invalid file (unknown keys, bad values, rules without a reason) stops `diagnose` with exit code 3 before it contacts the controller. Other commands do not read this file. On Python 3.10 the `tomli` package (installed automatically) reads it; 3.11 and later use the standard library.

#### JSON output and finding codes

`diagnose --json` prints one JSON document on stdout (warnings still go to stderr) for scripts, dashboards and notifiers. The exit code is the same as without `--json` (`--fail-on` is honored), and `--no-emoji` has no effect. Findings suppressed by the ignore list are counted in `summary.ignored`; they are listed under a separate `ignored` key (each with its rule's `reason`) only with `--show-ignored`. A configuration error prints nothing on stdout and exits 3.

```json
{
  "version": 1,
  "summary": {
    "critical": 0,
    "warning": 10,
    "info": 2,
    "ignored": 0
  },
  "findings": [
    {
      "severity": "warning",
      "code": "device.offline",
      "subject": "Garage AP",
      "message": "device is offline",
      "mac": "AA:00:00:00:00:04"
    },
    {
      "severity": "warning",
      "code": "reservation.outside_subnet",
      "subject": "old-printer",
      "message": "reserved IP 10.0.0.50 is outside network IoT (10.0.20.1/24)",
      "mac": ""
    },
    {
      "severity": "info",
      "code": "port.slow_link",
      "subject": "Office Switch port 2",
      "message": "negotiated at 100 Mbps",
      "mac": "AA:00:00:00:00:02"
    }
  ]
}
```

(The example is from the synthetic fixture with `--no-events`; the findings are shortened, so they do not add up to the summary.)

- **`version`** is the document format (currently `1`); it changes only when a field is removed or renamed. New fields may be added without a new version.
- **`code`** names the check that produced the finding, so a script does not have to match wording that can change. Codes are an interface: they are never renamed or reused. The same finding can have a different `severity` between runs (a device offline is critical for a gateway, a warning otherwise), so key on `code` and `subject`.
- **`subject`** and **`message`** are the same text as the plain output, with names exactly as the controller reports them (JSON escapes control characters; treat them as untrusted data if you pass them on).
- **`mac`** is the upper-case MAC address of the device the finding is about, or an empty string when it is not about one device.
- `client --json` and `topology --json` carry the same `code` in each of their findings.

| Code | What it reports |
| ---- | --------------- |
| `client.link_local_ip` | a connected client has a link-local (169.254.x.x) address |
| `client.no_ip` | a connected client has no IP address |
| `client.private_mac_summary` | how many connected clients use randomized (private) MAC addresses |
| `controller.legacy_unavailable` | legacy device data could not be read, so port checks were skipped |
| `controller.pending_adoption` | devices waiting to be adopted |
| `device.cpu_high` | device CPU utilization at or above the warning threshold |
| `device.memory_high` | device memory utilization at or above the warning threshold |
| `device.offline` | a UniFi device is not online (critical for a gateway or a device that others uplink through) |
| `event.client_disconnects` | a client disconnected repeatedly in the event window |
| `event.client_roams` | a client roamed repeatedly in the event window |
| `event.device_unreachable` | a device was reported unreachable in the event window |
| `event.internet_latency` | the controller reported high internet latency in the event window |
| `event.ip_conflict` | the controller reported an IP conflict in the event window |
| `event.log_truncated` | the event log read hit its cap, so event counts may be low |
| `health.device_subsystem` | lan/wlan subsystem status that only reflects disconnected devices |
| `health.subsystem` | a controller health subsystem is in a warning or error state |
| `internet.drops` | internet drops at or above the threshold |
| `internet.latency` | internet latency at or above the threshold |
| `internet.speedtest_failed` | the last speedtest failed |
| `ip.duplicate` | the same IP is in use by several clients or devices |
| `link.below_capability` | an uplink negotiated below what both ends support |
| `port.drops` | a switch port is dropping packets above the threshold |
| `port.errors` | a port has rx/tx errors |
| `port.half_duplex` | a port link is half duplex |
| `port.link_flaps` | a switch port's link has gone down repeatedly since boot |
| `port.poe_budget` | a switch's PoE budget use is at or above the threshold |
| `port.slow_link` | a port negotiated at or below the slow-link speed |
| `port.stp` | an up port is not in the STP forwarding state |
| `reservation.duplicate` | the same IP is reserved for several clients |
| `reservation.in_dhcp_pool` | a reserved IP lies inside its network's dynamic DHCP range |
| `reservation.ip_in_use` | a reserved IP is in use by a different client or device |
| `reservation.ip_mismatch` | an online client's IP differs from its reservation |
| `reservation.never_seen` | a reservation whose client has no last-seen time |
| `reservation.offline` | a client with a reservation has been offline longer than the threshold |
| `reservation.outside_subnet` | a reserved IP is outside its network's subnet |
| `reservation.pool_unknown` | a network's DHCP range is missing or invalid, so its reservations cannot be checked |
| `reservation.private_mac` | a reservation is tied to a randomized (private) MAC address |
| `wan.availability` | 24-hour internet availability below the threshold |
| `wan.cgnat` | the WAN address is in the carrier-grade NAT range (100.64.0.0/10) |
| `wan.double_nat` | the WAN address is private: the gateway is behind another router doing NAT |
| `wan.link_local_address` | the WAN address is link-local: the gateway got no address from the ISP |
| `wan.monitor_availability` | one monitored internet target below the availability threshold |
| `wan.speedtest_slow` | the last speedtest download is well below the 30-day median |
| `wifi.client_retries` | a Wi-Fi client retries too many transmissions |
| `wifi.client_satisfaction` | a Wi-Fi client's satisfaction is below the threshold |
| `wifi.radio_retries` | an AP radio retries too many transmissions |
| `wifi.radio_satisfaction` | an AP radio's satisfaction is below the threshold |
| `wifi.radio_utilization` | an AP radio's channel utilization is at or above the threshold |
| `wifi.weak_signal` | a Wi-Fi client's signal is at or below the threshold |

#### Notifications

`diagnose --notify` sends a short message to **ntfy** and/or a **generic webhook** when something changed since the last notified run, so you hear about a new problem once instead of reading cron output. Nothing is ever sent unless you pass `--notify` **and** have configured a destination, and nothing is ever sent to the controller's address or any other place.

```bash
# in .env (the URLs and tokens are secrets; keep the file private, `chmod 600 .env`)
NOTIFY_NTFY_URL=https://ntfy.example.com/a-long-random-topic-name
NOTIFY_NTFY_TOKEN=tk_...                  # optional, for a protected topic
NOTIFY_WEBHOOK_URL=https://hooks.example.com/in/abc123
NOTIFY_WEBHOOK_TOKEN=...                  # optional, sent as "Authorization: Bearer ..."

uv run unifi-sentinel.py diagnose --notify --notify-baseline   # once: treat today's findings as already reported
*/15 * * * * cd /path/to/unifi-sentinel && uv run unifi-sentinel.py diagnose --notify --fail-on critical
```

- **When it sends:** a finding at or above `--notify-min` (default `warning`) that is **new** (not reported before), has got **worse** (warning to critical), or is a **critical** one still unresolved after `notify_repeat_hours` (default 24, `0` turns reminders off; set it in `[thresholds]`); and a **recovery** note when a reported finding has gone (or fallen below `--notify-min`). Warnings and info are sent once, never repeated. Unchanged situations send nothing, and one run sends one message per destination (at most 20 lines, then `... and N more`).
- **Identity:** a finding is the same finding when its check (`code`) and subject are the same, whatever its wording says, so a changing count does not re-send. What was reported is remembered in `snapshots/notify-state.json` (git-ignored, owner-only; `--notify-state FILE` changes it). The first run with no state would report everything, so run `--notify-baseline` once to record today's findings as already reported.
- **`--notify-dry-run`** prints the message that would be sent to stderr and sends nothing and keeps the state, the way to check the wording and what leaves your network. The ignore list applies before notifying, so ignored findings are never sent.
- **What leaves your network:** the finding text, which **includes device and client names, IP addresses and MACs** (an alert without the name is hard to act on), to the services you configured and nowhere else. **`--notify-redact`** sends only the generic description of each check and a count (`[WARNING] NEW  a switch port is dropping packets above the threshold (x3)`), no names, addresses or MACs. Never sent: the API key, the controller's address, the site id. Names are untrusted data (control characters are removed from the text; a chat system that renders markdown or mentions may still show them).
- **ntfy:** a public `ntfy.sh` topic is readable by **anyone who knows its name**, so use a long random topic or your own server, and treat the topic like a password (it is part of the URL, kept in `.env`, never printed). The message is the body; the priority is 5 for a critical problem, 4 for a warning, 3 otherwise, with a matching tag.
- **Webhook:** a JSON `POST` with `source`, `version`, `redacted`, `title`, a ready-to-show `text`, and an `events` list (`event` of `new`, `worsened`, `reminder` or `recovered`, `severity`, `code`, `description`, and, unless redacted, `subject` and `message`). Slack and Discord style webhooks are not adapted yet.
- **Safety of the destinations:** `https://` only (a lab can opt in with `ALLOW_INSECURE_HTTP=true`), TLS is always verified, redirects are not followed (a redirect could carry the token elsewhere), and a user name or password in the URL is refused. Errors say only which destination and a fixed reason (`HTTP 403`, `timed out`, `connection error`, `TLS certificate verification failed`), never the URL, the topic or the token.
- **Failures and exit codes:** the findings decide the exit code as usual. If a message could not be delivered to any destination, a warning goes to stderr, the state is **not** updated (the next run tries again), and the exit code is `3` **only if the findings would have given `0`**, so a cron job notices. One destination working is enough. `--notify-*` options other than `--notify` itself are usage errors without it.

#### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success; for `diagnose`, no findings at or above the `--fail-on` threshold |
| 1 | `diagnose` found at least one non-critical finding at or above the `--fail-on` threshold |
| 2 | `diagnose` found at least one critical finding |
| 3 | error: bad configuration, or the controller could not be reached or returned an error; also `diagnose --notify` when the findings gave 0 but the notification could not be delivered |
| 4 | `client` found no client, or several (it lists them) |
| 64 | command-line usage error |

`--fail-on {info,warning,critical}` sets the lowest severity that gives a non-zero code (default `warning`). Critical always exits 2. Example cron entry that only alerts on outages:

```bash
*/15 * * * * cd /path/to/unifi-sentinel && uv run unifi-sentinel.py diagnose --fail-on critical || notify-me
```

Event-based warnings (above) count towards exit code 1 like any other warning. `--json` does not change any exit code.

Tool errors used to exit 1 for every command; they now exit 3 so that 1 and 2 only ever mean findings.

### Devices

`query devices` shows each UniFi device with its firmware version, whether a firmware update is available, and its uptime (for example `2d 7h`). Offline devices have no uptime. `--json` adds `Uptime (s)` with the raw seconds. These columns come from the Integration API and appear only for `query devices`; the `export` CSV columns are unchanged.

### Snapshots and diff

When something breaks, the first question is "what changed since it last worked?". `snapshot` saves the inventory, and `diff` compares.

```bash
uv run unifi-sentinel.py snapshot                         # now: ./snapshots/snapshot-20261001-011530Z.json
# ...later, when something is wrong...
uv run unifi-sentinel.py diff                             # the newest snapshot against the network right now
uv run unifi-sentinel.py diff --last-two                  # the two newest snapshots (no controller needed)
uv run unifi-sentinel.py diff snapshot-20260929-080000Z.json   # a named snapshot against now
uv run unifi-sentinel.py diff OLD.json NEW.json           # two files
```

```text
Comparing snapshot-20261001-011530Z.json (captured 2026-09-30 20:15) -> the network right now

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
- **Files:** `snapshot` writes `snapshot-YYYYMMDD-HHMMSSZ.json` into `./snapshots/` (the time in the name is UTC, the `Z`, so the order never depends on time zone or daylight saving; the time inside the file keeps your local time and offset; files named without the `Z` by earlier versions are local time and still listed and sorted correctly, using their offset-bearing `captured_at` when available to disambiguate a repeated hour and falling back to the local filename time if unreadable) (change it with `--dir DIR`), never overwriting an existing file. `-o FILE` (long form `--output`) picks the name; it refuses to replace an existing file unless you add `--force`. `--keep N` afterwards deletes the oldest snapshots in the directory beyond the newest N; it only touches files named like the ones this tool writes, and never the one just saved.
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

Findings on these devices:
  [WARNING ] Office Switch: CPU utilization 95%
  [WARNING ] Office Switch: PoE budget 41.6 W of 52 W used (80%)
  [WARNING ] Office Switch: uplink to Gateway negotiated at 100 Mbps but both ends support 1000 Mbps
  [WARNING ] Office Switch port 1: link has gone down 5 times since boot, switch up 3h 12m
  [WARNING ] Office Switch port 2: 4 rx/tx errors
  [WARNING ] Office Switch port 2: link is half duplex
  [WARNING ] Office Switch port 2: dropping 0.75% of rx packets (75 of 10000)
  [WARNING ] Office Switch port 2: STP state is blocking, not forwarding
  [WARNING ] Garage AP: device is offline

4 devices, 2 clients, 1 offline, 1 link(s) below capability, 2 with findings
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
    Line Break xxxxxxxxxxxxxxxxxxxxxxxxxxxx…  (-70 dBm, WPA2-Personal (AES/CCMP))

5 GHz
Channel  Neighbors  Strong  Your radios
-------  ---------  ------  -----------
36       0          0       Office AP
44       1          1
149      1          1

  Channel 44: strongest of 1 stronger than -80 dBm
    Five GHz Neighbor  (-55 dBm, WPA2-Personal (AES/CCMP))

  Channel 149: strongest of 1 stronger than -80 dBm
    Far Block  (-70 dBm, WPA2-Personal (AES/CCMP))

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
  NAT: none seen (public WAN address; a modem doing NAT in front of the gateway cannot be seen)
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
  Last: 2026-10-01 10:13 (6h ago): download 880 Mbps, upload 40 Mbps, latency 25 ms
  Download: min 500 Mbps, median 925 Mbps, max 940 Mbps
  Upload: min 31 Mbps, median 40 Mbps, max 41 Mbps
  Latency: min 23 ms, median 24 ms, max 41 ms

  Download below 70% of the median (1):
    2026-09-23 16:13  download 500 Mbps, upload 31 Mbps, latency 41 ms
```

- **Now:** the WAN and internet subsystems of the controller's health (status, ISP, WAN IP, latency, drops) and the gateway's WAN link: its negotiated speed against what the port supports (a 1 Gbps plan on a 2.5 Gbps port is normal, so that is only shown, never flagged) and the live traffic rate.
- **NAT:** whether the gateway is behind NAT, judged from the address of its WAN port (the `wan_ip` of the controller's health, nothing is looked up outside your network). A **private** address (10.x, 172.16 to 172.31, 192.168.x, or an IPv6 `fc00::/7`) means another router that does NAT sits in front of the gateway (**double NAT**); an address in **100.64.0.0/10** is carrier-grade NAT, where the ISP shares one public address between customers; a **link-local** address (169.254.x.x or IPv6 `fe80::/10`) means the gateway got no address from the ISP. Double NAT and carrier-grade NAT break inbound port forwards and some VPNs, game and camera features. `--json` has it under `nat` (`wan_ip`, `kind` of `public`, `private`, `cgnat`, `link_local`, `none` or `unknown`, and `message`). **Limits:** a public-looking address does not prove there is no NAT, because a modem or router in front of the gateway that translates addresses while handing the gateway a public one cannot be seen without an outside lookup, which this tool does not make; and only the primary WAN is checked (the `wan_ip` of the health entry), not a second WAN.
- **Last 24 hours:** the controller's own monitoring of the connection: overall availability and average latency, and each monitoring target (`icmp` ping or `dns`) with its availability and latency. `Alerts: yes` marks a target the controller is configured to alert on; it does not mean the target is failing.
- **Speedtests:** every stored result (the controller runs them on a schedule) over `--days N` (default 30), using the latest run's `wan_networkgroup` or `interface_name` so dual-WAN links are not mixed: the last one and how old it is, the minimum, median and maximum of download, upload and latency, and the runs whose download fell below `wan_speed_drop_pct` (default 70%) of the median, with their dates. That is where a degradation window shows up; try `--days 90`. At least 5 runs are needed before the median means anything.
- `--json` prints the same data, and `--config FILE` (or `./unifi-sentinel.toml`) sets the threshold. A missing piece (no speedtests stored, no monitoring data, a gateway with no WAN link data) is simply left out.
- **`diagnose` uses the same data:** a warning (`wan.double_nat`, `wan.cgnat` or `wan.link_local_address`) when the NAT check above finds a private, shared or link-local WAN address (silence a deliberate double NAT with an ignore rule: `subject = "wan"`, `message = "double NAT"`), a warning when 24-hour availability, overall or for any single monitoring target, is below `wan_availability_warn_pct` (default 99%), and a warning when the last speedtest (within 30 days) is below `wan_speed_drop_pct` of the 30-day median, with its age.
- **Not included:** an hourly traffic and latency history. The controller only returns that from a POST to its report endpoint, which is outside the one approved POST (the event log); a plain GET returns empty rows.

### Event history

`events` reads the controller's event log, so it can answer "why did the Wi-Fi drop at 3 pm?", which none of the other commands can because they show the network as it is now. The controller keeps about three months.

```text
uv run unifi-sentinel.py events --client phone --since 6h
Time                 Severity  Category        Event                         Message
-------------------  --------  --------------  ----------------------------  --------------------------------------------------
2026-10-01 16:03:05  Low       CLIENT_DEVICES  CLIENT_DISCONNECTED_WIRELESS  phone disconnected from Home. Time Connected: 25s.
2026-10-01 15:48:05  Low       CLIENT_DEVICES  CLIENT_CONNECTED_WIRELESS     phone connected to Home on Office AP.
2026-10-01 15:33:05  Low       CLIENT_DEVICES  CLIENT_DISCONNECTED_WIRELESS  phone disconnected from Home. Time Connected: 2m.
2026-10-01 15:23:05  Low       CLIENT_DEVICES  CLIENT_ROAMED                 phone roamed from Garage AP to Office AP.
2026-10-01 15:13:05  Low       CLIENT_DEVICES  CLIENT_DISCONNECTED_WIRELESS  phone disconnected from Home. Time Connected: 1h.

5 event(s)
```

Options (the filters combine with AND; the first group is done by the controller, the second by this tool):
- `--since DURATION`: how far back, such as `90m`, `24h`, `7d` or `2w` (default `24h`)
- `--category NAME` (repeatable): for example `CLIENT_DEVICES`, `UNIFI_DEVICES`, `INTERNET_AND_WAN` or `AUDIT`
- `--severity low|medium|high` (repeatable) and `-s TEXT` or `--search TEXT` (text search)
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
  2026-10-01 14:43:05  CLIENT_CONNECTED_WIRED: desktop connected to Main on Office Switch Port 3.

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

`new-clients` lists every known client, connected or not, that has not been added to at least one client group (Network > Client Groups), so newly seen devices stand out. Add a client to a group in the controller and it drops off the report. Columns: Name, MAC Address, IP Address, Vendor, Connection Type, Where (switch and port, or AP), First Seen, Last Seen, Status, Private MAC (`yes` for a randomized address, see below). Newest first-seen comes first, with no age cutoff. `-s TEXT` (or `--search TEXT`) filters and `--json` prints JSON.

Group membership comes from the legacy `stat/alluser` client records and the legacy v2 `network-members-groups` definitions; the Integration API has no client groups. A group that has been deleted does not count as membership. If the group definitions cannot be read, the tool warns and trusts each client's own group list.

### Randomized MAC addresses

Phones, tablets and laptops often use a **private (randomized) Wi-Fi MAC address**, frequently a different one for each network, and some rotate it. That makes one device look like several, defeats "new client" detection, makes `snapshot`/`diff` noisy, and breaks DHCP reservations, which are tied to one MAC. A MAC address is **randomized** here when it is a locally administered unicast address: the second hex digit is `2`, `6`, `A` or `E` (the second-lowest bit of the first byte is set and the lowest is clear). This is read from the address itself, so it needs no extra request.

- `query clients` (table and `--json`) and `new-clients` have a **Private MAC** column: `yes` for a randomized address, empty otherwise. The CSV export and `query devices` are unchanged.
- `client` adds `[randomized MAC: reservations and history may not hold]` after the MAC, and `client --json` has `identity.private_mac` (`true` or `false`).
- `diagnose` adds two **info** findings, never a warning, because it is normal for phones: `reservation.private_mac` for each reservation whose MAC is randomized (it stops applying if the device changes its address), and one `client.private_mac_summary` finding with the count of connected clients that use randomized addresses. To silence both, use an ignore with `message = "randomized"` (and a required `reason`); add `subject = "clients"` to silence only the summary, or use the reservation's client name to silence only that reservation.

It is a hint, not proof: virtual machines, containers, bridges, VPNs and some IoT devices also use locally administered addresses, and for them the vendor (OUI) lookup is empty. A device that turned the feature off keeps its old random MAC until it reconnects.

### Switch ports

`query ports` lists every port on every switch: status, speed, duplex, PoE power, the connected client or device, and rx/tx errors (`--json` includes every column, such as traffic counters). Filters, which combine with AND:

- `--switch NAME`: switches whose name contains NAME (case-insensitive)
- `--down`: only ports that are down
- `--errors`: only ports with rx/tx errors
- `-s TEXT`: text match on any field, for example a connected client's name

`--switch`, `--down` and `--errors` are only valid with `query ports`. Port data comes from the legacy `stat/device` and `stat/sta` endpoints, so it is empty (with a warning) if those are unavailable.

### DHCP reservations

`query reservations` lists every enabled fixed IP reservation, including clients that are currently offline. Columns: Name, MAC Address, Reserved IP, Network, VLAN, Current IP, Status, Last Seen. It reads the legacy `stat/alluser` and `rest/networkconf` endpoints, since the Integration API does not expose reservations. Only clients with the reservation enabled are listed; disabled reservations keep a stale IP on the controller and are ignored.

**`--offline`** keeps only the reservations whose client is not connected and was last seen at least `reserved_offline_warn_days` ago (default 1 day, from `./unifi-sentinel.toml` or `--config FILE`), or has no last-seen time, and adds an **Offline For** column (`6d`, `30h`, or `never seen`). It is the same rule the `diagnose` check uses, without severities and without the ignore list, so it shows the whole set. It only applies to `query reservations`; `--config` is only valid together with `--offline`.

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
[WARNING ] Office Switch: PoE budget 41.6 W of 52 W used (80%)
[WARNING ] Office Switch: uplink to Gateway negotiated at 100 Mbps but both ends support 1000 Mbps
[WARNING ] Office Switch port 1: link has gone down 5 times since boot, switch up 3h 12m
[WARNING ] Office Switch port 2: 4 rx/tx errors
[WARNING ] Office Switch port 2: link is half duplex
[WARNING ] Office Switch port 2: dropping 0.75% of rx packets (75 of 10000)
[WARNING ] Office Switch port 2: STP state is blocking, not forwarding
[WARNING ] old-printer: reserved IP 10.0.0.50 is outside network IoT (10.0.20.1/24)
[INFO    ] Office Switch port 2: negotiated at 100 Mbps
[INFO    ] wlan: wlan subsystem reports warning: 1 device(s) disconnected (see the device findings)

10 warnings, 2 info
```

### new-clients

```text
Name         MAC Address        IP Address  Vendor  Connection Type  Where                        First Seen           Last Seen            Status   Private MAC
-----------  -----------------  ----------  ------  ---------------  ---------------------------  -------------------  -------------------  -------  -----------
old-tablet   BB:00:00:00:00:04  10.0.0.51           Wireless                                      2025-06-15 15:06:40  2025-12-06 05:46:40  Offline
old-printer  BB:00:00:00:00:03  10.0.0.50           Wired            Wired, Office Switch port 6  2023-11-14 22:13:20  2026-10-01 13:13:05  Offline

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
- **`TLS certificate verification failed`**: for a self-signed certificate point `VERIFY_SSL` at the certificate (or CA) file, or install a valid certificate on the controller; `VERIFY_SSL=false` skips the check and is a last resort. If `VERIFY_SSL` already names a file, the message says the certificate is not signed by anything in it.
- **A command is slow or fails and you want to see why**: run it again with `--verbose` (see above) to see each request, its status and time, and any retries.
- **Connection errors or timeouts**: check `CONTROLLER_URL` and that the controller is reachable from this machine. `timed out after 15 s (after 3 attempts)` means a slow gateway: raise `TIMEOUT` or pass `--timeout 60`.
- **`403 Forbidden`**: the API key is valid but lacks access to that request; check the key under Settings > Control Plane > Integrations.
- **`VERIFY_SSL names a CA bundle that does not exist`**: fix the path (it is relative to the directory you run from).
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
  snapshot.py            collect_snapshot: one read of the controller, as declared by a Needs object, output-agnostic
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
  notify.py              notifications: what changed since the last run, ntfy/webhook sending, state file
  settings.py            diagnose thresholds and ignore list (TOML)
  util.py                shared helpers: output safety (printable names, CSV formulas), numbers, MACs, times, plurals
  cli.py                 argument parser and main: loads the configuration, builds the client, runs a command
  commands.py            the commands: each one's arguments, checks and handler, and the registry
tests/
  conftest.py            FakeSession: a fake controller served from the fixture
  fixtures/controller.json   synthetic, sanitized controller data
```

New features are new subcommands (a section and a registry row in `commands.py`) backed by modules that take a `Snapshot` (fetching stays in `snapshot.py` and `client.py`). Dependencies are declared once, in `pyproject.toml` (lockfile: `uv.lock`; regenerate with `uv lock`). Run the tests with `uv run pytest`; they use a synthetic fixture in `tests/fixtures/` and never contact a controller.

Checks (the same ones CI runs on every push and pull request, in `.github/workflows/ci.yml`):

```bash
uv run pytest             # tests; CI runs them on Python 3.10, 3.11, 3.12 and 3.13
uv run ruff check .       # lint (rules E, F, B, I, UP in pyproject.toml; lines up to 120 characters, tests exempt)
uv run mypy               # types; reported in CI but not blocking yet (about 27 known findings)
uv lock --check           # uv.lock must match pyproject.toml; run `uv lock` after changing dependencies
```

**Tests that keep the documentation and the output honest:**

- `tests/test_golden.py` compares the text output of the main commands (`diagnose`, `topology`, `wan`, `wifi`, `client`, `events`, `new-clients` and the `query` kinds) with stored files in `tests/golden/`, produced from the synthetic fixture. Times, ages and table padding are normalised, so the files do not change from day to day. When a change to the output is intended, refresh them with `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff like code.
- `tests/test_docs_drift.py` checks this README against the program: every example command parses with the real argument parser, every command has a row in the Commands table, every long option is mentioned (and every option the README shows exists), and the sample output blocks (topology, wifi, wan, client, diagnose, new-clients, events) equal what the commands print. After an intended output change, `UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py` rewrites those blocks. The `diff` sample is illustrative on purpose and is not checked.
- `tests/test_entry_points.py` runs the launcher, the installed `unifi-sentinel` script and `python -m unifi_sentinel.cli` in subprocesses, and `tests/test_exit_codes.py` produces every documented exit code (0, 1, 2, 3, 4 and 64) from a real scenario and checks the table above.
- Coverage: `COVERAGE_FILE=/tmp/.coverage uv run --with coverage coverage run --branch --source=unifi_sentinel -m pytest` then `COVERAGE_FILE=/tmp/.coverage uv run --with coverage coverage report -m`. It is at 100% of lines and branches; the few `pragma: no cover`/`no branch` comments say why a line cannot run (for example the Python 3.10-only `tomli` import, which CI covers).

`uv run ruff check . --fix` applies the safe fixes (import order, unused imports). The `List[...]` and `Optional[...]` annotation style is not enforced yet, and there is no code formatter. See [CLAUDE.md](CLAUDE.md) for contributor and AI-assistant guidelines.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests are welcome. Work is tracked in [GitHub issues](https://github.com/jeffholst/unifi-sentinel/issues).

## Acknowledgments

- [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), the project this was forked from
- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) from Ubiquiti
- [uv](https://docs.astral.sh/uv/) for Python package management
