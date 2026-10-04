# Changelog

All notable changes to UniFi Sentinel are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
[Semantic Versioning](https://semver.org/) as far as a 0.x project can: while the major version is 0, a minor
version may change behavior, and anything that does is listed under **Changed**.

This project is a fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export). The
`export` command and its CSV layout come from that project.

## What scripts can rely on

These are the parts of the interface that scripts, cron jobs and dashboards depend on. A change to any of them is
always listed here.

- **Exit codes:** `0` success (for `diagnose` and `audit`: no finding at or above `--fail-on`); `1` a warning (or
  `--fail-on info` finding); `2` a critical finding (`diagnose` only); `3` a configuration, connection, controller, or
  file read/write error (also `diagnose --notify` when the findings gave `0` but no message could be delivered); `4`
  `client` found no single match; `64` a command-line usage error. `1` and `2` are never used for errors.
- **Finding codes** (`device.offline`, `port.slow_link`, `audit.wifi_open`, ...) are never renamed or reused; new
  ones are added. They are what `--json` output and ignore rules (`[[ignore]] code = ...`) use.
- **JSON documents** carry a `version` as their first key (every `--json` output that is an object, and the webhook
  payload, are version `1`; the plain lists of `query`, `new-clients` and `events` are bare arrays and have none),
  and each has a JSON Schema in `docs/schemas/`. It changes when a field is removed, renamed or its meaning changes; new fields may appear without a new version.
- **Saved snapshots** (`snapshot`, `diff`) have `schema_version` `1`; a file of another version is refused with a
  clear message, never misread.
- The package never changes anything on the controller. Every request is a GET, with one read-only exception: the
  event log can only be queried with a POST (`events`, and `diagnose` and `client` unless `--no-events`).

## [0.2.0] - Unreleased

The first tagged release. Everything since the fork is listed below.

**Tested against one controller (UniFi Network 10.6.106) only.** Most of the endpoints used are undocumented and
vary by version and by hardware. Where a field could not be verified there (the port forward fields, the `open`
and `wep` Wi-Fi security values) the README says so.

### Added

