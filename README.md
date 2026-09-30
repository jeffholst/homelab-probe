# UniFi Sentinel

A command-line tool for querying, troubleshooting and inventorying a UniFi Network controller. It is **read-only**: it only sends GET requests to the controller.

> **Status: early development.** Tested against one live controller (Network 10.6.106); other versions and hardware may differ. See [open issues](https://github.com/jeffholst/unifi-sentinel/issues) for the roadmap.

## Credits

UniFi Sentinel is a fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export) by Eric Fitzgerald, whose CSV export is the foundation of the `export` command. It is licensed under the Apache License 2.0, as is the original.

## Commands

| Command  | Description                                                    |
| -------- | -------------------------------------------------------------- |
| `export` | Export connected clients, UniFi devices and switch ports to CSV |
| `query`  | List and filter devices, clients, DHCP reservations and switch ports (table or `--json`) |
| `new-clients` | List clients that are in no client group, to spot new devices |
| `diagnose` | Read-only health checks with 🛑 critical, ⚠️ warning and ℹ️ info findings |
| `info`   | Show the controller application info and available sites       |

Planned: richer inventory and troubleshooting reports.

## Features

- **Client and device inventory**: connected clients (wired and wireless) and all UniFi devices (switches, access points, gateways) in one CSV
- **Switch port mapping**: per-switch CSVs with port status, speed, duplex, PoE, connected client or device, and traffic counters
- **Network topology**: which switch and port each client or device is attached to
- **DHCP reservations**: list every fixed IP reservation, including offline clients, with network and VLAN
- **Querying and health checks**: filter devices and clients from the command line (table or JSON) and run read-only diagnostics
- **Official API first**: uses the UniFi Network Integration API (`/proxy/network/integration/v1`). Legacy endpoints are used only for data the Integration API does not expose (per-port counters, client-to-port mapping, DHCP reservations and network config) and degrade gracefully with a warning if unavailable
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
uv run unifi-sentinel.py query reservations          # DHCP fixed IP reservations
uv run unifi-sentinel.py query ports                 # every switch port
uv run unifi-sentinel.py query ports --down --switch rack   # down ports on matching switches
uv run unifi-sentinel.py query ports --errors        # ports with rx/tx errors
uv run unifi-sentinel.py new-clients                 # clients in no client group
uv run unifi-sentinel.py diagnose                    # health checks
```

Run these from the project root (uv uses `pyproject.toml`). After `pip install .` use `unifi-sentinel <command>` instead. Run `--help` on the tool or any command for options.

### Diagnose

`diagnose` prints findings sorted by severity:

| Level | Examples |
| ----- | -------- |
| 🛑 critical | gateway offline; an offline switch or device that other devices uplink through; CPU or memory at 98% or higher |
| ⚠️ warning | other offline devices; port rx/tx errors; half-duplex links; CPU or memory at least 90% but below 98%; connected clients with no IP address or a link-local (169.254.x.x) address, shown with where they attach; DHCP reservation problems: an online client whose current IP differs from its reservation, the same IP reserved for several clients, or a reserved IP outside its network's subnet; the same IP in use by several clients or UniFi devices on any VLAN, or a reserved IP currently used by a different client or UniFi device |
| ℹ️ info | ports negotiated at 100 Mbps or less; legacy data unavailable (port checks skipped) |

The reservation checks read the legacy `stat/alluser` and `rest/networkconf` endpoints (the same data as `query reservations`); offline clients are only checked for duplicate and out-of-subnet reservations, and a reservation whose network cannot be resolved is skipped for the subnet check.

Emoji labels are used on a UTF-8 terminal. When output is piped or redirected, or with `--no-emoji`, it prints text labels (`[CRITICAL]`, `[WARNING ]`, `[INFO    ]`) instead.

#### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success; for `diagnose`, no findings at or above the `--fail-on` threshold |
| 1 | `diagnose` found at least one non-critical finding at or above the `--fail-on` threshold |
| 2 | `diagnose` found at least one critical finding |
| 3 | error: bad configuration, or the controller could not be reached or returned an error |
| 64 | command-line usage error |

`--fail-on {info,warning,critical}` sets the lowest severity that gives a non-zero code (default `warning`). Critical always exits 2. Example cron entry that only alerts on outages:

```bash
*/15 * * * * cd /path/to/unifi-sentinel && uv run unifi-sentinel.py diagnose --fail-on critical || notify-me
```

Tool errors used to exit 1 for every command; they now exit 3 so that 1 and 2 only ever mean findings.

### Devices

`query devices` shows each UniFi device with its firmware version, whether a firmware update is available, and its uptime (for example `2d 7h`). Offline devices have no uptime. `--json` adds `Uptime (s)` with the raw seconds. These columns come from the Integration API and appear only for `query devices`; the `export` CSV columns are unchanged.

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

The official documentation covers the Integration API only. The legacy `stat/*` and `rest/*` endpoints this tool also uses (port counters, client-to-port mapping, DHCP reservations, network config) are undocumented; their fields were determined from live controller responses.

## Troubleshooting

- **`CONTROLLER_URL is not set` / `API_KEY is not set`**: copy `example.env` to `.env` and fill it in.
- **`401 Unauthorized`**: the API key is invalid or was revoked; create a new one.
- **`TLS certificate verification failed`**: for a self-signed certificate set `VERIFY_SSL=false`, or install a valid certificate on the controller.
- **Connection errors or timeouts**: check `CONTROLLER_URL` and that the controller is reachable from this machine.
- **`Site '...' not found`**: run `info` to list site names, references and IDs.
- **`legacy stat/... unavailable` warning**: switch port mapping and counters will be incomplete, but the rest of the export still runs.

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
  diagnose.py            read-only health checks
  cli.py                 argparse subcommands
```

New features are new subcommands in `cli.py` backed by modules that take a `UniFiClient`. Dependencies are declared once, in `pyproject.toml` (lockfile: `uv.lock`; regenerate with `uv lock`). Run the tests with `uv run pytest`; they use a synthetic fixture in `tests/fixtures/` and never contact a controller. See [CLAUDE.md](CLAUDE.md) for contributor and AI-assistant guidelines.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests are welcome. Work is tracked in [GitHub issues](https://github.com/jeffholst/unifi-sentinel/issues).

## Acknowledgments

- [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), the project this was forked from
- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) from Ubiquiti
- [uv](https://docs.astral.sh/uv/) for Python package management
