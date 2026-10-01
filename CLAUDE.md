# UniFi Sentinel

Fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), being extended into a tool for querying, troubleshooting and inventorying a UniFi Network controller. Keep the Apache-2.0 license and upstream credit.

## Layout

- `unifi-sentinel.py`: thin launcher; all logic lives in `unifi_sentinel/`.
- `unifi_sentinel/config.py`: env/`.env` loading. `client.py`: `UniFiClient`, the only place that makes HTTP calls. `snapshot.py`: `collect_snapshot`, the one read of the controller. `export.py`, `query.py`, `reservations.py`, `new_clients.py`, `client_view.py`, `events.py`, `topology.py`, `history.py`, `diagnose.py`: pure functions over a `Snapshot`. `settings.py`: `diagnose` thresholds and ignore rules from an optional TOML file; new checks take their thresholds from `DiagnoseSettings`, never module constants. `cli.py`: argparse subcommands.
- New features are new subcommands in `cli.py` backed by modules that take a `Snapshot`; keep fetching (snapshot), analysis and output separate.

## Commands

- Run: `uv run unifi-sentinel.py <export|query|client|new-clients|diagnose|events|topology|snapshot|diff|info>`
- Tests: `uv run pytest`

## Documentation

- Every new feature must be documented in `README.md` in the same PR: a row in the Commands table, a usage example, a Features bullet, a section for anything non-obvious (columns, filters, data sources, caveats), the layout block if you add a module, and a `--help`-accurate list of flags. Changed behavior (flags, output columns, exit codes) updates the existing text too.
- Keep `CLAUDE.md` current when adding a module or subcommand (Layout, Commands) or learning something non-obvious about the API.
- Example output in docs must come from the synthetic fixture, never from a real network.
- Before opening a PR, check that the README matches `unifi-sentinel <command> --help`.

## Conventions

- Stdlib `csv`, not pandas. Match the surrounding style; type-hint public functions.
- Raise `UniFiAPIError`/`ConfigError`; only `cli.main` turns them into messages and exit codes.
- Exit codes: 0 success, 1 and 2 are reserved for `diagnose` findings (warning, critical), 3 is a config/connection error, 4 is `client` finding no single match, 64 is a usage error. Do not reuse 1 or 2 for errors.
- Dependencies are declared only in `pyproject.toml`; run `uv lock` after changing them and commit `uv.lock`.

## UniFi API notes

- Prefer the Integration API (`/proxy/network/integration/v1`): paginated, site IDs are UUIDs. `Config.site` may be a name, internal reference (`default`) or UUID; use `resolve_site`.
- Legacy `/proxy/network/api/s/{ref}/stat/...` takes the internal reference, not the UUID. It is used only for data the Integration API lacks (per-port counters, client-to-switch-port, DHCP reservations, network config).
- Legacy client records reference networks by legacy ids (from `rest/networkconf`), which do not match Integration API network UUIDs. A reservation is `use_fixedip` true; `fixed_ip` alone is stale-prone.
- Controller health is legacy `stat/health` (subsystems `wlan`, `lan`, `wan`, `www`, `vpn`). `lan`/`wlan` turn `error`/`warning` merely because devices are disconnected, so `diagnose` only trusts them when `num_disconnected` is 0. `Snapshot.health` is collected with `include_health`.
- Switch port health comes from legacy `stat/device` `port_table`: `link_down_count` is cumulative since boot, drops must be judged as a percentage of packets, and `poe_good` is false on any PoE-capable port without a PoE device (do not use it). A device's `uplink` dict carries `speed`/`max_speed` for its own uplink port.
- Wi-Fi quality comes from legacy `stat/sta` (`signal` in dBm, `satisfaction`, `wifi_tx_retries_percentage`, `wifi_tx_attempts`, `radio` ng/na/6e) and each AP's `radio_table_stats` (`cu_total`, `tx_retries_pct`, `satisfaction`). Many clients lack these fields, radio `satisfaction` is -1 when unknown, and `anomalies` is on nearly every client (do not use it). Retry percentages need a minimum attempt count to mean anything.
- Event checks in `diagnose` read `snap.events` (collected with `include_events`, window in `snap.event_window_seconds`); roaming is normal for phones, so it is info only. Use the snapshot's device names in findings so ignore rules match.
- Event log: POST `/proxy/network/v2/api/site/{ref}/system-log/all` with `timestampFrom`/`timestampTo` (ms), `pageNumber`, `pageSize` and the server filters `categories`, `severities`, `keys`, `searchText`. Singular names (`category`, `severity`, `types`) are silently ignored, so test a filter by checking the total changes. Records are newest first with `{PLACEHOLDER}` messages filled from `parameters[X].name`; some audit events leave placeholders unresolved. Reading does not change event status.
- Uplinks: a device's legacy `uplink` dict has `uplink_mac` (the parent), `uplink_remote_port` (the PARENT's port), `port_idx` (its own uplink port), `speed` and `max_speed`; a gateway's uplink is its WAN link, not part of the tree. Integration `uplink.deviceId` names the parent when the legacy data does not. An offline device keeps its last known uplink. `DeviceIndex` (client_view.py) and `diagnose.uplink_speeds` are the shared helpers for names, state and negotiated-vs-capability speed.
- Saved snapshots (`history.py`, schema version 1) hold real MACs/IPs: they are written owner-only into the git-ignored `snapshots/`, and tests and docs use only the synthetic fixture. Match by MAC; keep constantly changing values (uptime, last-seen, traffic) out of the record; an unknown location is never a "move". Snapshot file names sort by timestamp then numeric suffix, not as text.
- Client group membership is `network_members_group_ids` on legacy client records; group names/ids come from legacy v2 `/proxy/network/v2/api/site/{ref}/network-members-groups` (`client.legacy_v2`).
- Integration API and legacy fields were checked against one live controller (Network 10.6.106). Other versions and hardware may differ, and some fields vary by model (e.g. `mac_table_count` is null on some switches). Verify a new field against real data before relying on it.