- **Commands:** `export --include-offline` (also the previously seen clients), `query` (devices, clients, DHCP reservations, switch ports, networks and Wi-Fi networks as a table, `--json` or
  `--csv`; `query clients --network`, `--ssid` and `--ap` find the clients on one network, SSID or access point),
  `client` (one client: where it attaches through the whole uplink chain, link quality, addressing, recent events
  and findings), `new-clients` (clients in no client group), `topology` (the uplink tree with ports, negotiated
  speeds, client counts and flagged devices), `wifi` (radios and a channel plan from the neighboring networks),
  `wan` (internet health, the controller's 24-hour monitoring, speedtest history, NAT in front of the gateway),
  `events` (event history with filters and a summary), `firewall` (policies, port forwards and the zone matrix of
  the zone-based firewall, with findings), `audit` (configuration findings: open, WEP and WPA2-only Wi-Fi, guest
  networks without client isolation, default device names, firmware updates, unnamed clients), `snapshot` and `diff`
  (save the inventory and see exactly what changed), `completion` (shell completion scripts for bash, zsh and fish, generated from the parser), `diagnose` and `info`.
- **`diagnose` checks:** offline devices (critical for a gateway or a device others uplink through), a device that reports it is
  overheating (`device.overheating`, critical), CPU and memory,
  controller health subsystems, internet latency, drops, availability and speedtest drops, double NAT and
  carrier-grade NAT, clients without an IP or with a link-local one, duplicate IPs, DHCP reservations (mismatch,
  outside the subnet, duplicate, in use by another device, inside the DHCP pool, offline too long, never seen,
  randomized MAC), switch ports (errors, drops as a share of packets, flapping links, STP, half duplex, slow links,
  PoE budget, uplinks negotiated below what both ends support), Wi-Fi quality (signal, retries, satisfaction, radio
  utilization) and event-log checks (IP conflicts, repeated disconnects, roaming, unreachable devices, internet
  latency).
- **Severities, exit codes and options for `diagnose`:** `critical`, `warning` and `info`, with `--fail-on`;
  `--watch SECONDS` to repeat the checks and print only what changed (new, worse, fixed) until Ctrl-C;
  `--json` with a stable code per finding; `--only` and `--skip` to run a subset of the checks (by area: `devices`,
  `health`, `wan`, `clients`, `reservations`, `ports`, `wifi`, `events`), reading only the data those checks need;
  `--since`, `--no-events`, `--show-ignored`, `--no-emoji`.
- **Settings file** (`unifi-sentinel.toml`): thresholds for every check and an ignore list. A rule matches a finding
  by `code` (exact), `subject` (case-insensitive, wildcards), `message` (substring) or any combination, with a
  required `reason`; an unknown code is an error that suggests the closest one. A rule may carry `until = 2026-12-31`,
  the last day it applies: after that the findings come back, `diagnose` and `audit` warn on stderr about the expired
  rule, and `--show-ignored` and `--json` show the date (a new optional `until` on the `ignored` entries).
- **Notifications** (`diagnose --notify`): ntfy, a generic webhook and email (SMTP). A message is sent only when a
  finding is new, got worse, is a critical one still unresolved after a day, or is fixed; the state is kept in an
  owner-only file. Options: `--notify-min`, `--notify-redact` (no names, addresses or MACs), `--notify-dry-run`,
  `--notify-baseline`, `--notify-state`. A partial `--only`/`--skip` run never announces the recovery of areas it
  did not check. Email is TLS only, one plain-text message per run.
- **JSON Schemas** (`docs/schemas/*.v1.schema.json`, draft 2020-12) for every `--json` output, the snapshot file and the
  webhook payload, checked by the tests against real output, plus a `version` field in `topology`, `wifi`, `wan`,
  `client`, `diff` and `events --summary` documents (additive).
- **`--verbose` / `--debug`:** every request (method, path, status, milliseconds, retries) and what was read, on
  stderr, never the API key.
- **`--parallel N` / `PARALLEL_REQUESTS`** (default 6), `--site NAME|REF|UUID` (beats `SITE_ID`) and `--timeout` / `TIMEOUT`, `--env-file` /
  `UNIFI_SENTINEL_ENV`, `VERIFY_SSL` as a CA bundle path.
- **Development:** a contract table of the fields the code reads (`tests/contract.py`), opt-in live contract tests
  (`pytest -m live`, GET only), a fixture recorder with a deterministic sanitiser and leak check (`tools/`), golden
  files and a docs-drift test, a 100% line and branch coverage requirement (checked in CI), Dependabot for the GitHub Actions and
  `uv.lock`, and CI on Python 3.10 to 3.13 with `ruff` and `mypy`. `SECURITY.md` (how to report a vulnerability
  privately, what is in scope), `CONTRIBUTING.md` and GitHub issue forms for a bug report and a feature request, which
  ask the reporter to redact addresses, names and keys; `tests/test_community_files.py` keeps them honest.

### Changed

- **The tool talks to the Integration API first** (`/proxy/network/integration/v1`). The legacy endpoints are used
  only for data it lacks (per-port counters, client-to-port mapping, reservations, network and Wi-Fi settings,
  health), and the zone-based firewall comes from the v2 endpoints.
- **Layout:** the script is now a thin launcher over the `unifi_sentinel` package (`unifi-sentinel.py`, or the
  `unifi-sentinel` command after `pip install .`), commands are one registry, each command declares what it reads,
  and `diagnose` is a package with a module per topic. None of this changes the command line.
- **Reads run side by side** (up to 6 requests at once) and a client lookup reads far less; analysis is linear in
  the number of clients. Output and the order of warnings are the same as when reading one by one (`--parallel 1`).
- **Minimum Python is 3.10** (3.9 is no longer supported).
- **Errors exit with code 3** (earlier untagged versions exited 1 for every error), so `1` and `2` only ever mean findings.
- **`.env` is read from the current directory** (or `--env-file`, or `UNIFI_SENTINEL_ENV`), never from the package
  directory or a parent directory. A tool installed with `pip install .` now finds the `.env` the README describes.
- **Snapshot file names are in UTC** (`snapshot-YYYYMMDD-HHMMSSZ.json`); older local-time names are still read and
  ordered correctly.
- **`--show-ignored` lists each finding's code**, so it can be copied into an ignore rule.
- The version is written in one place, `unifi_sentinel.__version__`.
- **Documentation:** the README is now a short quickstart (what it is, install, configure, the commands with one example each, the exit codes) and the detail is in `docs/` (diagnose and audit, notifications, inventory, network views, configuration and troubleshooting, running on a schedule, examples, features, development). Historical README anchors remain at their original URLs, and output samples are generated from the checked-in fixture.

### Fixed

- Empty network inventories are now reported as zero rows, and client filters and WLAN/network client counts use
  explicit `stat/sta` availability instead of inferring failures from empty results or unrelated degraded reads.
- Shell completions now dispatch correctly in zsh, stop offering consumed positional choices, and complete later items in fish comma lists.
- An incomplete optional-data pass in **`diagnose --watch`** no longer reports findings as recovered or resets the watch baseline.
- Notification setup errors link to the online guide, including for installed users outside a source checkout.
- Switch ports are matched on switches that report no `mac_table_count`, and offline devices keep their last uplink.
- A device's type is detected from its model when the legacy type is unavailable.
- Correct handling of 2.4 GHz channel 14 and the U-NII-4 channels (165 to 177) in the Wi-Fi channel plan.
- MAC addresses are compared in any spelling (`aa:bb:...`, `AA-BB-...`, `aabb.ccdd.eeff`, no separators); several
  checks used to treat two spellings of one address as different devices.
- Warnings say what was really skipped when an optional read fails, and a command whose answer would be wrong
  without the client history (`new-clients`, `snapshot`, `diff`) fails instead of printing a misleading result.
- A file that cannot be read or written ends a command with `ERROR: <reason> (<path>)` and exit code 3, not a
  traceback.

### Security

- **Read-only by construction:** a test enforces that the controller client only issues GET requests and the one
  approved event-log query, and that the notification code never touches the controller client.
- **Output safety:** names that come from devices on your network are cleaned of control characters, line breaks,
  text-direction overrides and invisible characters before they are printed; exported CSV cells that a spreadsheet
  would run as a formula (`=`, `+`, `-`, `@`) get a leading apostrophe (`export` and `query --csv`).
- **Credentials:** the API key is never printed (it is stripped from response bodies and error text); a `.env` that
  other users can read gives a warning; an `http://` controller URL is refused unless `ALLOW_INSECURE_HTTP` opts in;
  notification URLs, tokens and the mail account are secrets that never appear in a message, error or log line.
- **Saved snapshots** are written owner-only into the git-ignored `snapshots/` and hold real MACs and IPs, so
  treat them like the `.env`.
- **Email** (SMTP) uses STARTTLS or implicit TLS with the certificate and host name verified, never sends a
  password without encryption, and its failures print a fixed reason, never the host, user or password.
