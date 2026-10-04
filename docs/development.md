# Development and API documentation

How the code is laid out, the checks CI runs, the tests that keep the documentation honest, and the UniFi API documentation.

## Development

```text
hlp.py        thin launcher
homelab_probe/
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
  audit.py               configuration audit: Wi-Fi security, default names, firmware updates, unnamed clients
  firewall.py            firewall view: policies, port forwards, zone matrix and findings (zone-based)
  wifi.py                wireless report: radios and a channel plan from neighbors
  topology.py            uplink tree: wiring, link speeds, client counts, flags
  history.py             saved inventories (snapshot) and the diff between them
  diagnose/              read-only health checks, one module per topic, and areas.py (what --only/--skip choose between)
    __init__.py            diagnose(): runs every check, worst findings first
    model.py               severities, exit codes, the catalogue of finding codes, Finding
    devices.py  health.py  offline devices, CPU and memory; controller subsystems and the internet connection
    addresses.py reserved.py  client IPs, duplicates, randomized MACs; DHCP reservations
    ports.py  wireless.py  switch ports and uplinks; Wi-Fi quality
    event_checks.py        the event log (conflicts, disconnects, roaming, unreachable devices)
    output.py              ignoring findings, exit codes, text and JSON
  documents.py           documents: the JSON value a data-reporting command prints, plus warnings from its read (data-reporting commands except diagnose, snapshot, diff and export so far)
  demo/                  the synthetic controller: controller.json and DemoSession (the tests' fake controller and what --demo serves)
  logs.py                the logger tree: command-line, text and JSON formats, the redaction filter, ids, the events list, the warnings sink
  watch.py               diagnose --watch: what changed since the last pass (reuses the notification planner)
  doctor.py              the doctor checks of the tool's own setup (versions, files, settings, controller, endpoints)
  notify.py              notifications: what changed since the last run, ntfy/webhook/email sending, state file
  completion.py          shell completion scripts for bash, zsh and fish, generated from the argument parser
  settings.py            diagnose thresholds and ignore list (TOML)
  util.py                shared helpers: output safety (printable names, CSV formulas), numbers, MACs, times, plurals
  cli.py                 argument parser and main: loads the configuration, builds the client, runs a command
  commands.py            the commands: each one's arguments, checks and handler, and the registry
tests/
  conftest.py            the isolation fixture; FakeSession is demo.DemoSession
  (the synthetic controller data is homelab_probe/demo/controller.json)
  contract.py            the fields the code reads from each endpoint (the table behind the shape and live checks)
  field_tracking.py      finds those fields by recording which keys each command touches
  test_live_contract.py  opt-in (pytest -m live): the same table against a real controller, GET only
  docs_support.py        shared by the documentation tests: the doc files and GitHub's anchors
  test_docs_layout.py    the README stays short; every link resolves; every old anchor still exists
  test_json_schemas.py   real `--json` output (and the snapshot and webhook payload) validates against docs/schemas/
docs/                    the detail behind the README: one page per group of commands, settings, notifications, examples
  schemas.md, schemas/   a versioned JSON Schema for every --json output, the snapshot file and the webhook payload
tools/                   development scripts, not part of the package
  record_fixture.py      records a controller into a sanitised fixture
  sanitize.py            the deterministic sanitiser and its leak check
```

Outside contributors: [CONTRIBUTING.md](../CONTRIBUTING.md) has the short version of the rules, and a security problem goes through [SECURITY.md](../SECURITY.md), not an issue.

New features are new subcommands (a section and a registry row in `commands.py`) backed by modules that take a `Snapshot` (fetching stays in `snapshot.py` and `client.py`). Dependencies are declared once, in `pyproject.toml` (lockfile: `uv.lock`; regenerate with `uv lock`). Run the tests with `uv run pytest`; they use a synthetic fixture in `tests/fixtures/` and never contact a controller (except the opt-in `-m live` tests described below).

**Releases.** A release is a `vX.Y.Z` tag that **the owner** pushes; nothing here tags or publishes by itself. To prepare one: finish the entry for the version in [CHANGELOG.md](../CHANGELOG.md) (grouped Added, Changed, Fixed and Security; a change to an exit code, a finding code or a JSON `version` always gets a line) and replace its `Unreleased` by the date, set `__version__` in `homelab_probe/__init__.py` (the one place the version is written; `pyproject.toml` reads it), and merge that as a pull request. Then `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`. The [release workflow](../.github/workflows/release.yml) runs only for that push: it checks the lockfile, installs dependencies, refuses a tag that is not `v` plus the package version or a changelog entry without a real date (`tools/release_notes.py`), runs the tests, builds the wheel and source distribution with `uv build`, and creates the GitHub release with the changelog entry as its notes and the two files attached. It is the only workflow allowed to write to the repository and uses the runner's `gh` CLI rather than a third-party publishing action. A test keeps the changelog, the version and the workflow in step.

