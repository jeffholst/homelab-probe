# Web interface

The web interface is built in stages (see the roadmap in issue #160). This page covers what exists so far: the **server** (`serve`, which needs the `web` extra, and today answers only a few routes without data) and the **accounts** that will be allowed to log in, with `web-user`, the command that manages them (base install only; it does not contact the controller or read your `.env`).

## Running the server: `serve`

```bash
uv run --extra web hlp.py serve         # http://127.0.0.1:8787, until Ctrl-C
uv run --extra web hlp.py serve --port 9000 --config lab.toml
uv run --extra web hlp.py --demo serve   # the synthetic network: no controller, no .env
```

From the project checkout, `uv run --extra web` installs FastAPI and uvicorn into uv's environment;
the command line never needs them. For a standalone installation, use
`python -m pip install 'homelab-probe[web]'`, then `hlp serve`.

Without the extra it exits with code 3 and says what to install. Options:

- **`--host ADDRESS`** (default `127.0.0.1`): only a loopback address (`127.0.0.1`, `::1`, `localhost`) is accepted. Anything else is a usage error **until login exists**, so the server cannot be exposed by accident.
- **`--port PORT`** (default `8787`).
- **`--data-dir DIR`** (default: the current directory): where the server keeps its own files; used by the stages that follow.
- **`--config FILE`**: the `hlp.toml` with the `diagnose` thresholds and ignore rules, as for the other commands.

What it answers: `/healthz` (is the process up; no data and no controller read), `/readyz` (can the controller be read: 200, or 503 with a one-word reason such as `unauthorized` or `timeout`), `/api/v1/meta` (version and whether setup and login are needed), `/api/v1/platforms` and `/api/v1/openapi.json` (the API description, built into the server; the Swagger and ReDoc pages are off because they load scripts from a CDN). Only `GET` is answered. The reports are under `/api/v1/unifi` (next section).

**Until login is built in, anyone who can reach this machine can read the API.** That is why it binds loopback only. What protects it meanwhile:

| Protection | What it does |
| ---------- | ------------ |
| Loopback bind | Nobody else on the network can connect |
| `Host` check | A request whose `Host` header is not the server's own address (the loopback names and the bind address) is refused with 400, so a web page cannot reach it through DNS rebinding |
| No CORS | A browser never lets another site read an answer |
| Headers on every response | A strict content-security policy (`default-src 'none'`), `nosniff`, no referrer, no framing, `no-store`, no server banner |
| GET only, no passthrough | No route accepts a path to forward to the controller, and nothing writes to it |
| Request log | One `INFO` record per request (`server.request`, [logging](logging.md)): method, route **template** (never the path asked for, which can hold a MAC address), status and milliseconds, with a request id that is also sent back as `X-Request-ID` (an id a client sends is ignored) |

### The API: `/api/v1/unifi`

Every report is a `GET` that returns the same document as the command's `--json`, with two more keys, and takes the command's options as query parameters (`?only=wan&only=wifi`, `?days=90`, `?include_offline=true`). `{site}` is a site name, internal reference or UUID (`default` on most controllers).

| Route | Same as | Query parameters |
| ----- | ------- | ---------------- |
| `/api/v1/unifi/sites` | `info` | |
| `/api/v1/unifi/sites/{site}/diagnose` | `diagnose` | `only`, `skip` (repeatable), `since`, `no_events`, `show_ignored` |
| `.../audit` | `audit` | `show_ignored` |
| `.../firewall` | `firewall` | `all`, `search` |
| `.../topology` | `topology` | `clients` |
| `.../wifi` | `wifi` | `band`, `ap`, `min_signal` |
| `.../wan` | `wan` | `days` |
| `.../events` | `events` | `since`, `category`, `severity` (repeatable), `event`, `client`, `device`, `search`, `limit` |
| `.../events/summary` | `events --summary` | the same, without `limit` |
| `.../clients` | `query clients` | `search`, `include_offline`, `network`, `ssid`, `ap` |
| `.../devices` | `query devices` | `search`, `include_offline` |
| `.../networks`, `.../wlans` | `query networks`, `query wlans` | `search` |
| `.../ports` | `query ports` | `search`, `switch`, `down`, `errors` |
| `.../reservations` | `query reservations` | `search`, `offline` |
| `.../new-clients` | `new-clients` | `search` |
| `.../clients/{mac}` | `client` | `events`, `since` (the MAC in any spelling) |
| `/api/v1/schemas`, `/api/v1/schemas/{name}` | | the [JSON Schemas](schemas.md) |

Every route also takes `refresh=true`, which reads the controller again instead of using the cache (honoured at most every 5 seconds; a faster one is ignored and the response says so in `warnings`).

**The response** is the command's `--json` document plus `generated_at` (when the data was read from the controller, UTC) and `warnings` (what was degraded or served from the cache). The three commands whose `--json` is a bare array (`events`, `query`, `new-clients`) return `{"items": [...], "generated_at", "warnings"}` instead; `events` also has `truncated` (the limit cut the list) and `read_cap_reached`. Each response validates against the schema of its document (the API's own schemas are in `/api/v1/openapi.json`).

