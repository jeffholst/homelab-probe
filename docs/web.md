# Web interface

The web interface is built in stages (see the roadmap in issue #160). What exists so far is the **backend**, with no screens yet: the **server** (`serve`, which needs the `web` extra) with its login, roles and CSRF protection, the read-only report API under `/api/v1/unifi` and a guided first-run setup API; and the **accounts** that may log in, managed with `web-user` (base install only; it does not contact the controller or read your `.env`). The web app that uses these comes next.

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

- **`--host ADDRESS`** (default `127.0.0.1`, this machine only): another address makes the server reachable from the network, see [Reaching it from other machines](#reaching-it-from-other-machines).
- **`--allowed-host NAME`** (repeatable): a name the server is reached by, such as `hlp.lan` or `192.168.1.5` (a port is ignored). A request whose `Host` header is none of these, the loopback names and the bind address is refused. **Required with `--host 0.0.0.0` or `::`**. Wildcards, schemes and paths are usage errors: the list is never open.
- **`--forwarded-allow-ips IPS`**: believe `X-Forwarded-For` and `X-Forwarded-Proto` from these reverse proxies (addresses or networks, comma-separated) and from nobody else; by default from none. `*` and a network of prefix length 0 (`0.0.0.0/0`, `::/0`), which also mean "everyone", are refused.
- **`--allow-public-controller`**: let the [guided setup](#first-run-setup-a-server-with-no-settings) connect to a controller on a **public** address (by default it refuses them, so the setup cannot be used to probe other people's machines). It changes nothing for a server that already has settings.
- **`--read-only`**: write no file on this machine. The settings editor ([below](#settings-apiv1settings)) and the setup's `finish` answer `403 read_only`; logging in, the sessions in memory and the audit log keep working. `/api/v1/meta` and `GET /api/v1/settings` say `read_only: true` so a page can hide its edit buttons. A test lists every route that is not a read, and fails on one that neither writes nothing nor is refused here.
- **`--port PORT`** (default `8787`).
- **`--data-dir DIR`** (default: the current directory): where the server keeps its own files (the accounts, the audit log) and, when no `--env-file` or `HLP_ENV` names one, the `.env` it reads ([first-run setup](#first-run-setup-a-server-with-no-settings)).
- **`--config FILE`**: the `hlp.toml` with the `diagnose` thresholds and ignore rules, as for the other commands.

What it answers: `/healthz` (is the process up; no data and no controller read), `/readyz` (can the controller be read: 200 or 503 and `{"ready": ...}`, the reason is in the server log), `/api/v1/meta` (version, that login is required, and whether this request came over HTTPS or from a loopback name, which is what a login page needs to decide on a warning), then, after a login, `/api/v1/platforms` and `/api/v1/openapi.json` (the API description, built into the server; the Swagger and ReDoc pages are off because they load scripts from a CDN). The report and schema routes answer only `GET`; the only `POST`s are the login and the logout (CSRF below). The reports are under `/api/v1/unifi`.

**Every route except five needs a login.** Only `/`, `/healthz`, `/readyz` (yes or no, nothing more), `/api/v1/meta` and the login itself answer without one, from `127.0.0.1` as well. A route nobody declared anything about needs a login too (the rule is the default of the whole application, and a test pins the list of public routes). A configured server **without an enabled administrator** starts in the limited [admin mode](#first-run-setup-a-server-with-no-settings) (or create one first: `hlp web-user add NAME --role admin`), and one with no settings at all starts the guided setup, and by default it binds only a loopback address (the options below change that on purpose). What protects it:

| Protection | What it does |
| ---------- | ------------ |
| Login | A session cookie from `POST /api/v1/auth/login` (below) for everything but the five routes |
| Loopback bind | By default only this machine can connect; another `--host` is your decision (below) |
| `Host` check | A request whose `Host` header is not one of the loopback names, the bind address or an `--allowed-host` is refused with 400, so a web page cannot reach it through DNS rebinding. The list is never a wildcard: an entry such as `*` or `*.lan` is refused wherever it comes from |
| CSRF | Every `POST`, `PUT`, `PATCH` and `DELETE` needs an `Origin` that names the server's own `Host` **and** the session's token in `X-CSRF-Token`; its body must be JSON. A script that sends neither is meant to use the command line |
| No CORS | A browser never lets another site read an answer |
| Headers on every response | A strict content-security policy (`default-src 'none'`), `nosniff`, no referrer, no framing, `no-store`, no server banner |
| GET only, no passthrough | No report route accepts a path to forward to the controller, and nothing writes to it |
| Request log | One `INFO` record per request (`server.request`, [logging](logging.md)): method, route **template** (never the path asked for, which can hold a MAC address), status and milliseconds, with a request id that is also sent back as `X-Request-ID` (an id a client sends is ignored) |
| Proxy headers only from named proxies | The address of a client is the connection's own unless the connection comes from a proxy named with `--forwarded-allow-ips`; `X-Forwarded-For` from anyone else is ignored (the throttle and the audit log depend on it) |

### Reaching it from other machines

A login sends a password, and with plain HTTP anyone on the path can read it. So there are two good ways to reach the server from another machine, and one you should not use:

1. **An SSH tunnel** (nothing to configure on the server): `ssh -L 8787:127.0.0.1:8787 user@server`, then browse to `http://localhost:8787`.
2. **A reverse proxy that terminates TLS**, on the same machine or another one. Name it with `--forwarded-allow-ips` so the client address and the HTTPS scheme come through (the cookie becomes `Secure` with the `__Host-` prefix), and name the host your users type with `--allowed-host`:

   ```bash
   uv run --extra web hlp.py serve --forwarded-allow-ips 127.0.0.1 --allowed-host hlp.example.lan
   ```

   Caddy (certificates included):

   ```text
   hlp.example.lan {
       reverse_proxy 127.0.0.1:8787
   }
   ```

   nginx:

   ```text
   location / {
       proxy_pass http://127.0.0.1:8787;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $remote_addr;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

   The proxy must **replace** (not append to) `X-Forwarded-For`, as above, and must pass `Host` and the browser's `Origin` unchanged.
3. **Not recommended: plain HTTP on the network** (`--host 192.168.1.5` or `--host 0.0.0.0 --allowed-host ...` with no proxy). The server says so when it starts, and `/api/v1/meta` returns `"https": false, "loopback": false` so a web app can show a banner above the login form; the password travels in clear text.

Binding every address (`0.0.0.0`, `::`) is only accepted with at least one `--allowed-host`; binding one address or name adds it to the allowed hosts by itself.

### Logging in: `/api/v1/auth`

- **`POST /api/v1/auth/login`** with `{"username": ..., "password": ...}` (JSON, and an `Origin` header, which a browser sends) returns `{username, role, csrf_token, idle_seconds_left, session_seconds_left}` and sets the session cookie. The only error for a wrong password, an unknown user or a disabled one is `401 invalid_credentials` ("Invalid username or password."), and they cost the same work. The CSRF token goes into `X-CSRF-Token` on every unsafe request.
- **`GET /api/v1/auth/me`** says who is logged in and how long the session has left; **`POST /api/v1/auth/logout`** ends it.
- **The cookie** is `HttpOnly`, `SameSite=Strict`, `Path=/`, with no `Domain` and no expiry. Over HTTPS it is also `Secure` and named `__Host-hlp_session` (a browser then refuses to let a subdomain or a plain-HTTP page replace it); over HTTP it is `hlp_session`, and each name is accepted only on its own scheme. Its value is 32 random bytes; the server keeps only a hash of it, in memory, so a restart logs everybody out. There is no "remember me".
- **A session ends** after `SESSION_IDLE_MINUTES` without a request (default 30) or `SESSION_MAX_HOURS` after the login (default 12), and **on the next request after its user's password was changed or reset, the user was disabled or deleted, or the role changed**, whether that was done by the `web-user` command or anything else. A new login ends the session it replaces, and each user keeps at most 10 sessions.
- **Guessing is slowed down**, not blocked for good. Failures are counted per address and per username; the first three cost nothing, then the wait doubles (2, 4, 8 ... seconds) up to 5 minutes for an address and **30 seconds for a username**, so nobody can lock a real user out for long. While a wait lasts every attempt, right password or wrong, gets `429 too_many_attempts` with `Retry-After`. A success clears the counts.
- **The audit log** gets `auth.login` (with the role), `auth.login_failed`, `auth.throttled` (once, when a wait begins) and `auth.logout`, each with the user and the address. A username that does not exist is written as `(unknown user)`, never as typed (it may be a password typed in the wrong box). A login that cannot be written to the audit log does not happen (500); a failure is still refused if its entry cannot be written. Passwords and session ids never reach the log or the audit trail.

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

### Settings: `/api/v1/settings`

The thresholds and ignore rules of `diagnose` and `audit` are the `hlp.toml` the command line reads, and this is the same file: a change here is a change there. It is the file named with `--config`, else **`hlp.toml` in the data directory** (`--data-dir`; with the default, the current directory, as before). The report routes read it from there too.

| Method and path | What it does |
| --------------- | ------------ |
| `GET /api/v1/settings` | An administrator: the effective `thresholds` with their `defaults` and a `provenance` for each (`default`, `file` or `environment`), the `ignore` rules (`code`, `subject`, `message`, `reason`, `until`, `expired`), the valid finding `codes`, whether the file `exists`, a `version` and `read_only`. Notification destinations appear only as `notifications: {ntfy, webhook, email}` flags: never a URL, token or address |
| `PUT /api/v1/settings` | An administrator, with the CSRF token. Body: `version` (from the `GET`), `thresholds` (a map: a number sets that key, `null` removes it so the default applies again; keys not named stay) and/or `ignore` (the **whole** list of rules). Answers with the new document |

- **One loader.** The new text is written to a temporary file and loaded with the function every command uses, so a value it refuses is a `422 invalid_settings` with **its own message** (`[thresholds] resource_warn_pct must be between 0 and 100`, `[[ignore]] #1: unknown code 'device.offlin' (did you mean 'device.offline'?)`) and nothing is written. A value of the wrong JSON type (`true`, a string) is a `422` too.
- **Comments stay.** The file is edited in place (`tomlkit`, in the `web` extra): comments, blank lines and the order of keys are kept, and an ignore rule that did not change keeps its table and the comments around it. A new file starts from the commented stub `hlp init` writes. The comments of a rule you remove go with it.
- **Nobody is overwritten.** `version` is a hash of the file (`absent` for no file). If the file is different when the `PUT` arrives, or becomes different while it works, the answer is `409 settings_changed` and nothing is written: load it again. Two changes with the same version cannot both win.
- **Safe writes.** The file is replaced in one step with its permissions kept (a new file is owner-only), the old content is kept as `hlp.toml.bak`, and a symbolic link is left alone (`409`). A `PUT` that changes nothing writes nothing: no new version, no `.bak`, no audit entry. A file the loader refuses cannot be edited here (`409 settings_file_invalid`; `GET` says `500 settings_invalid`): fix it by hand, `hlp diagnose` shows the reason.
- **Environment wins, and the API says so.** A threshold that an environment variable manages would show `provenance: "environment"` and a write to it is a `409 environment_managed` naming the variable, instead of claiming a change that would be ignored. No threshold is managed that way today (the thresholds live only in `hlp.toml`), so every value is `default` or `file`.
- **Audit.** `settings.updated` with the user, the names of the thresholds that changed and the number of ignore rules (`"2 rule(s)"` or `"unchanged"`), never a rule's text.

### Snapshots: `/api/v1/unifi/sites/{site}/snapshots` and `/diff`

The saved snapshots of [`snapshot` and `diff`](inventory.md#snapshots-and-diff), kept per site in `snapshots/<site id>/` of the data directory (the older ones that were saved straight into `snapshots/` are listed for the site their record names).

| Method and path | What it does |
| --------------- | ------------ |
| `GET .../snapshots?limit=50` | Anyone logged in: the newest saved snapshots of the site (`name`, `captured_at`, how many `devices`, `clients` and `reservations`), newest first, and `total`. A file that cannot be read as a snapshot is listed with `readable: false` |
| `POST .../snapshots` | An administrator, with the CSRF token: save a snapshot of the network **now** (the cache is refreshed first, at most every 5 seconds) with the code `snapshot` uses. Body `{"keep": N}` (optional) afterwards keeps only the newest N of this site, as `snapshot --keep` does. Answers `201` with the `snapshot` summary, the `removed` names, `generated_at` and `warnings` |
| `GET .../diff?old=NAME&new=NAME` | Anyone logged in: what changed, in the document `diff --json` prints (plus `old`, `new`, `generated_at`, `warnings`). `old` defaults to the newest snapshot of the site, `new` to the network right now |

- **Names, not paths.** A snapshot is named by its bare file name from the list. A name with a directory part, a name that is not the kind `snapshot` writes, and the snapshot of another site are a `404 snapshot_not_found`; no path a caller sends reaches the disk.
- **The site is looked up first** (one cached request), because the directory is named by the site's id: when the controller cannot be read, these routes answer `502` or `504` like the other site routes.
- **Links and permissions.** A `snapshots/` directory or a site directory that is a symbolic link is never followed (`500 snapshots_unsafe`), and a snapshot file that is a link is neither listed nor reachable. A save makes the directories owner-only (`0700`, tightening ones that were not), like the setup does. A name must be a real date and time, as `snapshot` writes it.
- **The write** is refused by `serve --read-only` (`403 read_only`), is audited as `snapshot.saved` (the file name and the counts) and fails with a fixed `500 snapshot_not_written` when the disk does. The file is owner-only in a `0700` directory, as the command writes it.

### Users: `/api/v1/users`

The accounts of [`web-user`](#managing-accounts-web-user), the same `users.json`, the same rules (a user name of 3 to 64 characters, a password of at least 12, the roles `viewer` and `admin`). **Everything here is for administrators**, with the CSRF token, and `serve --read-only` refuses every change.

| Method and path | What it does |
| --------------- | ------------ |
| `GET /api/v1/users` | Every user (`username`, `role`, `disabled`, `created_at`, `last_login`) and the `total`. Never a password or a hash |
| `POST /api/v1/users` | Add a user: `username`, `password`, `role` (default `viewer`). `201`; `409 user_exists`; `422 invalid_user` with a fixed sentence for the rule that was broken |
| `PATCH /api/v1/users/{username}` | Change the `role` and/or `disabled` in one step. `404 user_not_found`; nothing is changed if the result would break the rule below |
| `POST /api/v1/users/{username}/password` | Reset the password (`password` in the body) |

- **The last enabled administrator can never be demoted or disabled** (`409 last_administrator`), on any path and for a combined change as a whole, because the account file enforces it under its lock.
- **A change takes effect at once.** A session re-checks its account on every request, so a user who is disabled, or whose role or password changed, is out on their next request and logs in again (a role change ends the sessions too, so nobody keeps an administrator's session after a demotion). An administrator who resets their own password logs themselves out.
- **Audit.** The events of the command line (`user.added`, `user.role_changed`, `user.disabled`, `user.enabled`, `user.password_reset`) with the administrator as the actor and the address of the request, and `user.updated` (with `role` and `disabled`) when one request changes both. A change is always **one** entry, so if it cannot be written the change is rolled back (`500 audit_unavailable`) and no record claims half of it. A password is never in an answer, an audit entry or a log.
- **No deleting** in the API: `hlp web-user delete`. A disabled user cannot log in.

### How the server reads the controller

Every read of the controller goes through one cache shared by all requests, so a browser that polls does not turn into dozens of reads per page:

- An answer is kept for **30 seconds**. N requests at once for the same data cause **one** read of the controller (the others wait for it).
- When a read fails and an older good answer is **at most 10 minutes old**, that answer is served and the response carries a warning that says when it was read (`served from the cache as of 12:03:11`). Without an older answer the error is returned.
- A failure is remembered for **5 seconds**, so a burst of requests after an outage does not become a burst of reads.
- At most `UNIFI_PARALLEL_REQUESTS` reads (default 6) are on the wire at once, however many requests are running.
- Each response says when its data was read (`generated_at` is the age of the oldest answer it used, not the time it was built).
- Only the reads the command line makes are made: GETs, and the one read-only event-log query. The window of that query is rounded to 30 seconds so that repeated requests share it. The session that talks to the controller refuses cookies, so nothing the controller sets can leak from one request into the next.

## First-run setup: a server with no settings

`hlp serve` does not need a `.env` to start. When **nothing is configured** (neither `UNIFI_URL` nor `UNIFI_API_KEY`, in the environment or the `.env`), the server starts in the **setup mode** instead of exiting, prints a **setup token** once on its console and waits for the guided setup:

```
Not configured (UNIFI_URL is not set).
Serving on http://127.0.0.1:8787 (Ctrl-C to stop).
Not set up: open the server in a browser to finish the setup with this token: 7Qk...
```

Where the settings come from (first match wins): `--env-file FILE`, then `HLP_ENV`, then the `.env` in the **data directory** (`--data-dir`, the current directory by default, which is where `hlp init` and the wizard write it); the real environment beats the file, as everywhere. They are read without changing the process environment. A file that is named and does not exist is still an error, and so are settings that exist but are broken (a plain `http://` address, one of the two missing, a bad value): that is not a first run, and it fails loudly as it always did. A server that **has** usable settings but no administrator starts in the **admin mode**: the same token may create the first administrator, and nothing else is available.

In the setup mode every endpoint answers `503` with `{"error": "not_configured"}`, except the public ones (`/`; `/healthz`; `/readyz`, which says `503` since there is no controller to read; `/api/v1/meta`, which says `needs_setup: true` and `setup_mode: "setup"`; and the login) and the setup routes below. The login still works, and an existing account can log in, but its session opens nothing except the setup routes (and only for an administrator): every other route stays `503` until the setup is finished. Nothing is read from a controller until you test the connection yourself.

| Route | What it does |
| ----- | ------------ |
| `GET /api/v1/setup/status` | The draft (never the API key, only `api_key_set`), the mode and why, and the sentence that turns certificate checking off |
| `POST /api/v1/setup/draft` | Change `url`, `site`, `api_key`, `verify` (`true`, `pin` or `false`), with `fingerprint` or `confirm` where they are needed. Each value is checked with the rules every command uses and the request changes everything or nothing (`422` `invalid_setting`, with the `setting`). A new address forgets the certificate and the verification choice made for the old one |
| `POST /api/v1/setup/certificate` | Fetch the certificate the controller shows and test that it would be accepted for that address; answers with its SHA-256 fingerprint |
| `POST /api/v1/setup/connection` | The controller checks of `hlp doctor` on the draft: is it reachable, is the key accepted, which endpoints answer; when they pass, also the controller's `sites` (name, reference, id) for a site picker |
| `POST /api/v1/setup/preview` | `diagnose` without the event log, on the draft: the summary and the first 25 findings |
| `POST /api/v1/setup/notifications` | A dry run of the notification destinations in the draft (`notify` in `draft`: the `NOTIFY_*` settings, checked together; a blank value removes one): what is configured and the text that would be sent. Nothing is sent, and the values are never returned (`status` lists only the names) |
| `POST /api/v1/setup/finish` | Save the settings, create the first administrator (`username`, `password`) and leave the setup mode, without a restart |

While no administrator exists, all of them need the token in an **`X-Setup-Token`** header (and, like every `POST`, an `Origin` that names the server). **As soon as an enabled administrator exists** (`hlp web-user add NAME --role admin`, looked at on every request) the token is no longer printed or accepted, and the routes need an administrator's session instead (log in, then send the session and its CSRF token as everywhere). The token is 24 random bytes, new at every start, held only in memory; set `HLP_SETUP_TOKEN` (at least 16 characters, in the environment, never in a `.env`: `doctor` flags it there) to choose it, and it is then not printed. A wrong token is refused with the same `401` whatever it was, counted and slowed down like a wrong password (3 free attempts, then a growing wait: `429` with `Retry-After`) and written to the audit log; the token itself never is. 

**Finishing.** `finish` needs a draft whose connection test passed (any later change to the draft needs a new test: `409 not_tested`). The first administrator is checked before anything is written (a user name of 3 to 64 characters, a password of at least 12; `422 invalid_admin`). Then the files are written to the data directory with the same engine as `hlp init`: `.env` (owner-only, merged into one that exists, which is kept as `.env.bak`), `hlp.toml` if there is none, `snapshots/` (`0700`) and, for a pinned certificate, `certs/controller.pem` (`0600`; `UNIFI_VERIFY_SSL` is set to its absolute path). The administrator is created, the configuration is read again exactly as the server read it at start (`--env-file`, `HLP_ENV`, the data directory, `--timeout`, `--parallel`, `--site`), the controller service starts and the setup mode ends: the token stops working, `/api/v1/meta` says `needs_setup: false` and the new administrator can log in. Nothing is restarted. The answer lists what was written; it never holds the API key or the password. If the administrator cannot be created after the files were written, or the new settings cannot be loaded, the answer is a `500` that says so and the mode does not change.

**When the settings cannot be saved here** nothing is written (not the administrator either) and `finish` answers `{"finished": false, "written": false, "reason": ...}` with the finished **`.env`** (`env`) and a **compose snippet** (`compose`, values quoted and `$` doubled) to copy. The API key and every notification setting in them are placeholders (`your-api-key-here`, `your-notify-ntfy-url-here`, ...; `placeholders` lists the names): secrets are never sent back, so paste your own. For a pinned certificate the answer also holds the certificate (`certificate`) to save, and `UNIFI_VERIFY_SSL` is `/path/to/controller.pem`. The `reason` is `environment` (a setting is set in the environment to something else, so a saved `.env` would lose; `environment_names` lists which), `env_file_named` (the settings file is named by `--env-file` or `HLP_ENV`, not the data directory) or `not_writable` (a read-only volume or a link: `detail` says which). Create the administrator with `hlp web-user add` wherever the data directory is writable.

**The admin mode.** A server that has usable settings but no enabled administrator (for example a new data directory, or settings from the environment) starts with the token printed as above and offers only `status` and `finish`; the other setup routes answer `409 step_unavailable`, everything else `503`. `finish` with a `username` and `password` creates the first administrator, starts the controller service and ends the mode. You can instead run `hlp web-user add NAME --role admin --data-dir DIR` and restart.

**What is typed stays on the server.** The draft is held in memory; the API key is registered with the log redaction the moment it arrives and is only reported as "set". The audit log gets `setup.token_failed`, `setup.draft_changed` (which settings changed and the controller address, never a value of a secret), `setup.certificate_fetched` (the fingerprint and whether it is usable), `setup.connection_tested`, `setup.preview_run`, `setup.notifications_checked`, `setup.finish_fallback` (with the reason) and `setup.finished` (with whether an administrator was created), with the address of the caller; the new administrator is a `user.added` entry as everywhere. No password, key or notification value is ever written.

**Certificates.** A controller at home usually has a self-signed certificate. `certificate` fetches the one it shows (this handshake is not verified, since the point is to see an unknown certificate, and nothing is sent over it, so no key can reach a server that was not verified), then tests that it would be accepted **for that address** the way the client will verify it. Accept it by sending its fingerprint back with `verify: "pin"`: compare the fingerprint with the one your controller shows (its web page, in the browser's certificate details) first, since whoever answers at that address could be somebody else. A certificate that would not be accepted (it is not valid for the address, or a certificate in the chain cannot be a CA) cannot be pinned, and the reason is given. `verify: "false"` sends the key without verifying the controller and needs `confirm` to hold the sentence from `status` exactly. The settings written at the end of the setup name the pinned certificate as `UNIFI_VERIFY_SSL`.

**What the server connects to.** The address must be `https://`, without credentials, and must not be one no controller has: unspecified (`0.0.0.0`), multicast, link-local (this includes the cloud metadata address `169.254.169.254`) or reserved addresses are always refused, and a **public** address is refused too unless the server was started with `--allow-public-controller` (the guided setup is for a controller on your own network). A name is resolved first and **every** address it gives must pass; the certificate step connects to the address that was checked, not to a second lookup. The connection test and the preview use a short timeout and **do not follow redirects**, because a redirect would carry the API key to another host. (They let the HTTP library resolve a name again, so a name whose answer changes between the two lookups is not caught; give the address if that matters.) They answer with fixed words, never the text of an error: an address, a key or a response body does not reach the browser or the log.

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

The scheduler, application backup and restore, finding triage and notes ([#186](https://github.com/jeffholst/homelab-probe/issues/186)), and the web app (the screens of the setup wizard among them), come with later stages of the roadmap. The login uses the interface the accounts module was built for: an `Authenticator` that turns a username and password into a `Principal(username, role, source)`, with `LocalAccounts` (this page's accounts) as the first implementation. A wrong password, an unknown user and a disabled one all take the same work and give the same answer, so the answer does not reveal which usernames exist. Authentication checks the current account record and records the login under the same file lock, so a concurrent disable, role change or deletion cannot return a stale principal.