**Dependency updates.** [Dependabot](../.github/dependabot.yml) checks once a week (Mondays) for newer versions of the GitHub Actions that CI uses and of the Python dependencies in `pyproject.toml`/`uv.lock`, and opens one pull request per group, not one per package. They go through the same CI as any change (tests on Python 3.10 to 3.13, `ruff`, `mypy`, `uv lock --check`) and are merged by hand. The actions are pinned to exact versions on purpose (`astral-sh/setup-uv` publishes no floating major tag), which is what lets Dependabot keep them current; a test checks the pins. For security advisories, switch on **Dependabot alerts** and **Dependabot security updates** in the repository's Settings, under Advanced Security (they are repository settings, not files).

Checks (the same ones CI runs on every push and pull request, in `.github/workflows/ci.yml`):

```bash
uv run pytest             # tests; CI runs them on Python 3.10, 3.11, 3.12 and 3.13
uv run ruff check .       # lint (rules E, F, B, I, UP in pyproject.toml; lines up to 120 characters, tests exempt)
uv run mypy               # types, checked in untyped functions too; CI fails on any finding
uv lock --check           # uv.lock must match pyproject.toml; run `uv lock` after changing dependencies
```

**Tests that keep the documentation and the output honest:**

- `tests/test_golden.py` compares the text output of the main commands (`diagnose`, `topology`, `wan`, `wifi`, `client`, `events`, `new-clients` and the `query` kinds) with stored files in `tests/golden/`, produced from the synthetic fixture. Times, ages and table padding are normalised, so the files do not change from day to day. When a change to the output is intended, refresh them with `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff like code.
- `tests/test_docs_drift.py` checks the README and these pages against the program: every example command parses with the real argument parser, every command has a row in the README's Commands table, every long option is mentioned (and every option the README shows exists), and the sample output blocks (topology, wifi, wan, client, diagnose, new-clients, events and the exported CSVs) equal what the commands print. After an intended output change, `UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py` rewrites those blocks and their golden CSVs. The `diff` sample is illustrative on purpose and is not checked.
- `tests/test_entry_points.py` runs the launcher, the installed `hlp` script and `python -m homelab_probe.cli` in subprocesses, and `tests/test_exit_codes.py` produces every documented exit code (0, 1, 2, 3, 4 and 64) from a real scenario and checks the [README exit-code table](../README.md#exit-codes).
- Coverage: `uv run coverage run -m pytest` then `uv run coverage report` (the settings are in `pyproject.toml`: line and branch coverage, and the report fails under 100%). CI runs exactly that in the **Coverage (100%)** job, on Python 3.13 with fish and zsh installed so the completion tests that run the real shells do not skip. It is at 100% of lines and branches on every supported Python; the few `pragma: no cover`/`no branch` comments say why a line cannot run (for example the two TOML imports, of which only one runs on any Python). On a Mac without `fish`, or without util-linux `flock`, the tests that need them skip (the cron tests use a stand-in `flock`), so CI is where they all run.

**Does a real controller still return what the code reads?** Almost every endpoint the tool uses is undocumented, and the synthetic fixture is written by hand, so the tests alone prove consistency, not that a controller still answers with these fields. `tests/contract.py` is the one table of the fields the code reads from each endpoint (per endpoint: fields in every record, fields in at least one record, and optional ones). It is used three ways:

- `tests/test_contract_fixture.py` fails when the fixture lacks a field of the table, when the code reads a field the table does not list (every command is run against the fixture with a wrapper that records each key it touches) and when the table lists a field no command reads. When you read a new field, add it to the table and to the fixture.
- `uv run pytest -m live` reads the real controller once (settings from the environment or `./.env`, like the program) and checks each endpoint against the table. It is skipped by default and in CI, only makes GET requests and the one read-only event log query (anything else raises), and its failures name the endpoint and field, never a value. Without credentials it skips.
- `uv run tools/record_fixture.py` records a controller into `tools/recorded/controller.json` (git-ignored, readable only by you; `--output FILE` chooses another place, `--event-days N` how many days of events, `--env-file FILE` the settings). It keeps only the fields in the table and replaces every MAC, IP address, id, device, client and network name, SSID and ISP name with a synthetic one, the same way every time and the same everywhere (one real MAC is one synthetic MAC in every record; a private address stays private and in the same /24, a randomized MAC stays randomized), then checks that nothing real is left and writes nothing if it is. Use it to reproduce a problem on realistic data or to see what changed in a new controller version; the recording does not replace the hand-written fixture, which the tests depend on name by name.

`uv run ruff check . --fix` applies the safe fixes (import order, unused imports). The `List[...]` and `Optional[...]` annotation style is not enforced yet, and there is no code formatter. See [CLAUDE.md](../CLAUDE.md) for contributor and AI-assistant guidelines.

## API documentation

- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) on developer.ui.com, versioned by Network Application release (use the newest version listed). The copy matching your controller's version is also under **UniFi Network > Integrations** in the controller.
- [Getting Started with the Official UniFi API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API) in the Ubiquiti Help Center, including how to create API keys.

The official documentation covers the Integration API only. The legacy `stat/*`, `rest/*` and `v2/api/*` endpoints this tool also uses (port counters, client-to-port mapping, DHCP reservations, network config, client groups) are undocumented; their fields were determined from live controller responses.