**Errors** are `{"error": CODE, "message": SENTENCE}` and the sentence is fixed text, never the text of an exception (that could hold the controller's address):

| Status | Code | When |
| ------ | ---- | ---- |
| 404 | `site_not_found`, `client_not_found`, `schema_not_found` | the site, the client or the schema does not exist |
| 409 | `client_ambiguous` | more than one client matches (the body has the `candidates`) |
| 422 | `invalid_parameter` | a parameter the command line would refuse, or one that is out of range |
| 500 | `settings_invalid` | the settings file cannot be used (the server log has the reason) |
| 502 | `controller_unauthorized`, `controller_forbidden`, `controller_tls`, `controller_unreachable`, `controller_error` | the controller could not be read, by kind |
| 504 | `controller_timeout` | the controller did not answer in time |

No route takes a path to forward to the controller, and none writes anything: every request that leaves is a `GET`, plus the one read-only event-log query.

### How the server reads the controller

Every read of the controller goes through one cache shared by all requests, so a browser that polls does not turn into dozens of reads per page:

- An answer is kept for **30 seconds**. N requests at once for the same data cause **one** read of the controller (the others wait for it).
- When a read fails and an older good answer is **at most 10 minutes old**, that answer is served and the response carries a warning that says when it was read (`served from the cache as of 12:03:11`). Without an older answer the error is returned.
- A failure is remembered for **5 seconds**, so a burst of requests after an outage does not become a burst of reads.
- At most `UNIFI_PARALLEL_REQUESTS` reads (default 6) are on the wire at once, however many requests are running.
- Each response says when its data was read (`generated_at` is the age of the oldest answer it used, not the time it was built).
- Only the reads the command line makes are made: GETs, and the one read-only event-log query. The window of that query is rounded to 30 seconds so that repeated requests share it. The session that talks to the controller refuses cookies, so nothing the controller sets can leak from one request into the next.

## Roles

| Role | May |
| ---- | --- |
| `viewer` | See every read-only report, change their own password |
| `admin` | Also edit settings, take snapshots and manage users |

The **last enabled administrator cannot be deleted, demoted or disabled**, by `web-user` or by anything else that uses the accounts module, so the interface cannot lock everybody out. A disabled administrator does not count.

## Managing accounts: `web-user`

```bash
uv run hlp.py web-user list
uv run hlp.py web-user add alice --role admin             # asks for the password twice
echo 'a long password here' | uv run hlp.py web-user add bob --password-stdin
uv run hlp.py web-user set-role bob --role admin
uv run hlp.py web-user disable bob
uv run hlp.py web-user enable bob
uv run hlp.py web-user reset-password bob          # asks for the new password; --password-stdin reads it from a pipe
uv run hlp.py web-user delete bob --data-dir /srv/hlp
```

| Action | What it does |
| ------ | ------------ |
| `list` | The users with their role, status, creation time and last login |
| `add NAME` | Creates a user (`--role viewer` by default) |
| `set-role NAME --role ROLE` | Changes the role |
| `disable NAME` / `enable NAME` | Stops or allows logins without deleting the account |
| `reset-password NAME` | Sets a new password: this is also the **recovery path** when an administrator forgets theirs (run it on the host, or with `docker exec` in a container) |
| `delete NAME` | Removes the account |

Options:

- **`--role viewer|admin`**: for `add` (default `viewer`) and `set-role`.
- **`--password-stdin`**: for `add` and `reset-password`: read the password from one line of standard input. Without it the command asks for the password twice, with nothing echoed. **A password is never an argument**, so it cannot end up in the process list or a shell history.
- **`--data-dir DIR`**: where `users.json` and `audit.log` are kept (default: the current directory, like `.env`, `hlp.toml` and `snapshots/`).

A username has 3 to 64 characters (letters, digits and `. _ @ -`, starting with a letter or digit) and capitals do not matter: `Alice` and `alice` are one user. A password has **at least 12** and at most 1024 characters; there are no other rules (a long phrase is better than a short mixed one).

Exit codes are the usual ones: `0` done, `3` refused (a user that does not exist or already exists, a password that is too short, the last administrator, an accounts file that cannot be read), `64` a command-line usage error. `--demo` refuses the command, since it works on your own files.

## The files

Both live in the data directory, readable by the owner only (`0600`, and `0700` for a directory the command creates), and are git-ignored.

- **`users.json`**: the accounts. Each has `username`, `role`, `password`, `created_at`, `last_login` and `disabled`. A password is stored only as `scrypt$N$r$p$salt$hash` (a per-user random salt; the cost parameters travel with the hash, so they can be raised later and a user's hash is **upgraded at their next login**). Every write is atomic (a temporary file, then a rename) under a lock file `users.json.lock`, so a crash never leaves half a file and two writers never lose each other's change. A program that has the file open notices when it changed and reads it again. Existing files are tightened to owner-only permissions when read. A damaged file is refused with a reason and never overwritten.
- **`audit.log`**: one JSON line per change: `time`, `event`, `actor` (`cli:` and the operating-system user), the `user` and, where it applies, the `role`.

  | Event | When |
  | ----- | ---- |
  | `user.added` | `add` |
  | `user.role_changed` | `set-role` |
  | `user.disabled`, `user.enabled` | `disable`, `enable` |
  | `user.password_reset` | `reset-password` |
  | `user.password_upgraded` | a login replaced an old hash with one made with the current parameters |
  | `user.deleted` | `delete` |

  A password never reaches the file (a field named like a secret is hidden whatever it holds), and a refused change is not recorded because nothing changed. A failed audit write is reported and the corresponding account change is rolled back. Login and settings events arrive with the features that make them. The same records are also logged at `INFO` as `audit.event` on `homelab_probe.audit` ([logging](logging.md)).

  The log **rotates by size, never by age**: `AUDIT_LOG_MAX_MB` (default 5, 1 to 1024) is the size of one file and `AUDIT_LOG_FILES` (default 10, 2 to 1000) how many are kept in all, the current `audit.log` and `audit.log.1`, `audit.log.2`... `web-user` reads both from the environment (not from `.env`, which it does not read); they are also settings of `.env` for the server to come.

## What is not here yet

The login page, sessions, the report routes and the web app come with later stages of the roadmap. The accounts module already has the interface they will use: an `Authenticator` that turns a username and password into a `Principal(username, role, source)`, with `LocalAccounts` (this page's accounts) as the first implementation. A wrong password, an unknown user and a disabled one all take the same work and give the same answer, so the answer does not reveal which usernames exist. Authentication checks the current account record and records the login under the same file lock, so a concurrent disable, role change or deletion cannot return a stale principal.
