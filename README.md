# Homelab Probe
![Homelab Probe](docs/images/homelab-probe.png)

Homelab Probe is a read-only UniFi Network probe for inventory, troubleshooting, snapshots and diagnostics. Use it from the `hlp` command, or run the login-protected web API with `hlp serve` for browser/API clients (it also serves the built web app when the install has one, see [docs/web.md](docs/web.md#the-web-app-files); it listens on this machine only by default; the server options can expose it to other machines on purpose, see [docs/web.md](docs/web.md)). It never changes the controller: requests are GETs except the controller's event-history endpoint, which requires a read-only POST. The only data the tool sends anywhere else is opt-in notifications from `diagnose --notify`; the web API shows controller data to whoever you give a login.

> **Status: early development.** Tested against one live controller (Network 10.6.106); other versions and hardware may differ. See [open issues](https://github.com/jeffholst/homelab-probe/issues) for the roadmap.

## Commands

| Command | Description | |
| ------- | ----------- | --- |
| `audit` | Report risky settings such as open Wi-Fi, default names and updates | [details](docs/diagnose.md#audit) |
| `client` | Troubleshoot one client by name, MAC or IP | [details](docs/network.md#client-view) |
| `completion` | Print shell completion for bash, zsh or fish | [details](docs/configuration.md#shell-completion) |
| `diagnose` | Run health checks, optionally as JSON or notifications | [details](docs/diagnose.md#diagnose) |
| `diff` | Compare snapshots, or a snapshot against the live network | [details](docs/inventory.md#snapshots-and-diff) |
| `doctor` | Check local setup and controller access | [details](docs/configuration.md#checking-your-setup-doctor) |
| `events` | Read controller events: disconnects, roams, IP conflicts and outages | [details](docs/network.md#event-history) |
| `export` | Export clients, UniFi devices and switch ports to CSV or JSON | [details](docs/inventory.md#output-files) |
| `firewall` | Show firewall policies, port forwards, zones and suspicious rules | [details](docs/network.md#firewall) |
| `info` | Show controller application info and sites | [details](docs/configuration.md#finding-your-site-info) |
| `init` | Guided first-time setup for `.env`, `hlp.toml` and snapshots | [details](docs/configuration.md#guided-setup-init) |
| `new-clients` | List new clients: first seen recently (7 days), or in no client group | [details](docs/inventory.md#new-clients) |
| `query` | List and filter devices, clients, reservations, ports, networks and Wi-Fi networks | [details](docs/inventory.md#devices) |
| `serve` | Run the login-protected web API, and the built web app when the install has one (needs the `web` extra) | [details](docs/web.md#running-the-server-serve) |
| `snapshot` | Save the current inventory to compare later | [details](docs/inventory.md#snapshots-and-diff) |
| `topology` | Draw the uplink tree, ports, speeds, client counts and problems (or a Mermaid or DOT graph) | [details](docs/network.md#topology) |
| `wan` | Show internet state, monitoring and speedtest history | [details](docs/network.md#wan) |
| `web-user` | Manage web/API users, roles and passwords | [details](docs/web.md#managing-accounts-web-user) |
| `wifi` | Report AP radios and nearby-channel planning | [details](docs/network.md#wi-fi) |

## Features

- Inventory, exports and snapshots for clients, UniFi devices, switch ports, networks, Wi-Fi networks and DHCP reservations
- Troubleshooting views for health findings, one client, topology, Wi-Fi, WAN, events, firewall rules and audits
- Stable JSON output and schemas for automation, plus exit codes, finding codes and shell completion
- Optional ntfy, webhook or email notifications when findings are new, worse or fixed
- Login-protected local web API with roles, CSRF protection, settings editing, snapshots and scheduler support
- Secure terminal API and gated browser panel: authenticated capabilities, static completion and bounded read-only execution; production enablement awaits CSP approval and final browser verification ([contracts, engine and testing](docs/web.md#the-terminal-api))
- A Docker image (and compose file) for the web server and the command line, with a read-only root file system ([docs/docker.md](docs/docker.md))

Full feature notes are in [docs/features.md](docs/features.md).

## Requirements

- Python 3.10 or higher
- A UniFi Network Application recent enough to support the Integration API and API keys. **Tested only against Network 10.6.106.** The upstream project this started from recommended 9.5.21 or later; that has not been tried here, and undocumented legacy fields can differ between versions and models.
- A controller API key; read-only access is sufficient and recommended

## Installation

| Path | What you get now | Instructions |
| ---- | ---------------- | ------------ |
| CLI from a checkout or tag | Command line; no Node required | Steps below |
| Web UI development | Browser UI with a development proxy to the Python server; requires Node 22+ and npm | [Development](docs/development.md#the-web-interface-web) |
| Native production web UI | Build and copy the browser bundle, then serve it with Python; Node is needed only for the build | [Build and serve](docs/web.md#building-and-serving-the-browser-ui) |
| Docker / Compose | Locally built CLI and web API; the image does not yet include the browser UI and is not yet published | [Docker](docs/docker.md) |

```bash
git clone https://github.com/jeffholst/homelab-probe
cd homelab-probe
cp example.env .env
chmod 600 .env
```

### Configure

Edit `.env`:

```env
UNIFI_URL=https://your-controller-ip:443
UNIFI_API_KEY=your-api-key-here
UNIFI_SITE_ID=default
UNIFI_VERIFY_SSL=true
```

Create the API key in UniFi Network under **Settings > Control Plane > Integrations**. Every setting, including `--env-file`, TLS, timeouts, logging and notifications, is in [docs/configuration.md](docs/configuration.md#configure). `--verbose` logs requests and reads to stderr, never the API key ([details](docs/configuration.md#seeing-what-the-tool-does---verbose); structured logging: [docs/logging.md](docs/logging.md)).

### Install Dependencies

With [uv](https://docs.astral.sh/uv/) (recommended), dependencies are resolved on first run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

With pip:

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install .
```

This installs `hlp`. `hlp serve` also needs the web extra: `pip install '.[web]'` (or `uv run --extra web hlp.py serve`). The optional `pretty` extra (`pip install '.[pretty]'`) enables compact site headings, progress, grouped findings and responsive inventory tables; `--color`, `--no-progress` and `--plain` control it ([details](docs/configuration.md#terminal-styling---color---plain-and---no-progress)). Findings stay plain with `--no-emoji` or `diagnose --watch`. A tagged release can be installed without a clone; check the [releases page](https://github.com/jeffholst/homelab-probe/releases) and [changelog](CHANGELOG.md), then replace `vX.Y.Z`:

With `uv tool`, which puts `hlp` on your PATH in its own environment (leave out the extras you do not want):

```bash
uv tool install '.[web,pretty]'                          # from a checkout
uv tool install --editable '.[web,pretty]'               # the same, and code changes take effect at once
uv tool install --force --reinstall '.[web,pretty]'      # pick up changes to a normal install
uv tool update-shell                                     # if hlp is not found: add uv's tool folder to PATH
uv tool uninstall homelab-probe
```

From a release tag (add the extras with the last form):

```bash
uv tool install git+https://github.com/jeffholst/homelab-probe@vX.Y.Z
pip install git+https://github.com/jeffholst/homelab-probe@vX.Y.Z
uv tool install 'homelab-probe[web,pretty] @ git+https://github.com/jeffholst/homelab-probe@vX.Y.Z'
```

The built web app is not in git, so an install from a checkout serves the API only until a build is copied into the package ([how](docs/web.md#the-web-app-files)).

### Docker

One image runs the command line and the web server (not published yet: build it from a checkout). It starts with no configuration, in the setup mode, and prints a one-time setup token in its log ([details](docs/docker.md)):

```bash
docker build -t homelab-probe .
docker run -d --name hlp -p 127.0.0.1:8787:8787 -v hlp-data:/data homelab-probe && docker logs hlp
docker run --rm homelab-probe --demo diagnose
```

## Usage

```bash
uv run hlp.py audit                                # risky settings
uv run hlp.py client desktop                       # attachment, link and findings
uv run hlp.py completion zsh                       # shell completion
uv run hlp.py --demo diagnose                      # synthetic data, no controller
uv run hlp.py diagnose                             # health checks
uv run hlp.py diff                                 # compare with the newest snapshot
uv run hlp.py doctor                               # setup check
uv run hlp.py events --since 7d --severity high    # recent serious events
uv run hlp.py export -o ./out --include-offline    # CSV files; --output-dir is the long form, or --format json
uv run hlp.py firewall --all --zones               # policies, forwards and zones
uv run hlp.py info                                 # controller version and sites
uv run hlp.py init                                 # guided local setup
uv run hlp.py new-clients --since 24h               # clients first seen in the last day
uv run hlp.py new-clients --ungrouped                # clients in no client group
uv run hlp.py query clients -s printer --json      # filter clients; --search is the long form
uv run --extra web hlp.py serve                    # local web API (and web app, if built in) on 127.0.0.1:8787
uv run hlp.py snapshot                             # save inventory to ./snapshots/
uv run hlp.py topology --format mermaid --clients  # a Mermaid graph (or dot) with the wired clients
uv run hlp.py wan --days 90                        # internet health
uv run hlp.py web-user list                        # web/API accounts
uv run hlp.py wifi --band 2.4 --ap hall            # radios and neighboring channels
```

Run these from the project root. After `pip install .`, use `hlp <command>` instead. More examples and sample output are in [docs/examples.md](docs/examples.md).

### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success; for `diagnose`, no findings at or above `--fail-on` |
| 1 | `diagnose` or `audit` found a non-critical finding at or above `--fail-on` |
| 2 | `diagnose` found a critical finding |
| 3 | configuration, controller or notification-delivery error |
| 4 | `client` found no client, or several |
| 64 | usage error |

`--fail-on {info,warning,critical}` sets the lowest severity that gives a non-zero code. Critical findings always exit 2. Event-based warnings count like other warnings. `--json` does not change exit codes. Scheduling examples are in [docs/scheduling.md](docs/scheduling.md).

## Documentation

Start with [Configuration and troubleshooting](docs/configuration.md), [Diagnose and audit](docs/diagnose.md), [Inventory, queries and exports](docs/inventory.md), [Network views](docs/network.md), [Web interface and API](docs/web.md), [Notifications](docs/notifications.md), [Running on a schedule](docs/scheduling.md), [Docker](docs/docker.md), [JSON output schemas](docs/schemas.md), [Examples](docs/examples.md), [Features](docs/features.md), [Development and API documentation](docs/development.md), [Changelog](CHANGELOG.md) and [Maintainer guide](MAINTAINING.md).

Old README anchors still land here:
<a id="credits"></a><a id="getting-an-api-key"></a><a id="diagnose"></a><a id="controller-health"></a><a id="port-health"></a><a id="recent-events"></a><a id="wi-fi-quality"></a><a id="configuration-thresholds-and-ignore-list"></a><a id="json-output-and-finding-codes"></a><a id="notifications"></a><a id="the-one-post-and-why-it-is-safe"></a><a id="names-in-exports-and-output"></a><a id="example-output"></a><a id="unificlientscsv"></a><a id="switchswitch---dencsv"></a><a id="api-documentation"></a><a id="seeing-what-the-tool-does---verbose"></a><a id="devices"></a><a id="snapshots-and-diff"></a><a id="topology"></a><a id="wi-fi"></a><a id="wan"></a><a id="audit"></a><a id="firewall"></a><a id="event-history"></a><a id="client-view"></a><a id="new-clients"></a><a id="randomized-mac-addresses"></a><a id="switch-ports"></a><a id="dhcp-reservations"></a><a id="output-files"></a><a id="acknowledgments"></a>

## Troubleshooting

See [docs/configuration.md](docs/configuration.md#troubleshooting) for common messages and fixes.

## Development

Use the [routine checks](MAINTAINING.md#routine-checks) to install the locked development dependencies with both `web` and `pretty` extras and reproduce the full local checks. Project layout and documentation checks are in [docs/development.md](docs/development.md#development); contributor and AI-assistant guidelines are in [AGENTS.md](AGENTS.md), with the [module map](docs/architecture.md), [UniFi API notes](docs/unifi-api-notes.md) and [subsystem rules](docs/agent-reference.md) behind it.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests are welcome; read [CONTRIBUTING.md](CONTRIBUTING.md) first. Report security problems privately, as [SECURITY.md](SECURITY.md) describes. Work is tracked in [GitHub issues](https://github.com/jeffholst/homelab-probe/issues).

## Acknowledgments

- [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), the project this was forked from
- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) from Ubiquiti
- [uv](https://docs.astral.sh/uv/) for Python package management
