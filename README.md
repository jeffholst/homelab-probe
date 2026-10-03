# UniFi Sentinel

A command-line tool for querying, troubleshooting and inventorying a UniFi Network controller. It is **read-only**: it never changes anything on the controller. Every request is a GET, with one exception: the event log can only be queried with a POST, so `events`, and `diagnose` and `client` by default (`--no-events` skips it), send a read-only query to that one endpoint (see [Event history](#event-history)). Nothing else is ever sent anywhere, with one opt-in exception: `diagnose --notify` can send a short message to a notification service or mail server you configure (see [Notifications](docs/notifications.md#notifications)).

> **Status: early development.** Tested against one live controller (Network 10.6.106); other versions and hardware may differ. See [open issues](https://github.com/jeffholst/unifi-sentinel/issues) for the roadmap.

## Credits

UniFi Sentinel is a fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export) by Eric Fitzgerald, whose CSV export is the foundation of the `export` command. It is licensed under the Apache License 2.0, as is the original.

## Commands

| Command | Description | |
| ------- | ----------- | --- |
| `export` | Export connected clients, UniFi devices and switch ports to CSV | [details](docs/inventory.md#output-files) |
| `query` | List and filter devices, clients, DHCP reservations and switch ports (table, `--json` or `--csv`) | [details](docs/inventory.md#devices) |
| `snapshot` | Save the current inventory to a local JSON file, to compare later | [details](docs/inventory.md#snapshots-and-diff) |
| `diff` | What changed: compare saved snapshots, or a snapshot against the live network | [details](docs/inventory.md#snapshots-and-diff) |
| `topology` | Draw the uplink tree from the gateway down: ports, link speeds, client counts and problems | [details](docs/network.md#topology) |
| `wifi` | Wireless report: each AP's radios and a channel plan from the neighboring networks | [details](docs/network.md#wi-fi) |
| `wan` | Internet health: current state, 24-hour monitoring and speedtest history | [details](docs/network.md#wan) |
| `firewall` | Firewall policies, port forwards and the zone matrix, with what looks wrong (zone-based firewall) | [details](docs/network.md#firewall) |
| `events` | Event history from the controller log: disconnects, roams, IP conflicts, device outages | [details](docs/network.md#event-history) |
| `client` | Troubleshoot one client by name, MAC or IP: where it attaches, link quality and related findings | [details](docs/network.md#client-view) |
| `new-clients` | List clients that are in no client group, to spot new devices | [details](docs/inventory.md#new-clients) |
| `audit` | Configuration audit: Wi-Fi networks that are open or WPA2-only, default device names, firmware updates, unnamed clients | [details](docs/diagnose.md#audit) |
| `diagnose` | Read-only health checks with 🛑 critical, ⚠️ warning and ℹ️ info findings (`--json` for scripts) | [details](docs/diagnose.md#diagnose) |
| `info` | Show the controller application info and available sites       |  |

The tool is still evolving; see open issues for planned reports, controller-version coverage, packaging, and release work.

## Features

- **Inventory and exports**: clients, UniFi devices and switch ports as tables, JSON or CSV, with DHCP reservations, client groups, randomized-MAC detection, and snapshots that show what changed
- **Troubleshooting**: `diagnose` health checks with exit codes and stable finding codes, `client` for one device, `topology`, `wifi`, `wan`, `events` and `firewall` views, and `audit` for risky settings
- **Notifications**: ntfy, a webhook or email when a problem is new, worse or fixed, opt-in, with redaction
- **Official API first**, read-only (every request is a GET, apart from one read-only event-log query), safe output for names and CSV cells, and credentials kept in a `.env` file

All of them, in full, are in [docs/features.md](docs/features.md).

## Requirements

- Python 3.10 or higher
- A UniFi Network Application recent enough to support the Integration API and API keys. **Tested only against Network 10.6.106.** The upstream project this started from recommended 9.5.21 or later; that has not been tried here, and the undocumented legacy fields (`stat/*`, `rest/*`, `v2/*`) can differ between versions and models. If something looks wrong on another version, run the command with `--verbose` and open an issue (with names, addresses and MACs removed).
- An API key from your controller (read-only access is sufficient, and recommended)

## Installation

```bash
git clone https://github.com/jeffholst/unifi-sentinel
cd unifi-sentinel
cp example.env .env
chmod 600 .env
```

### Configure

Edit `.env`: the controller's address and an API key are all you need (a self-signed certificate is the usual first hurdle; see `VERIFY_SSL`):

```env
CONTROLLER_URL=https://your-controller-ip:443
API_KEY=your-api-key-here
SITE_ID=default
VERIFY_SSL=true
```

Every setting, how the file is found (`--env-file`), TLS, timeouts, speed and the notification settings are in [docs/configuration.md](docs/configuration.md#configure).

### Seeing what the tool does: `--verbose`

`--verbose` (before the command) logs every request and what was read to stderr, never the API key. See [docs/configuration.md](docs/configuration.md#seeing-what-the-tool-does---verbose).

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

**A tagged release, without a clone:** each release is listed on the [releases page](https://github.com/jeffholst/unifi-sentinel/releases) with its notes and a wheel, and the [changelog](CHANGELOG.md) says what changed in every version and which parts of the interface scripts can rely on (exit codes, finding codes, JSON versions). The first release is `v0.2.0`; use the newest tag you find there in place of `vX.Y.Z`:

```bash
uv tool install git+https://github.com/jeffholst/unifi-sentinel@vX.Y.Z      # an isolated install with uv
pip install git+https://github.com/jeffholst/unifi-sentinel@vX.Y.Z          # or into a virtual environment
```

Either installs the `unifi-sentinel` command (its `--version` option shows which version you have). The `.env` file is still read from the directory you run it in (or `--env-file`), so put it there. `uv tool upgrade unifi-sentinel` does not move a pinned tag; install the newer tag instead.

## Usage

```bash
uv run unifi-sentinel.py info                                 # the controller's version and its sites
uv run unifi-sentinel.py --site Lab diagnose --no-events      # another site for this run (beats SITE_ID)
uv run unifi-sentinel.py export -o ./out --include-offline    # CSV files (long form: --output-dir); also previously seen clients
uv run unifi-sentinel.py query clients -s printer --json      # filter (long form: --search), as JSON; --csv for a spreadsheet
uv run unifi-sentinel.py query clients --include-offline      # also previously seen clients
uv run unifi-sentinel.py query reservations --offline         # reserved clients that have been offline for a day or more
uv run unifi-sentinel.py query ports --down --switch rack     # down ports on matching switches
uv run unifi-sentinel.py snapshot                             # save the inventory to ./snapshots/
uv run unifi-sentinel.py diff                                 # what changed since the newest snapshot?
uv run unifi-sentinel.py topology --clients                   # how the gateway, switches and APs are wired
uv run unifi-sentinel.py wifi --band 2.4 --ap hall            # radios and a channel plan from the neighbors
uv run unifi-sentinel.py wan --days 90                        # is it my internet or my LAN?
uv run unifi-sentinel.py firewall --all --zones               # policies, port forwards, zones and the matrix
uv run unifi-sentinel.py events --since 7d --severity high    # recent serious events
uv run unifi-sentinel.py client desktop                       # one client: attachment, link, findings
uv run unifi-sentinel.py new-clients                          # clients in no client group
uv run unifi-sentinel.py audit                                # settings that are probably not what you want
uv run unifi-sentinel.py diagnose                             # health checks (--json for scripts, --notify for alerts)
uv run unifi-sentinel.py diagnose --only ports,wifi           # just those checks, reading only what they need
```

Run these from the project root (uv uses `pyproject.toml`); after `pip install .` use `unifi-sentinel <command>` instead. Run `--help` on the tool or any command for options, and `--version` for the version. More examples, with sample output, are in [docs/examples.md](docs/examples.md).

### Diagnose

Read-only health checks with critical, warning and info findings, thresholds and an ignore list, `--json` with a stable code per finding, and `--only` / `--skip`. [Details](docs/diagnose.md#diagnose).

### Devices

`query devices` lists the UniFi devices with firmware, update availability and uptime. [Details](docs/inventory.md#devices).

### Snapshots and diff

`snapshot` saves the inventory to a file and `diff` shows what changed since. [Details](docs/inventory.md#snapshots-and-diff).

### Topology

`topology` draws the uplink tree from the gateway down, with ports, speeds, client counts and problems. [Details](docs/network.md#topology).

### Wi-Fi

`wifi` reports each AP's radios and a channel plan from the neighboring networks. [Details](docs/network.md#wi-fi).

### WAN

`wan` shows the internet connection's state, the controller's 24-hour monitoring and the speedtest history. [Details](docs/network.md#wan).

### Audit

`audit` reports settings that are probably not what you want: open or WPA2-only Wi-Fi, default names, firmware updates, unnamed clients. [Details](docs/diagnose.md#audit).

### Firewall

`firewall` lists your policies, the port forwards and the zone matrix, and what looks wrong. [Details](docs/network.md#firewall).

### Event history

`events` reads the controller's event log: disconnects, roams, IP conflicts and device outages. [Details](docs/network.md#event-history).

### Client view

`client` troubleshoots one client: where it attaches, its link quality and related findings. [Details](docs/network.md#client-view).

### New clients

`new-clients` lists the clients that are in no client group, to spot new devices. [Details](docs/inventory.md#new-clients).

### Randomized MAC addresses

Clients that use a private Wi-Fi MAC address are flagged in `query clients`, `new-clients`, `client` and `diagnose`. [Details](docs/inventory.md#randomized-mac-addresses).

### Switch ports

`query ports` lists every switch port, or only the down ones or those with errors. [Details](docs/inventory.md#switch-ports).

### DHCP reservations

`query reservations` lists the fixed IP reservations, including offline clients and `--offline` ones. [Details](docs/inventory.md#dhcp-reservations).

### Output files

`export` writes the inventory and one CSV per switch; names are cleaned and formula-like cells neutralized. [Details](docs/inventory.md#output-files).

### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success; for `diagnose`, no findings at or above the `--fail-on` threshold |
| 1 | `diagnose` or `audit` found at least one non-critical finding at or above the `--fail-on` threshold |
| 2 | `diagnose` found at least one critical finding (`audit` has none) |
| 3 | error: bad configuration, or the controller could not be reached or returned an error; also `diagnose --notify` when the findings gave 0 but the notification could not be delivered |
| 4 | `client` found no client, or several (it lists them) |
| 64 | command-line usage error |

`--fail-on {info,warning,critical}` sets the lowest severity that gives a non-zero code (default `warning`). Critical always exits 2. Example cron entry that only alerts on outages:

```bash
*/15 * * * * cd /path/to/unifi-sentinel && uv run unifi-sentinel.py diagnose --fail-on critical || notify-me
```

Event-based warnings (above) count towards exit code 1 like any other warning. `--json` does not change any exit code.

Tool errors used to exit 1 for every command; they now exit 3 so that 1 and 2 only ever mean findings.

## Documentation

Historical section links still work: <a id="controller-health"></a>[Controller health](docs/diagnose.md#controller-health) · <a id="port-health"></a>[Port health](docs/diagnose.md#port-health) · <a id="recent-events"></a>[Recent events](docs/diagnose.md#recent-events) · <a id="wi-fi-quality"></a>[Wi-Fi quality](docs/diagnose.md#wi-fi-quality) · <a id="configuration-thresholds-and-ignore-list"></a>[Thresholds and ignore list](docs/diagnose.md#configuration-thresholds-and-ignore-list) · <a id="json-output-and-finding-codes"></a>[JSON and finding codes](docs/diagnose.md#json-output-and-finding-codes) · <a id="notifications"></a>[Notifications](docs/notifications.md)
<a id="the-one-post-and-why-it-is-safe"></a>[The one POST](docs/network.md#the-one-post-and-why-it-is-safe) · <a id="names-in-exports-and-output"></a>[Names in exports](docs/inventory.md#names-in-exports-and-output) · <a id="example-output"></a>[Example output](docs/examples.md#example-output) · <a id="unificlientscsv"></a>[Client CSV](docs/examples.md#unificlientscsv) · <a id="switchswitch---dencsv"></a>[Switch CSV](docs/examples.md#switchoffice-switchcsv) · <a id="api-documentation"></a>[API documentation](docs/development.md#api-documentation).

- [Diagnose and audit](docs/diagnose.md): the checks, thresholds and ignore list, `--json` and the finding codes
- [Notifications](docs/notifications.md): ntfy, webhook and email
- [Inventory, queries and exports](docs/inventory.md): `query`, `export`, snapshots, reservations, ports
- [Network views](docs/network.md): topology, Wi-Fi, WAN, firewall, events, the client view
- [Configuration and troubleshooting](docs/configuration.md): every setting, `--verbose`, when something fails
- [JSON output schemas](docs/schemas.md): a versioned JSON Schema for every `--json` output, the snapshot file and the webhook payload
- [Examples](docs/examples.md): more commands and sample output
- [Features](docs/features.md) and [Development and API documentation](docs/development.md)
- [Changelog](CHANGELOG.md)

## Troubleshooting

See [docs/configuration.md](docs/configuration.md#troubleshooting) for the usual messages and what they mean.

## Development

Run `uv run pytest`, `uv run ruff check .` and `uv run mypy`. The layout, the checks and the tests that keep these docs honest are in [docs/development.md](docs/development.md#development); contributor and AI-assistant guidelines are in [CLAUDE.md](CLAUDE.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests are welcome. Work is tracked in [GitHub issues](https://github.com/jeffholst/unifi-sentinel/issues).

## Acknowledgments

- [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), the project this was forked from
- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) from Ubiquiti
- [uv](https://docs.astral.sh/uv/) for Python package management