## Safety

- Read-only: GET requests only, with **one approved exception**: the event log (`v2/system-log/all`) is POST-only, so `UniFiClient.system_log` sends a read-only query there (approved by the owner for event history, issue #49). It is used by `events` and, by default, by `diagnose` (`--no-events` skips it); a new feature may reuse it through the snapshot (`include_events`) but must not add another POST. It is the only POST: do not add a general POST/PUT/PATCH/DELETE to `UniFiClient`, and do not point `system_log` at any other path. `tests/test_events.py` enforces this. Any other write to the controller needs explicit approval for that specific action.
- Ask before running anything that contacts the real controller.
- Never print or commit `.env` or the API key. Redact real MACs, IPs and hostnames in issues, commits, PRs and test fixtures.

## Testing

- `FakeSession` deep-copies the fixture for each test, so tests may mutate the data they get; keep it that way.
- Tests use the synthetic fixture `tests/fixtures/controller.json` served by `FakeSession` in `tests/conftest.py`; never hit the live controller from tests. Add tests for new features and extend the fixture rather than pasting real data (redact MACs, IPs, names).

## Git and GitHub

- Commit only when asked. When asked to check in code, always create a feature branch and open a PR; never commit to `main`.
- Open the PR and stop; do not merge it. PRs normally wait for a GitHub Copilot code review, and the user merges after addressing it. Merge only when the user explicitly asks for that PR.
- This repo is a fork, so always pass `--repo jeffholst/unifi-sentinel` to `gh pr create`; the default target is the upstream repo.
- Keep local branches clean. Never work on `main`; it only moves via `git pull`. Start each branch from an up-to-date `main` unless the work depends on an open PR. Keep the PR branch (and stay on it) while the PR is open, since review fixes are pushed to it.
- After a PR is merged, clean up: `git checkout main && git pull`, then `git branch -d <branch>` (`-d` refuses if unmerged, so it doubles as a check; use `-D` only after confirming the PR is merged, e.g. after a squash merge). `fetch.prune` is enabled globally and the repo deletes head branches on merge, so `origin/*` refs clean themselves up. Before finishing a task, check `git branch -vv` and delete any local branch whose PR is merged; leave branches with open PRs alone.
- Track work in GitHub issues (`jeffholst/unifi-sentinel`) and reference them in commits and PRs. Pushing and issue creation need the `jeffholst` gh account active (`gh auth switch -u jeffholst`).
