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
  new_clients.py         new clients: first seen within a window, or in no client group
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
  server/terminal_policy.py  explicit terminal capability registry, pure parsing, limits and tested compatibility matrix
  server/terminal_api.py     authenticated capabilities, completion and bounded execution; terminal_completion.py suggests static metadata, terminal_reports.py has fixed adapters, report_validation.py shares report-route checks
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
  src/api/               typed fetch client (same origin, CSRF header from memory), the one ApiError, the server's own answers, the report, setup and backup calls
  src/generated/         types generated from docs/schemas (committed; CI regenerates and fails on a difference)
  src/components/, pages/  the shell (header, menu sheet, account menu) and footer, the brand mark, the data-state components (Loading, Refreshing, Empty, Error, stale and partial banners), Text, the small UI pieces (icons, stepper, secret field, check list, code block), pages
  src/layouts/           the frame of the screens before a login (the login, the setup)
  src/setup/             the guided first-run setup and the restore of a backup on a fresh installation
  src/terminal/engine/   pure command-line model: immutable transitions, tokenization, suggestions, streaming input decoding and cell-width wrapping; no DOM, xterm or API calls
  src/assets/brand/      the ant mascot and the logo (WebP, bundled with hashed names)
  src/terminal/          the terminal panel (#276): the command-line engine (`engine/`, contract in `engine/types.ts`), the xterm.js surface, the dock, the session hook, the mock backend and a stand-in engine for tests
  src/terminal/api.ts    gated real terminal backend (#277): validated capabilities, completion and execution over the shared API client; no execution retries or query cache
  scripts/probe-terminal-csp.mjs  isolated loopback CSP nonce feasibility probe; no production enablement or API calls
  src/lib/safeText.ts    port of util.printable (control, invisible and bidirectional characters), checked against shared vectors
  src/styles/, theme/    design tokens (dark by default, light, system) and the theme preference (the only thing put in browser storage)
  src/test/fakeApi.ts    a fake of the server's API for Vitest, held to tests/golden/openapi.json (fakeSetup.ts adds the setup and restore, fakeReports.ts the synthetic site list and dashboard)
  e2e/                   the Playwright tests, the script that serves the build in front of the real servers and `stub_controller.py`, the stub controller of the setup tests
Dockerfile, compose.yaml, .dockerignore   the container image (a placeholder web build stage, the wheels, the non-root runtime) and its compose example
tools/                   development scripts, not part of the package
  docker_smoke.sh        the checks CI runs on the built image (version, non-root, setup mode, demo server, nothing secret in it)
  record_fixture.py      records a controller into a sanitised fixture
  sanitize.py            the deterministic sanitiser and its leak check
  build_docs.py          builds the documentation bundle (docs.json) for the web interface's Docs page
```

### The web interface (`web/`)

The browser UI is a separate npm project. Its Node dependencies are not Python runtime dependencies; Node 22+ and npm are needed only to develop or build the UI. The built files can be shipped as data in the Python wheel. The server serves a build copied into `homelab_probe/web/` ([build and serve instructions](web.md#building-and-serving-the-browser-ui)); while you work on the UI it runs against `hlp serve` through a development proxy.

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
| `npm run generate:types` | Regenerates `src/generated/*.ts` from `docs/schemas/*.schema.json` and `docs/terminal/*.schema.json`; commit the result when a schema changes |
| `npm run check:types` | Regenerates and fails on any difference (changed, removed or untracked generated file): the CI drift check |
| `npm run lint` | ESLint, including the rules that forbid `dangerouslySetInnerHTML`, assigning markup and using browser storage anywhere but `src/theme/storage.ts` |
| `npm run typecheck` | `tsc` in strict mode for the app and for the tooling |
| `npm test` | Vitest (unit and component tests against the fake API) |
| `npm run build` | The production bundle, written to `web/dist` only |
| `npm run e2e` | Playwright on a phone (390x844) and a desktop (1280x800) viewport, against the demo server, a separate preview-gated terminal API build and servers in setup mode |

The Playwright tests build the app and start real servers on free ports (`E2E_SERVERS` in `web/playwright.config.ts`): `uv run --extra web hlp.py --demo serve` for the ordinary tests and the separately built preview-gated terminal API suite, and for each of the phone and desktop setup projects several `hlp serve` instances in empty temporary data directories with no `UNIFI_*`, `NOTIFY_*` or `HLP_ENV` variable and a setup token chosen by the script: one with no settings (setup mode), one configured for an address that cannot exist (`https://controller.invalid`) with no account (admin mode), and three for the tests that need a controller to talk to (a server to run the whole setup on, one whose environment sets `UNIFI_VERIFY_SSL` so that finishing returns the "nothing was saved" files, and one started with `--read-only`). Those talk to **stub controllers** (`web/e2e/stub_controller.py`): HTTPS servers on 127.0.0.1 that serve the synthetic demo network, accept one random API key (any other gets `401`) and show a throwaway certificate from `tests/tls_fixtures.py`, one that can be pinned and one that is valid only for another name. The mock build and preview-gated terminal API build are served separately; the latter uses the real demo server and `web/e2e/terminal-api.spec.ts` covers real supported/unsupported operations plus the API boundary's permission, timeout, truncation and hostile-output handling, suggestion truncation, session cleanup and light/phone display. `web/dist` and `web/dist-terminal-api` are served in front of their server on another free port, with the content-security policy the real server sends, so a script or style the policy forbids fails the test. The demo login and the setup tokens (and the stub's address, key and fingerprints, and each server's data directory, so a test can check what was and was not written there) come from the harness through `/__e2e/credentials`. `setup.spec.ts` creates the first administrator on the admin-mode server (light theme), exports a backup of it (it holds a seeded note about a device that is in no inventory) and restores it on the setup-mode server (dark theme); `setup-stub.spec.ts` runs the whole setup against a stub (a certificate that cannot be pinned, a refused key, pinning, the site picker, the health check, the dashboard read afterwards over the pinned certificate), the fallback and the read-only refusal. Nothing reaches a real controller or sends a notification (the notifications step is a dry run). The first run needs the browser: `npx playwright install chromium`. `E2E_SCREENSHOTS=/some/dir npm run e2e` also saves screenshots of the login, the shell, the dashboard and the setup screens.

For local browser testing, run these commands in `web/` after installing Python dependencies as described in [Routine Checks](../MAINTAINING.md#routine-checks):

```bash
npm ci
npx playwright install chromium
npm run e2e
# Interactive runner, or visible browsers
npm run e2e -- --ui
npm run e2e -- --headed
```

The configuration is `web/playwright.config.ts`; tests and the server harness are in `web/e2e/`. No manually started server or live UniFi controller is needed. If the browser executable is missing, repeat `npx playwright install chromium`; on Linux, `npx playwright install --with-deps chromium` also installs system libraries and may require administrator privileges. A server startup failure is reported before tests run: check that uv is available, the `web` extra is installed, and `npm run build` succeeds. Failure traces are kept under `web/test-results/`; open a reported trace file with `npx playwright show-trace PATH_TO_TRACE_ZIP`. GitHub CI uploads that folder on failure and retains it for seven days. Treat traces and screenshots as potentially sensitive when testing against anything other than the isolated synthetic harness.

Rules for the UI code: strings from the controller (names, SSIDs, event text, notes) are rendered with `<Text>`/`safeText()` only, never as markup; colours and sizes come from the tokens in `src/styles/tokens.css` (dark is the default at `:root`, light under `data-theme="light"` and, for `data-theme="system"`, the same light set under `prefers-color-scheme: light`; a test checks WCAG AA contrast of the pairs in both themes and that the two light sets agree); there are no inline styles or scripts, which the server's policy would refuse; the CSRF token and the setup token are kept in memory and nothing but the theme word and the height of the terminal panel goes to `localStorage` (`src/theme/storage.ts`); a call that carries a secret (a password, the API key, a passphrase, a backup file) uses `useAction` (`src/lib/useAction.ts`), never `useMutation`, whose cache keeps the variables for minutes (the shared client also drops every mutation at once, and the tests sweep the caches for poisoned values); a new route in the fake API must exist in `tests/golden/openapi.json`. A change to a JSON schema needs `npm run generate:types` in the same pull request.

**The terminal panel (`src/terminal/`, issue #276).** A panel docked at the bottom of the shell (open, collapsed, expanded or closed; resizable with the pointer or the arrow keys; its height is the one thing remembered, nothing of the transcript, history or drafts survives a reload). The command line itself is owned by the engine (`engine/types.ts` is its contract and `engine/` implements it; `defaultEngine.ts` picks the real one, and the panel tests run against both it and the small stand-in `testing/miniEngine.ts`, so the panel cannot come to depend on either); xterm.js only draws it, and the engine, not the terminal's display, is what a submitted command is built from. `render.ts` is the one place that turns state into terminal bytes: every character that came from a report, a command line or a suggestion goes through `clean.ts` first (no escape or other control character, no direction override), so a report cannot move the cursor, change the title, overwrite the prompt or draw a status line, and the only escape sequences written are the colours and cursor moves written there (`render.test.ts` checks that for hostile input). The terminal library is a separate chunk that is fetched when the panel is first opened. Only `@xterm/xterm` and `@xterm/addon-fit` are loaded: no links, clipboard-writing, title handling or answers to terminal queries. Keys: Tab completes, Escape leaves the terminal (then Tab moves on), Ctrl+C clears the line, copies a selection, or while a command runs says that waiting stopped (the command may still be running on the server). The shortcut buttons come from the same suggestion list as Tab and only add text. A screen reader gets status sentences in a polite live region and the transcript as a log it can navigate; typed characters are not announced.

The panel exists only where `TerminalContext` provides services. `VITE_TERMINAL_MOCK=1` selects the mock backend and real engine; `VITE_TERMINAL_API_PREVIEW=1` selects the real backend through the same API client as the rest of the app (the mock flag takes precedence). Both default off: a normal build contains no way to show the panel. `e2e/terminal.spec.ts` runs against the mock build; `e2e/terminal-api.spec.ts` runs against the separate preview-gated build and real synthetic demo API. The preview gate is for development against the synthetic demo server; it does not change the server CSP and is not production enablement. See [browser terminal API integration](web.md#browser-terminal-api-integration) for the remaining acceptance work.

**The policy and the terminal (open decision).** The server's policy for the web app is `style-src 'self'` with no `'unsafe-inline'`. xterm.js injects `<style>` elements, which that policy blocks: the text then loses its monospace layout and colours (checked in a browser; allowing only `style-src-attr` is not enough). The spec therefore states the one relaxation it needs (`style-src 'self' 'unsafe-inline'` on the page) instead of hiding the violation. Whether to relax the policy for the app, to serve the terminal from a separate document with its own policy, or to approve another renderer is the owner's decision and has to be made before #277 turns the terminal on; nothing in the server changed in #276.

The bordered terminal surface reserves bottom padding around an unpadded, borderless inner viewport. The fit addon measures that inner viewport, not the outer border-box height; keep padding on the outer surface so fitted rows cannot extend into the inset or be clipped. Browser tests check filled output, scrolling and resizing on phone and desktop.

Terminal text uses the default foreground and semantic ANSI color slots (red: errors, green: success, yellow: warnings, blue: commands, magenta: prompt, cyan: muted text), mapped to the current CSS tokens by `XtermSurface`. Do not embed RGB colors in rendered text: changing xterm's theme updates indexed cells, including scrollback, but does not recolor explicit RGB cells. Theme observers update only the theme, without rewriting the live region or replaying output, so scroll position, draft cursor and in-flight writes are preserved. Browser tests switch themes after writing output, including system color-scheme changes, and check actual rendered colors on phone and desktop.

**Narrow CSP investigation (#277, 2026-10-08).** The owner selected investigation of a narrow exception, not app-wide inline-style permission. A Chromium feasibility probe with the installed xterm 6.0.0 succeeds using a fresh 192-bit per-document style nonce: retain the existing policy and add `style-src-elem 'self' 'nonce-<random>'` plus `style-src-attr 'none'`. Do not add that nonce to `script-src`. A terminal-local document adapter authorizes xterm's generated styles before insertion, without changing global DOM methods, using the public `documentOverride` option. There is no native nonce option in this version. Its scrollbar takes a different document path, so the prototype also wraps `appendChild` on terminal-created screen elements; the document adapter alone covers only two of the three styles. This compatibility hook needs version-pinned regression tests, not an assumption that future xterm releases preserve those internals.

Run the isolated experiment from `web/` with `node scripts/probe-terminal-csp.mjs /tmp/hlp-terminal-csp`. It binds an ephemeral loopback port, serves only synthetic HTML and the installed library, captures phone/desktop screenshots in dark/light terminal themes, and stops its server/browser in cleanup. Six cases compare the unchanged strict policy, the correct nonce and a wrong nonce at both sizes. The correct token applies all three styles with no CSP violation through theme changes, resize and keyboard input; missing/wrong tokens reproduce blocked styling. Negative probes confirm unrelated inline styles, style attributes and inline scripts (even with the valid style nonce) stay blocked. Only Chromium is installed/tested; this is not real-app or cross-browser acceptance.

**Recommendation:** implement the style-only nonce path behind the existing preview gate, then run real-app acceptance before enabling production. The server must generate a fresh nonce for each HTML document, deliver it on the same-origin external bootstrap script's `nonce` attribute (read through `.nonce`, not a globally auto-noncing observer), and put the matching token in that document's CSP. Nonce-bearing HTML must not be reused from caches; update HEAD/content-length/ETag behavior consistently, leave immutable assets and API policies alone, and test deep links and token mismatch/reuse. CSP authorizes possession of the nonce, not a DOM subtree: trusted scripts can read it, so the adapter must not authorize arbitrary user/report HTML. Existing plain-text rendering remains mandatory. The standalone probe does not modify production headers, bootstrap or xterm integration; PR #305 remains draft and the real-API browser walkthrough is still outstanding. Background: [CSP nonce requirements](https://www.w3.org/TR/CSP/#security-nonces) and [xterm's upstream CSP discussion](https://github.com/xtermjs/xterm.js/issues/4445).

Outside contributors: [CONTRIBUTING.md](../CONTRIBUTING.md) has the short version of the rules, and a security problem goes through [SECURITY.md](../SECURITY.md), not an issue.

New data-reporting commands are subcommands (a section and a registry row in `commands.py`) backed by modules that take a `Snapshot` (fetching stays in `snapshot.py` and `client.py`). Dependencies are declared once, in `pyproject.toml` (lockfile: `uv.lock`; regenerate with `uv lock`). Use the [routine checks](../MAINTAINING.md#routine-checks), including both optional extras, to test the whole package. The synthetic controller fixture is `homelab_probe/demo/controller.json`; default tests never contact a real controller (except the opt-in `-m live` tests described below).

**Releases.** The owner publishes a release by pushing a `vX.Y.Z` tag after merging a release-preparation PR. Follow [Prepare a Release](../MAINTAINING.md#prepare-a-release) and [Tag and Publish](../MAINTAINING.md#tag-and-publish) for the canonical version, changelog, OpenAPI regeneration and validation steps. The release workflow builds Python artifacts and creates a GitHub release; it does not currently build the browser bundle or publish a Docker image.

**Dependency updates.** [Dependabot](../.github/dependabot.yml) checks weekly on Mondays for GitHub Actions, Python dependencies (`pyproject.toml`/`uv.lock`), web dependencies (`web/package.json`/`web/package-lock.json`) and Docker base images. Updates are grouped per ecosystem and reviewed by hand through the applicable CI jobs, including web checks for npm changes. For security advisories, enable Dependabot alerts and security updates in the repository settings; these are not configured by repository files.

Checks (CI runs on pushes to `main` and pull requests, not feature-branch pushes alone, in `.github/workflows/ci.yml`). The canonical setup and check commands are in [Routine Checks](../MAINTAINING.md#routine-checks):

```bash
uv sync --locked --group dev --extra web --extra pretty
uv run --extra web --extra pretty python -m pytest  # CI: Python 3.10 to 3.13
uv run --extra web --extra pretty ruff check .      # rules E, F, B, I, UP
uv run --extra web --extra pretty python -m mypy    # types; CI fails on any finding
uv lock --check           # uv.lock must match pyproject.toml; run `uv lock` after changing dependencies
```

**The Docker image** ([Docker](docker.md)) is built by the **Docker image** job of `ci.yml` on pushes to `main` and pull requests (`docker build`, nothing is pushed, no login, no `packages: write`). `tools/docker_smoke.sh` checks the version, non-root user, demo CLI, setup-mode health, demo API through the published port, read-only root file system with writable `/data`, and absence of secret or state files in the image. `tests/test_docker_files.py` checks the build files, workflow and documented commands without Docker. The image still has a placeholder web build stage; image publication is planned release work, not implemented in the current release workflow.

**Tests that keep the documentation and the output honest:**

- `tests/test_terminal_policy.py` checks strict parsing without exits or file access, rejects unavailable capabilities, forces CLI-to-web parity failures for added or stale commands/options/aliases/positional choices and changed file-reference metadata, and compares the compatibility matrix in [the terminal API](web.md#the-terminal-api) with the registry. Execution parsing refuses grammar drift at runtime. `build_parser(strict=True)` reuses the grammar and cross-option validators; the normal CLI parser and goldens stay unchanged.
- `tests/test_terminal_api.py` sends handcrafted requests directly: schema/auth/body refusals before dispatch, report equivalence, output redaction and caps, rate/concurrency admission, and worker slot retention after timeout or HTTP-task cancellation. Regenerate the terminal wire schemas with `uv run --extra web python -m tools.generate_terminal_contracts`, then run `npm run generate:types` in `web/`. The same-origin browser smoke procedure is in [Testing Before the Terminal UI](web.md#testing-before-the-terminal-ui).
- `tests/test_terminal_completion.py` proves static suggestions cannot execute or read configuration/files, validates token/cursor bounds and role isolation, shares execution admission, and checks the frontend mock fixture against the wire schemas and registry. [The frontend handoff](web.md#frontend-handoff) maps the complete #270 acceptance list to these tests and the existing guards.
- `tests/test_golden.py` compares the text output of the main commands (`diagnose`, `topology` and its Mermaid and DOT graphs, `wan`, `wifi`, `client`, `events`, `new-clients` and the `query` kinds) with stored files in `tests/golden/`, produced from the synthetic fixture. Times, ages and table padding are normalised, so the files do not change from day to day. When a change to the output is intended, refresh them with `UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff like code.
- `tests/test_docs_drift.py` checks the README and these pages against the program: every example command parses with the real argument parser, every command has a row in the README's Commands table, every long option is mentioned (and every option the README shows exists), and the sample output blocks (topology, its Mermaid and DOT graphs, wifi, wan, client, diagnose, new-clients, events and the exported CSVs) equal what the commands print. After an intended output change, `UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py` rewrites those blocks and their golden CSVs. The `diff` sample is illustrative on purpose and is not checked.
- `tests/test_entry_points.py` runs the launcher, the installed `hlp` script and `python -m homelab_probe.cli` in subprocesses, and `tests/test_exit_codes.py` produces every documented exit code (0, 1, 2, 3, 4 and 64) from a real scenario and checks the [README exit-code table](../README.md#exit-codes).
- Coverage: locally, `uv run --extra web --extra pretty coverage run -m pytest` then `uv run --extra web --extra pretty coverage report` (the settings are in `pyproject.toml`: line and branch coverage, and the report fails under 100%). CI first installs both extras, then runs `uv run coverage run -m pytest -q` and `uv run coverage report` in the **Coverage (100%)** job, on Python 3.13 with fish and zsh installed so the completion tests that run the real shells do not skip. It is at 100% of lines and branches on every supported Python; the few `pragma: no cover`/`no branch` comments say why a line cannot run (for example the two TOML imports, of which only one runs on any Python). On a Mac without `fish`, or without util-linux `flock`, the tests that need them skip (the cron tests use a stand-in `flock`), so CI is where they all run.

**Does a real controller still return what the code reads?** Almost every endpoint the tool uses is undocumented, and the synthetic fixture is written by hand, so the tests alone prove consistency, not that a controller still answers with these fields. `tests/contract.py` is the one table of the fields the code reads from each endpoint (per endpoint: fields in every record, fields in at least one record, and optional ones). It is used three ways:

- `tests/test_contract_fixture.py` fails when the fixture lacks a field of the table, when the code reads a field the table does not list (every command is run against the fixture with a wrapper that records each key it touches) and when the table lists a field no command reads. When you read a new field, add it to the table and to the fixture.
- `uv run pytest -m live` reads the real controller once (settings from the environment or `./.env`, like the program) and checks each endpoint against the table. It is skipped by default and in CI, only makes GET requests and the one read-only event log query (anything else raises), and its failures name the endpoint and field, never a value. Without credentials it skips.
- `uv run tools/record_fixture.py` records a controller into `tools/recorded/controller.json` (git-ignored, readable only by you; `--output FILE` chooses another place, `--event-days N` how many days of events, `--env-file FILE` the settings). It keeps only the fields in the table and replaces every MAC, IP address, id, device, client and network name, SSID and ISP name with a synthetic one, the same way every time and the same everywhere (one real MAC is one synthetic MAC in every record; a private address stays private and in the same /24, a randomized MAC stays randomized), then checks that nothing real is left and writes nothing if it is. Use it to reproduce a problem on realistic data or to see what changed in a new controller version; the recording does not replace the hand-written fixture, which the tests depend on name by name.

**The documentation bundle.** The bundle generator is infrastructure for a planned in-app Docs page; the current browser UI has no Docs route and links to documentation on GitHub instead. The bundle uses the documentation in this repository, with no second copy of the content: `uv run tools/build_docs.py OUT_DIR --tag vX.Y.Z` reads `README.md`, `docs/*.md` and `docs/schemas/*.json` and writes `OUT_DIR/docs.json` plus the referenced images and all schema files under `OUT_DIR/docs/`. It uses only the standard library, makes no request and reads nothing outside the repository (`--root DIR` builds another checkout; `--repository URL` changes the repository that links leaving the bundle point at; a previous build in `OUT_DIR` is replaced, anything else in it is refused, and a symbolic link in `OUT_DIR` where a file or folder of the bundle goes is refused before anything is removed or written; a package file, schema file or schema folder that is a link out of the repository fails the build). `--tag` is `v` plus the package version for a release (a mismatch fails, like `tools/release_notes.py`) or a branch name such as `main`. `docs.json` has the version built, the pages in README navigation order grouped (a page in no group of the `GROUPS` table at the top of the tool lands in a last group, "More"), and per page its id, title, source file, Markdown, heading tree with GitHub's anchors (repeated headings numbered `-1`, `-2`), plain search text, links and images. A relative link between pages becomes `{"kind": "page", "page", "anchor"}`, a schema or image `{"kind": "file", "path"}`, anything else in the repository a link at the tag (`{"kind": "repository", "href"}`) and an `http`, `https` or `mailto` link stays as it is. The tool only classifies the Markdown, it does not render it, and it accepts the approved subset: ATX headings, paragraphs, bullet and numbered lists (nested, holding fenced code and tables), tables, backtick fences, block quotes, links, images, emphasis and inline code. It **fails the build** (exit code 1, every problem listed as `file:line: reason`) on raw HTML outside code (a comment `<!-- ... -->` and an anchor `<a id="name"></a>` are inert: they are removed from the bundled Markdown, and the anchor name is kept in the page's `anchors`), on setext headings, horizontal rules, `~~~` fences, indented code blocks, reference-style links and definitions, `<...>` link destinations, tabs in indentation, a list item or quote continued by an unindented line, and control, zero-width or direction-changing characters; on an image that is missing, remote, not an image file or outside the repository; on a link to a page, file or anchor that does not exist, outside the repository (symbolic links are followed, so one that leaves it fails too) or with a scheme other than `http`, `https` and `mailto`; and on a page without a `# Title`, two pages with the same id, a schema that is not JSON, an unusable `--tag` or an output directory that is the repository. `tests/test_build_docs.py` builds the real documentation (every page, every anchor, a finding code in the search text, the same bytes on a second run) and each failure above from a crafted repository. A new page needs nothing but its file; add it to `GROUPS` to place it in a group.

`uv run ruff check . --fix` applies the safe fixes (import order, unused imports). The `List[...]` and `Optional[...]` annotation style is not enforced yet, and there is no code formatter. See [AGENTS.md](../AGENTS.md) for contributor and AI-assistant guidelines.

## API documentation

- [UniFi Network API documentation](https://developer.ui.com/network/v10.4.57/gettingstarted) on developer.ui.com, versioned by Network Application release (use the newest version listed). The copy matching your controller's version is also under **UniFi Network > Integrations** in the controller.
- [Getting Started with the Official UniFi API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API) in the Ubiquiti Help Center, including how to create API keys.

The official documentation covers the Integration API only. The legacy `stat/*`, `rest/*` and `v2/api/*` endpoints this tool also uses (port counters, client-to-port mapping, DHCP reservations, network config, client groups) are undocumented; their fields were determined from live controller responses.
