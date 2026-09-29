# UniFi Sentinel

Fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), being extended into a tool for querying, troubleshooting and inventorying a UniFi Network controller. Keep the Apache-2.0 license and upstream credit.

## Layout

- `unifi-sentinel.py`: thin launcher (PEP 723 metadata); all logic lives in `unifi_sentinel/`.
- `unifi_sentinel/config.py`: env/`.env` loading. `client.py`: `UniFiClient`, the only place that makes HTTP calls. `snapshot.py`: `collect_snapshot`, the one read of the controller. `export.py`, `query.py`, `diagnose.py`: pure functions over a `Snapshot`. `cli.py`: argparse subcommands.
- New features are new subcommands in `cli.py` backed by modules that take a `Snapshot`; keep fetching (snapshot), analysis and output separate.

## Commands

- Run: `uv run unifi-sentinel.py <export|info>`
- Tests: `uv run --with pytest pytest` (tests live in `tests/`)

## Conventions

- Stdlib `csv`, not pandas. Match the surrounding style; type-hint public functions.
- Raise `UniFiAPIError`/`ConfigError`; only `cli.main` turns them into messages and exit codes.
- Dependencies are declared in `pyproject.toml`, `requirements.txt` and the launcher's PEP 723 block; update all three together.

## UniFi API notes

- Prefer the Integration API (`/proxy/network/integration/v1`): paginated, site IDs are UUIDs. `Config.site` may be a name, internal reference (`default`) or UUID; use `resolve_site`.
- Legacy `/proxy/network/api/s/{ref}/stat/...` takes the internal reference, not the UUID. It is used only for data the Integration API lacks (per-port counters, client-to-switch-port).
- Integration API field names were written from docs and are not yet verified against a live controller; verify before relying on them.

## Safety

- Read-only by default: GET requests only. Any write to the controller (POST/PUT/PATCH/DELETE) needs explicit approval for that specific action.
- Ask before running anything that contacts the real controller.
- Never print or commit `.env` or the API key. Redact real MACs, IPs and hostnames in issues, commits, PRs and test fixtures.

## Testing

- Test against recorded/sanitized fixtures, never the live controller. Add tests for new features.

## Git and GitHub

- Commit only when asked. When asked to check in code, always create a feature branch and open a PR; never commit to `main`.
- Track work in GitHub issues (`jeffholst/unifi-sentinel`) and reference them in commits and PRs. Pushing and issue creation need the `jeffholst` gh account active (`gh auth switch -u jeffholst`).
