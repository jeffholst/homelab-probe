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
  topology_graph.py      the tree as a Mermaid flowchart or a Graphviz DOT digraph (escaping for both syntaxes)
  history.py             saved inventories (snapshot) and the diff between them
  groups.py              findings that share a cause (a device offline behind an offline uplink), with the evidence; the web findings list uses it (standard library only)
  diagnose/              read-only health checks, one module per topic, and areas.py (what --only/--skip choose between)
    __init__.py            diagnose(): runs every check, worst findings first
    model.py               severities, exit codes, the catalogue of finding codes, Finding
    devices.py  health.py  offline devices, CPU and memory; controller subsystems and the internet connection
    addresses.py reserved.py  client IPs, duplicates, randomized MACs; DHCP reservations
    ports.py  wireless.py  switch ports and uplinks; Wi-Fi quality
    event_checks.py        the event log (conflicts, disconnects, roaming, unreachable devices)
    output.py              ignoring findings, exit codes, text and JSON
  dashboard.py           the dashboard summary of the web API (status, completeness and headline counts from one diagnose-sized snapshot)
  documents.py           documents: the JSON value a data-reporting command prints, plus warnings from its read (every data-reporting command)
  demo/                  the synthetic controller: controller.json and DemoSession (the tests' fake controller and what --demo serves)
  logs.py                the logger tree: command-line, text and JSON formats, the redaction filter, ids, the events list, the warnings sink
  watch.py               diagnose --watch: what changed since the last pass (reuses the notification planner)
  doctor.py              the doctor checks of the tool's own setup (versions, files, settings, controller, endpoints)
  notify.py              notifications: what changed since the last run, ntfy/webhook/email sending, state file
  completion.py          shell completion scripts for bash, zsh and fish, generated from the argument parser
  pretty.py              enhanced terminal output: grouped, colored findings with an honest summary, tables that fit
  progress.py            the transient progress line on stderr (`Progress`, the stages the work reports through `client.stage`)
  present.py             terminal presentation policy (`--color`, `--plain`, `--no-progress`, `NO_COLOR`); the only module that imports the optional Rich
  server/                the web server (`serve`, the `web` extra): the app factory, security middleware and request log (`app`, `security`), the cache and the controller service (`cache`, `service`), the report routes, errors and API schemas (`routes`, `errors`, `apischema`), the guided setup of a server with no settings (`wizard`), the built web app's files with their SPA fallback (`static`; the bundle is `homelab_probe/web/`, git-ignored, shipped in the wheel); never imported by the core
  tlsprobe.py            the guided setup's TLS steps: fetch the certificate a controller shows, test a pinned one, refuse addresses no controller has (standard library and urllib3)
  accounts.py            web accounts: scrypt passwords, users.json, roles, last-admin protection, the audit log (standard library only)
  settings.py            diagnose thresholds and ignore list (TOML)
  util.py                shared helpers: output safety (printable names, CSV formulas), numbers, MACs, times, plurals
  cli.py                 argument parser and main: loads the configuration, builds the client, runs a command
  commands.py            the commands: each one's arguments, checks and handler, and the registry
tests/
  fixtures/web/         a placeholder bundle (not a built interface) that the static-file tests serve
  conftest.py            the isolation fixture; FakeSession is demo.DemoSession
  (the synthetic controller data is homelab_probe/demo/controller.json)
  contract.py            the fields the code reads from each endpoint (the table behind the shape and live checks)
  field_tracking.py      finds those fields by recording which keys each command touches
  test_live_contract.py  opt-in (pytest -m live): the same table against a real controller, GET only
  docs_support.py        shared by the documentation tests: the doc files and GitHub's anchors
  test_docs_layout.py    the README stays short; every link resolves; every old anchor still exists
  test_build_docs.py     the docs bundle builder, on the real docs and on crafted bad input
  test_json_schemas.py   real `--json` output (and the snapshot and webhook payload) validates against docs/schemas/
docs/                    the detail behind the README: one page per group of commands, settings, notifications, examples
  schemas.md, schemas/   a versioned JSON Schema for every --json output, the snapshot file and the webhook payload
web/                     the web interface (React, TypeScript, Vite): its own npm project, not part of the Python package
  src/api/               typed fetch client (same origin, CSRF header from memory), the one ApiError, the server's own answers, the setup and backup calls
  src/generated/         types generated from docs/schemas (committed; CI regenerates and fails on a difference)
  src/components/, pages/  the shell (header, menu sheet, account menu) and footer, the brand mark, the data-state components (Loading, Refreshing, Empty, Error, stale and partial banners), Text, the small UI pieces (icons, stepper, secret field, check list, code block), pages
  src/layouts/           the frame of the screens before a login (the login, the setup)
  src/setup/             the guided first-run setup and the restore of a backup on a fresh installation
  src/assets/brand/      the ant mascot and the logo (WebP, bundled with hashed names)
  src/lib/safeText.ts    port of util.printable (control, invisible and bidirectional characters), checked against shared vectors
  src/styles/, theme/    design tokens (dark by default, light, system) and the theme preference (the only thing put in browser storage)
  src/test/fakeApi.ts    a fake of the server's API for Vitest, held to tests/golden/openapi.json (fakeSetup.ts adds the setup and restore)
  e2e/                   the Playwright tests and the script that serves the build in front of `hlp --demo serve` and of a server in its setup mode
Dockerfile, compose.yaml, .dockerignore   the container image (a placeholder web build stage, the wheels, the non-root runtime) and its compose example
tools/                   development scripts, not part of the package
  docker_smoke.sh        the checks CI runs on the built image (version, non-root, setup mode, demo server, nothing secret in it)
  record_fixture.py      records a controller into a sanitised fixture
  sanitize.py            the deterministic sanitiser and its leak check
  build_docs.py          builds the documentation bundle (docs.json) for the web interface's Docs page
```

### The web interface (`web/`)

The browser UI is a separate npm project. Nothing in it reaches the command line or the Python wheel, and Node is needed only to work on the UI (Node 22, npm). The server serves a build copied into `homelab_probe/web/` ([web.md](web.md#the-web-app-files)); while you work on the UI it runs against `hlp serve` through a development proxy.

```bash
uv run --extra web hlp.py --demo serve     # terminal 1: synthetic network; prints "Demo login: user demo, password ..."
cd web
npm ci                                      # once; installs exactly package-lock.json
npm run dev                                 # terminal 2: http://localhost:5173, /api is proxied to http://127.0.0.1:8787
```

`HLP_API_TARGET=http://127.0.0.1:9000 npm run dev` points the proxy at another server (`serve --port 9000`). The proxy gives the server its own `Origin`, because the server refuses an unsafe request whose `Origin` is not its `Host` (its CSRF protection); the app itself sends no credentials but the session cookie and the `X-CSRF-Token` it got from the login. Log in with the demo account.

| Command (in `web/`) | What it does |
| ------------------- | ------------ |
| `npm run check` | Everything below except Playwright, in the order CI runs it |
| `npm run generate:types` | Regenerates `src/generated/*.ts` from `docs/schemas/*.schema.json`; commit the result when a schema changes |
| `npm run check:types` | Regenerates and fails on any difference (changed, removed or untracked generated file): the CI drift check |
| `npm run lint` | ESLint, including the rules that forbid `dangerouslySetInnerHTML`, assigning markup and using browser storage anywhere but `src/theme/storage.ts` |
| `npm run typecheck` | `tsc` in strict mode for the app and for the tooling |
| `npm test` | Vitest (unit and component tests against the fake API) |
| `npm run build` | The production bundle, written to `web/dist` only |
| `npm run e2e` | Playwright on a phone (390x844) and a desktop (1280x800) viewport, against the demo server and a server in its setup mode |

The Playwright tests build the app and start two real servers on free ports: `uv run --extra web hlp.py --demo serve`, and `hlp serve` with no settings in an empty temporary data directory (no `UNIFI_*`, `NOTIFY_*` or `HLP_ENV` variable, a setup token chosen by the script), which starts in its setup mode. `web/dist` is served in front of each on another free port, with the content-security policy the real server sends, so a script or style the policy forbids fails the test. They read the demo login from the server's output. The setup tests never let the server contact a controller (no certificate fetch or connection test: those are covered by Vitest with the fake), and the phone and desktop runs of them take turns because the draft lives on the server. The first run needs the browser: `npx playwright install chromium`. `E2E_SCREENSHOTS=/some/dir npm run e2e` also saves screenshots of the login, the shell and the setup screens.

Rules for the UI code: strings from the controller (names, SSIDs, event text, notes) are rendered with `<Text>`/`safeText()` only, never as markup; colours and sizes come from the tokens in `src/styles/tokens.css` (dark is the default at `:root`, light under `data-theme="light"` and, for `data-theme="system"`, the same light set under `prefers-color-scheme: light`; a test checks WCAG AA contrast of the pairs in both themes and that the two light sets agree); there are no inline styles or scripts, which the server's policy would refuse; the CSRF token and the setup token are kept in memory and nothing but the theme word goes to `localStorage`; a new route in the fake API must exist in `tests/golden/openapi.json`. A change to a JSON schema needs `npm run generate:types` in the same pull request.

Outside contributors: [CONTRIBUTING.md](../CONTRIBUTING.md) has the short version of the rules, and a security problem goes through [SECURITY.md](../SECURITY.md), not an issue.

New features are new subcommands (a section and a registry row in `commands.py`) backed by modules that take a `Snapshot` (fetching stays in `snapshot.py` and `client.py`). Dependencies are declared once, in `pyproject.toml` (lockfile: `uv.lock`; regenerate with `uv lock`). Run the tests with `uv run pytest`; they use a synthetic fixture in `tests/fixtures/` and never contact a controller (except the opt-in `-m live` tests described below).

**Releases.** A release is a `vX.Y.Z` tag that **the owner** pushes; nothing here tags or publishes by itself. To prepare one: finish the entry for the version in [CHANGELOG.md](../CHANGELOG.md) (grouped Added, Changed, Fixed and Security; a change to an exit code, a finding code or a JSON `version` always gets a line) and replace its `Unreleased` by the date, set `__version__` in `homelab_probe/__init__.py` (the one place the version is written; `pyproject.toml` reads it), and merge that as a pull request. Then `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`. The [release workflow](../.github/workflows/release.yml) runs only for that push: it checks the lockfile, installs dependencies, refuses a tag that is not `v` plus the package version or a changelog entry without a real date (`tools/release_notes.py`), runs the tests, builds the wheel and source distribution with `uv build`, and creates the GitHub release with the changelog entry as its notes and the two files attached. It is the only workflow allowed to write to the repository and uses the runner's `gh` CLI rather than a third-party publishing action. A test keeps the changelog, the version and the workflow in step.

**Dependency updates.** [Dependabot](../.github/dependabot.yml) checks once a week (Mondays) for newer versions of the GitHub Actions that CI uses, of the Python dependencies in `pyproject.toml`/`uv.lock` and of the base images named in the `Dockerfile`, and opens one pull request per group, not one per package. They go through the same CI as any change (tests on Python 3.10 to 3.13, `ruff`, `mypy`, `uv lock --check`) and are merged by hand. The actions are pinned to exact versions on purpose (`astral-sh/setup-uv` publishes no floating major tag), which is what lets Dependabot keep them current; a test checks the pins. For security advisories, switch on **Dependabot alerts** and **Dependabot security updates** in the repository's Settings, under Advanced Security (they are repository settings, not files).

Checks (the same ones CI runs on every push and pull request, in `.github/workflows/ci.yml`):

```bash
uv run pytest             # tests; CI runs them on Python 3.10, 3.11, 3.12 and 3.13
uv run ruff check .       # lint (rules E, F, B, I, UP in pyproject.toml; lines up to 120 characters, tests exempt)
uv run mypy               # types, checked in untyped functions too; CI fails on any finding
uv lock --check           # uv.lock must match pyproject.toml; run `uv lock` after changing dependencies
```

**The Docker image** ([Docker](docker.md)) is built by the **Docker image** job of `ci.yml` on every push and pull request (`docker build`, nothing is pushed, no login, no `packages: write`), and `tools/docker_smoke.sh` then checks it: `--version`, a non-root user, a command in `--demo` mode, a container with no configuration becoming healthy in the setup mode, a `--demo serve` container reached through the published port, a read-only root file system with a writable `/data`, and no secret or state file in the image. `tests/test_docker_files.py` checks the files without Docker (the `Dockerfile`, `compose.yaml`, `.dockerignore`, the workflow job and the documented commands). Publishing the image is part of a release, not of this job.

**Tests that keep the documentation and the output honest:**

- `tests/test_golden.py` compares the text output of the main commands (`diagnose`, `topology` and its Mermaid and DOT graphs, `wan`, `wifi`, `client`, `events`, `new-clients` and the `query` kinds) with stored files in `tests/golden/`, produced from the synthetic fixture. Times, ages and table padding are normalised, so the files do not change from day to day. When a change to the output is intended, refresh them with `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff like code.
- `tests/test_docs_drift.py` checks the README and these pages against the program: every example command parses with the real argument parser, every command has a row in the README's Commands table, every long option is mentioned (and every option the README shows exists), and the sample output blocks (topology, its Mermaid and DOT graphs, wifi, wan, client, diagnose, new-clients, events and the exported CSVs) equal what the commands print. After an intended output change, `UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py` rewrites those blocks and their golden CSVs. The `diff` sample is illustrative on purpose and is not checked.
- `tests/test_entry_points.py` runs the launcher, the installed `hlp` script and `python -m homelab_probe.cli` in subprocesses, and `tests/test_exit_codes.py` produces every documented exit code (0, 1, 2, 3, 4 and 64) from a real scenario and checks the [README exit-code table](../README.md#exit-codes).
- Coverage: `uv run coverage run -m pytest` then `uv run coverage report` (the settings are in `pyproject.toml`: line and branch coverage, and the report fails under 100%). CI runs exactly that in the **Coverage (100%)** job, on Python 3.13 with fish and zsh installed so the completion tests that run the real shells do not skip. It is at 100% of lines and branches on every supported Python; the few `pragma: no cover`/`no branch` comments say why a line cannot run (for example the two TOML imports, of which only one runs on any Python). On a Mac without `fish`, or without util-linux `flock`, the tests that need them skip (the cron tests use a stand-in `flock`), so CI is where they all run.

**Does a real controller still return what the code reads?** Almost every endpoint the tool uses is undocumented, and the synthetic fixture is written by hand, so the tests alone prove consistency, not that a controller still answers with these fields. `tests/contract.py` is the one table of the fields the code reads from each endpoint (per endpoint: fields in every record, fields in at least one record, and optional ones). It is used three ways:

- `tests/test_contract_fixture.py` fails when the fixture lacks a field of the table, when the code reads a field the table does not list (every command is run against the fixture with a wrapper that records each key it touches) and when the table lists a field no command reads. When you read a new field, add it to the table and to the fixture.
- `uv run pytest -m live` reads the real controller once (settings from the environment or `./.env`, like the program) and checks each endpoint against the table. It is skipped by default and in CI, only makes GET requests and the one read-only event log query (anything else raises), and its failures name the endpoint and field, never a value. Without credentials it skips.
- `uv run tools/record_fixture.py` records a controller into `tools/recorded/controller.json` (git-ignored, readable only by you; `--output FILE` chooses another place, `--event-days N` how many days of events, `--env-file FILE` the settings). It keeps only the fields in the table and replaces every MAC, IP address, id, device, client and network name, SSID and ISP name with a synthetic one, the same way every time and the same everywhere (one real MAC is one synthetic MAC in every record; a private address stays private and in the same /24, a randomized MAC stays randomized), then checks that nothing real is left and writes nothing if it is. Use it to reproduce a problem on realistic data or to see what changed in a new controller version; the recording does not replace the hand-written fixture, which the tests depend on name by name.

**The documentation bundle.** The in-app Docs page of the web interface is built from the documentation in this repository, with no second copy of the content: `uv run tools/build_docs.py OUT_DIR --tag vX.Y.Z` reads `README.md`, `docs/*.md` and `docs/schemas/*.json` and writes `OUT_DIR/docs.json` plus the referenced images and all schema files under `OUT_DIR/docs/`. It uses only the standard library, makes no request and reads nothing outside the repository (`--root DIR` builds another checkout; `--repository URL` changes the repository that links leaving the bundle point at; a previous build in `OUT_DIR` is replaced, anything else in it is refused, and a symbolic link in `OUT_DIR` where a file or folder of the bundle goes is refused before anything is removed or written; a package file, schema file or schema folder that is a link out of the repository fails the build). `--tag` is `v` plus the package version for a release (a mismatch fails, like `tools/release_notes.py`) or a branch name such as `main`. `docs.json` has the version built, the pages in README navigation order grouped (a page in no group of the `GROUPS` table at the top of the tool lands in a last group, "More"), and per page its id, title, source file, Markdown, heading tree with GitHub's anchors (repeated headings numbered `-1`, `-2`), plain search text, links and images. A relative link between pages becomes `{"kind": "page", "page", "anchor"}`, a schema or image `{"kind": "file", "path"}`, anything else in the repository a link at the tag (`{"kind": "repository", "href"}`) and an `http`, `https` or `mailto` link stays as it is. The tool only classifies the Markdown, it does not render it, and it accepts the approved subset: ATX headings, paragraphs, bullet and numbered lists (nested, holding fenced code and tables), tables, backtick fences, block quotes, links, images, emphasis and inline code. It **fails the build** (exit code 1, every problem listed as `file:line: reason`) on raw HTML outside code (a comment `<!-- ... -->` and an anchor `<a id="name"></a>` are inert: they are removed from the bundled Markdown, and the anchor name is kept in the page's `anchors`), on setext headings, horizontal rules, `~~~` fences, indented code blocks, reference-style links and definitions, `<...>` link destinations, tabs in indentation, a list item or quote continued by an unindented line, and control, zero-width or direction-changing characters; on an image that is missing, remote, not an image file or outside the repository; on a link to a page, file or anchor that does not exist, outside the repository (symbolic links are followed, so one that leaves it fails too) or with a scheme other than `http`, `https` and `mailto`; and on a page without a `# Title`, two pages with the same id, a schema that is not JSON, an unusable `--tag` or an output directory that is the repository. `tests/test_build_docs.py` builds the real documentation (every page, every anchor, a finding code in the search text, the same bytes on a second run) and each failure above from a crafted repository. A new page needs nothing but its file; add it to `GROUPS` to place it in a group.

`uv run ruff check . --fix` applies the safe fixes (import order, unused imports). The `List[...]` and `Optional[...]` annotation style is not enforced yet, and there is no code formatter. See [CLAUDE.md](../CLAUDE.md) for contributor and AI-assistant guidelines.

## API documentation

- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) on developer.ui.com, versioned by Network Application release (use the newest version listed). The copy matching your controller's version is also under **UniFi Network > Integrations** in the controller.
- [Getting Started with the Official UniFi API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API) in the Ubiquiti Help Center, including how to create API keys.

The official documentation covers the Integration API only. The legacy `stat/*`, `rest/*` and `v2/api/*` endpoints this tool also uses (port counters, client-to-port mapping, DHCP reservations, network config, client groups) are undocumented; their fields were determined from live controller responses.
