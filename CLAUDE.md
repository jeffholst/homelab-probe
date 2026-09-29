# UniFi Sentinel

Fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), being extended into a tool for querying, troubleshooting and inventorying a UniFi Network controller. Keep the Apache-2.0 license and upstream credit.

## Layout

- `unifi-sentinel.py`: thin launcher; all logic lives in `unifi_sentinel/`.
- `unifi_sentinel/config.py`: env/`.env` loading. `client.py`: `UniFiClient`, the only place that makes HTTP calls. `snapshot.py`: `collect_snapshot`, the one read of the controller. `export.py`, `query.py`, `diagnose.py`: pure functions over a `Snapshot`. `cli.py`: argparse subcommands.
- New features are new subcommands in `cli.py` backed by modules that take a `Snapshot`; keep fetching (snapshot), analysis and output separate.

## Commands

- Run: `uv run unifi-sentinel.py <export|info>`
- Tests: `uv run pytest`

## Conventions

- Stdlib `csv`, not pandas. Match the surrounding style; type-hint public functions.
- Raise `UniFiAPIError`/`ConfigError`; only `cli.main` turns them into messages and exit codes.
- Dependencies are declared only in `pyproject.toml`; run `uv lock` after changing them and commit `uv.lock`.

## UniFi API notes

- Prefer the Integration API (`/proxy/network/integration/v1`): paginated, site IDs are UUIDs. `Config.site` may be a name, internal reference (`default`) or UUID; use `resolve_site`.
- Legacy `/proxy/network/api/s/{ref}/stat/...` takes the internal reference, not the UUID. It is used only for data the Integration API lacks (per-port counters, client-to-switch-port, DHCP reservations, network config).
- Legacy client records reference networks by legacy ids (from `rest/networkconf`), which do not match Integration API network UUIDs. A reservation is `use_fixedip` true; `fixed_ip` alone is stale-prone.
- Integration API field names were written from docs and are not yet verified against a live controller; verify before relying on them.

## Safety

- Read-only by default: GET requests only. Any write to the controller (POST/PUT/PATCH/DELETE) needs explicit approval for that specific action.
- Ask before running anything that contacts the real controller.
- Never print or commit `.env` or the API key. Redact real MACs, IPs and hostnames in issues, commits, PRs and test fixtures.

## Testing

- Tests use the synthetic fixture `tests/fixtures/controller.json` served by `FakeSession` in `tests/conftest.py`; never hit the live controller from tests. Add tests for new features and extend the fixture rather than pasting real data (redact MACs, IPs, names).

## Git and GitHub

- Commit only when asked. When asked to check in code, always create a feature branch and open a PR; never commit to `main`.
- Open the PR and stop; do not merge it. PRs normally wait for a GitHub Copilot code review, and the user merges after addressing it. Merge only when the user explicitly asks for that PR.
- This repo is a fork, so always pass `--repo jeffholst/unifi-sentinel` to `gh pr create`; the default target is the upstream repo.
- Keep local branches clean. Never work on `main`; it only moves via `git pull`. Start each branch from an up-to-date `main` unless the work depends on an open PR. Keep the PR branch (and stay on it) while the PR is open, since review fixes are pushed to it.
- After a PR is merged, clean up: `git checkout main && git pull`, then `git branch -d <branch>` (`-d` refuses if unmerged, so it doubles as a check; use `-D` only after confirming the PR is merged, e.g. after a squash merge). `fetch.prune` is enabled globally and the repo deletes head branches on merge, so `origin/*` refs clean themselves up. Before finishing a task, check `git branch -vv` and delete any local branch whose PR is merged; leave branches with open PRs alone.
- Track work in GitHub issues (`jeffholst/unifi-sentinel`) and reference them in commits and PRs. Pushing and issue creation need the `jeffholst` gh account active (`gh auth switch -u jeffholst`).
