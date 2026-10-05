# Changelog

All notable changes to Homelab Probe are listed here, newest first. The format follows
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

## [Unreleased]

## [0.3.0] - 2026-10-05

### Added

- **`web-user`** (`hlp web-user add|list|set-role|disable|enable|delete|reset-password`): the accounts of the coming web interface, kept in an owner-only users file (`--data-dir`, the current directory by default) with scrypt password hashes that are upgraded at the next login, two roles (viewer and admin), protection of the last administrator, and an owner-only, size-rotated audit log file (`AUDIT_LOG_MAX_MB`, `AUDIT_LOG_FILES`). A password is read from a prompt or `--password-stdin`, never an argument. Standard library only. See [docs/web.md](docs/web.md).
- **`init`**: guided first-time setup. It asks for the controller's address, the API key (without echo; or one line on standard input with `--api-key-stdin`, never an argument), the site and whether to check the certificate, validates them with the rules every command uses, and writes `.env` (owner-only, atomic, merged into an existing one with a `.env.bak`), a commented `hlp.toml` if there is none and a private `snapshots/`. `--check` then reads the controller once. It contacts nothing otherwise. See [docs/configuration.md](docs/configuration.md#guided-setup-init).
- **`serve --allowed-host` and `--forwarded-allow-ips`** let the server be reached from other machines safely: `--host` accepts any address (listening on every address needs at least one allowed host), the `Host` header must be one of the loopback names, the bind address or an `--allowed-host` (never a wildcard), and `X-Forwarded-For` and `X-Forwarded-Proto` are believed only from the proxies named (`*` is refused). The server says when it is reachable over plain HTTP, `/api/v1/meta` now has `https` and `loopback`, and `docs/web.md` has the reverse-proxy and SSH-tunnel recipes.
- **The web server requires a login.** `POST /api/v1/auth/login` (JSON and an `Origin`) sets a session cookie (`HttpOnly`, `SameSite=Strict`, `Path=/`; `Secure` with the `__Host-` prefix over HTTPS); sessions are kept in memory with an idle timeout (`SESSION_IDLE_MINUTES`, 30) and an absolute lifetime (`SESSION_MAX_HOURS`, 12), and end when the user's password, role or disabled state changes. Every unsafe request needs the session's CSRF token and an `Origin` that matches the `Host`. Every route needs a login except `/`, `/healthz`, `/readyz` (now yes or no only), `/api/v1/meta` and the login itself, and a route that declares nothing needs one too. Failed logins are slowed down per address (up to 5 minutes) and per username (up to 30 seconds), never locked for good. The server refuses to start without an enabled administrator, `serve --demo` has a throwaway administrator whose password it prints once, proxy headers are no longer trusted, and the audit log records `auth.login`, `auth.login_failed`, `auth.throttled` and `auth.logout`. See [docs/web.md](docs/web.md).
- **The web server serves the reports** under `/api/v1/unifi/sites/{site}/` (`diagnose`, `audit`, `firewall`, `topology`, `wifi`, `wan`, `events`, `events/summary`, `clients`, `devices`, `networks`, `wlans`, `ports`, `reservations`, `new-clients`, `clients/{mac}`) and `/api/v1/unifi/sites`: each is the command's `--json` document with `generated_at` and `warnings`, or `{items, generated_at, warnings}` for the commands whose `--json` is a bare array; the options are query parameters and `refresh=true` reads the controller again. Errors are a code and a fixed sentence. `/api/v1/schemas` serves the JSON Schemas, which are now also shipped inside the wheel (`homelab_probe/schemas`). See [docs/web.md](docs/web.md).
- **`serve`**: the first stage of the web server, installed with the new `web` extra (`uv run --extra web hlp.py serve` from the project checkout, or `python -m pip install 'homelab-probe[web]'`; the command line needs no new dependency). It serves a few routes without data (`/healthz`, `/readyz`, `/api/v1/meta`, `/api/v1/platforms`, `/api/v1/openapi.json`) and binds a loopback address only, until login exists. A `Host` check, no CORS, a strict content-security policy on every response, GET only, and a request log that never holds the path asked for. See [docs/web.md](docs/web.md).
- The server reads the controller through a shared cache (30 s, one read for N simultaneous requests, an answer up to 10 minutes old is served with a warning when the controller cannot be read, failures remembered for 5 s, at most `UNIFI_PARALLEL_REQUESTS` reads at once), and `/readyz` now says whether the controller can be read (503 and a one-word reason when it cannot).
- **`serve` starts without settings: the first stage of the guided setup (#185).** When nothing is configured (neither `UNIFI_URL` nor `UNIFI_API_KEY`) the server no longer exits: it starts in a setup mode, prints a setup token once (or uses `HLP_SETUP_TOKEN`, at least 16 characters, from the environment only; once an administrator exists the token is not used and the routes need an administrator's session) and answers `503 not_configured` everywhere except `/healthz`, `/readyz` (503), `/api/v1/meta` (`needs_setup`, `setup_mode`) and `/api/v1/setup/{status,draft,certificate,connection,preview}`, which need the token in `X-Setup-Token`. The draft lives on the server (the key is never returned), wrong tokens are throttled and audited (`setup.token_failed`, `setup.draft_changed`, `setup.certificate_fetched`, `setup.connection_tested`, `setup.preview_run`), a controller's certificate can be fetched and pinned by its fingerprint, certificate checking is turned off only by typing a confirmation sentence, and the connection test and the preview use a short timeout, follow no redirects and refuse addresses no controller has (link-local including the cloud metadata address, unspecified, multicast, reserved, and public ones unless `serve --allow-public-controller`); the connection test also lists the controller's sites for a site picker. Settings that exist but are broken still fail loudly. `serve` now reads its `.env` from `--data-dir` when no `--env-file` or `HLP_ENV` names one (the same file as before by default, since the data directory is the current directory); a server with usable settings and no administrator still refuses to start. New option `serve --allow-public-controller`. The new `/api/v1/meta` key `setup_mode` is additive. See [docs/web.md](docs/web.md#first-run-setup-a-server-with-no-settings).
- **The guided setup can be finished (#185, part 3):** `POST /api/v1/setup/finish` writes the settings with the engine of `hlp init` (`.env`, `hlp.toml`, `snapshots/`, `certs/controller.pem` for a pinned certificate), creates the first administrator, reads the configuration again and starts the controller service **without a restart**. It needs a passing connection test of the same draft. `POST /api/v1/setup/notifications` is a dry run of the notification destinations in the draft (`notify` in `draft`; the values are never returned). When the settings cannot be saved here (the environment sets them, the settings file is named by `--env-file` or `HLP_ENV`, a read-only volume) nothing is written and the answer is the finished `.env` and a compose snippet with a placeholder where the API key goes. The connection test now also lists the controller's sites. **Changed:** a server with settings but no enabled administrator no longer exits with code 3: it starts in an admin mode where the setup token may create the first administrator (and nothing else); `hlp web-user add` and a restart still work. New audit entries `setup.notifications_checked`, `setup.finish_fallback` and `setup.finished`. See [docs/web.md](docs/web.md#first-run-setup-a-server-with-no-settings).
- `hlp doctor`'s `config.env_contents` warns about `HLP_SETUP_TOKEN` in a `.env` file (it is read from the environment only).
- New log events server.start, server.request and server.cache.
- A new log event, audit.event, and the settings `AUDIT_LOG_MAX_MB` and `AUDIT_LOG_FILES`.

### Changed

- **Snapshots and the notification state are kept per site (#140).** `snapshot` now saves into `snapshots/<site id>/` and `diagnose --notify` remembers findings in `snapshots/<site id>/notify-state.json` (the site's UUID as the controller reports it), so alternating `--site` runs no longer announce false recoveries and `diff` never picks another site's snapshot. `diff` without `--dir` picks the newest snapshot **of this site**, and `--keep` counts only this site's. `--dir` and `--notify-state` are used exactly as given. Files from earlier versions are still read and never moved: snapshots saved straight into `snapshots/` belong to the site their record names, and the shared `snapshots/notify-state.json` is read for the site `default` while it has no file of its own. A state file now records its `site` (additive; the state `version` stays 1) and a state of another site is refused with exit code 3. **Note:** finding a site's directory asks the controller which site is meant, so `diff` with no file paths and `diff --last-two` now make one request (give `--dir DIR` to work without the controller, as `diff OLD.json NEW.json` already does).

### Fixed

- `docs/web.md` describes what the web server does today (login, report API, first-run setup API) instead of "a few routes without data".
- Web account reads tighten existing `users.json` permissions, authentication revalidates account status and role under the file lock, and failed audit writes are reported with the account change rolled back.

## [0.2.0] - 2026-10-04

The first tagged release. Everything since the fork is listed below.

**Tested against one controller (UniFi Network 10.6.106) only.** Most of the endpoints used are undocumented and
vary by version and by hardware. Where a field could not be verified there (the port forward fields, the `open`
and `wep` Wi-Fi security values) the README says so.

### Added

- **`--demo`** (before the command): runs any command on the packaged synthetic network, with no `.env`, environment variable or implicit settings file read, no controller contacted and nothing sent (an explicitly named `--config` is still used; `--notify`, `doctor`, `snapshot` and `diff` are refused with exit code 64). See [docs/configuration.md](docs/configuration.md#trying-it-without-a-controller---demo).
- **Commands:** `export --include-offline` (also the previously seen clients) and `export --format json` (the same data as
  one `unifi_inventory.json`, with a JSON Schema), `query` (devices, clients, DHCP reservations, switch ports, networks and Wi-Fi networks as a table, `--json` or
  `--csv`; `query clients --network`, `--ssid` and `--ap` find the clients on one network, SSID or access point),
  `client` (one client: where it attaches through the whole uplink chain, link quality, addressing, recent events
  and findings), `new-clients` (clients in no client group), `topology` (the uplink tree with ports, negotiated
  speeds, client counts and flagged devices), `wifi` (radios and a channel plan from the neighboring networks),
  `wan` (internet health, the controller's 24-hour monitoring, speedtest history, NAT in front of the gateway),
  `events` (event history with filters and a summary), `firewall` (policies, port forwards and the zone matrix of
  the zone-based firewall, with findings), `audit` (configuration findings: open, WEP and WPA2-only Wi-Fi, guest
  networks without client isolation, default device names, firmware updates, unnamed clients), `snapshot` and `diff`
  (save the inventory and see exactly what changed), `completion` (shell completion scripts for bash, zsh and fish, generated from the parser), `doctor` (checks the
  installation, the settings and that the controller answers, with stable check ids and no secrets or addresses in its
  output), `diagnose` and `info`.
- **`diagnose` checks:** offline devices (critical for a gateway or a device others uplink through), a device that reports it is
  overheating (`device.overheating`, critical), CPU and memory, a device's storage nearly full (`device.storage`, the
  gateway lists it; `storage_warn_pct` and `storage_critical_pct`), a device that restarted recently
  (`device.recent_reboot`, information; `recent_reboot_minutes`),
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
- **Settings file** (`hlp.toml`): thresholds for every check and an ignore list. A rule matches a finding
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
- **`--parallel N` / `UNIFI_PARALLEL_REQUESTS`** (default 6), `--site NAME|REF|UUID` (beats `UNIFI_SITE_ID`) and `--timeout` / `UNIFI_TIMEOUT`, `--env-file` /
  `HLP_ENV`, `UNIFI_VERIFY_SSL` as a CA bundle path.
- **Development:** a contract table of the fields the code reads (`tests/contract.py`), opt-in live contract tests
  (`pytest -m live`, GET only), a fixture recorder with a deterministic sanitiser and leak check (`tools/`), golden
  files and a docs-drift test, a 100% line and branch coverage requirement (checked in CI), Dependabot for the GitHub Actions and
  `uv.lock`, and CI on Python 3.10 to 3.13 with `ruff` and `mypy`. `SECURITY.md` (how to report a vulnerability
  privately, what is in scope), `CONTRIBUTING.md` and GitHub issue forms for a bug report and a feature request, which
  ask the reporter to redact addresses, names and keys; `tests/test_community_files.py` keeps them honest.
- **`doctor` checks what is inside the `.env` file** (new check id `config.env_contents`): a setting listed on several lines (the last one is used), a misspelled name (with a suggestion), a line that cannot be read, an empty setting, and a variable already set in the environment with another value (the environment wins). It reports names and line numbers only, never a value, and is a warning, so the exit code does not change.

### Changed

- **The project is now Homelab Probe, and the command is `hlp`** (it was "UniFi Sentinel" and `unifi-sentinel`). UniFi is the primary and, for now, only supported platform; the name leaves room for others. Nothing was released under the old name, so there is **no alias and the old names are not read**:
  - the distribution is `homelab-probe`, the Python package `homelab_probe`, the launcher `hlp.py`, the installed command `hlp`;
  - the settings file is `hlp.toml` (example: `hlp.example.toml`) and the variable that names the `.env` file is `HLP_ENV` (they were `unifi-sentinel.toml` and `UNIFI_SENTINEL_ENV`);
  - the UniFi connection settings carry a `UNIFI_` prefix: `UNIFI_URL`, `UNIFI_API_KEY`, `UNIFI_SITE_ID`, `UNIFI_VERIFY_SSL`, `UNIFI_TIMEOUT` and `UNIFI_PARALLEL_REQUESTS` (they were `CONTROLLER_URL`, `API_KEY`, `SITE_ID`, `VERIFY_SSL`, `TIMEOUT` and `PARALLEL_REQUESTS`); `ALLOW_INSECURE_HTTP`, `NOTIFY_*` and `LOG_*` are shared and keep their names;
  - **webhook payload:** `source` is now `"homelab-probe"` (was `"unifi-sentinel"`; the JSON `version` stays 1), and notification and email titles start with `hlp:`;
  - log records: the `logger` field is `homelab_probe.*`;
  - the repository is `jeffholst/homelab-probe`, and the JSON Schema `$id`s point there.
- **Logging foundation (stderr only):** the tool's diagnostics now go through one logger (`homelab_probe`, standard library) with a redaction filter. Nothing changes by default: `Warning: ...` lines and `--verbose` `[verbose] ...` lines are byte-for-byte what they were. New settings `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) and `LOG_FORMAT` (`text` or `json`, one record per line with `ts`, `level`, `logger`, `msg`, `event`, `request_id`, `user`, `site` and the event's fields); a run has a `request_id` that also reaches the threads of a parallel read. Secrets (the API key, notification URLs, tokens and the mail account) and the values of `Authorization`/`Cookie` headers never reach a record; INFO and above never carry client names, MACs or addresses. `notify.delivery` and `watch.pass`/`watch.unavailable` are new records. See [docs/logging.md](docs/logging.md).
- **The tool talks to the Integration API first** (`/proxy/network/integration/v1`). The legacy endpoints are used
  only for data it lacks (per-port counters, client-to-port mapping, reservations, network and Wi-Fi settings,
  health), and the zone-based firewall comes from the v2 endpoints.
- **Layout:** the script is now a thin launcher over the `homelab_probe` package (`hlp.py`, or the
  `hlp` command after `pip install .`), commands are one registry, each command declares what it reads,
  and `diagnose` is a package with a module per topic. None of this changes the command line.
- **Reads run side by side** (up to 6 requests at once) and a client lookup reads far less; analysis is linear in
  the number of clients. Output and the order of warnings are the same as when reading one by one (`--parallel 1`).
- **Minimum Python is 3.10** (3.9 is no longer supported).
- **Errors exit with code 3** (earlier untagged versions exited 1 for every error), so `1` and `2` only ever mean findings.
- **`.env` is read from the current directory** (or `--env-file`, or `HLP_ENV`), never from the package
  directory or a parent directory. A tool installed with `pip install .` now finds the `.env` the README describes.
- **Snapshot file names are in UTC** (`snapshot-YYYYMMDD-HHMMSSZ.json`); older local-time names are still read and
  ordered correctly.
- **`--show-ignored` lists each finding's code**, so it can be copied into an ignore rule.
- The version is written in one place, `homelab_probe.__version__`.
- **Documentation:** the README is now a short quickstart (what it is, install, configure, the commands with one example each, the exit codes) and the detail is in `docs/` (diagnose and audit, notifications, inventory, network views, configuration and troubleshooting, running on a schedule, examples, features, development). Historical README anchors remain at their original URLs, and output samples are generated from the checked-in fixture.
- **Every command except `completion` builds a document first** (the dict that `--json` prints, or a bare array where it always was one, plus the warnings of the read), and the text, CSV and detail views, the exit code, `--notify` and `--watch` are rendered or derived from it, so that a later API cannot drift from the command line. Nothing changes for a user: the output, the snapshot file format (`schema_version` 1), the notification state files and the exit codes are the same.

### Fixed

- TLS failures with `UNIFI_VERIFY_SSL` pointing at a certificate file now distinguish a missing signing CA, a
  certificate that is not usable as a CA bundle, and a host-name/SAN mismatch, instead of always saying the
  certificate was not signed by the bundle.
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
- Warnings say what was really skipped when an optional read fails, including recent-reboot detection when
  `stat/device` is unavailable, and a command whose answer would be wrong without the client history
  (`new-clients`, `snapshot`, `diff`) fails instead of printing a misleading result.
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
