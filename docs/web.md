# Web interface

The web interface is built in stages (see the roadmap in issue #160). What exists so far is the **backend**, plus the first screens of the web app (login, the navigation shell, a home page and a profile page, built in `web/` and not yet served by the server): the **server** (`serve`, which needs the `web` extra) with its login, roles and CSRF protection, the read-only report API under `/api/v1/unifi` and a guided first-run setup API; and the **accounts** that may log in, managed with `web-user` (base install only; it does not contact the controller or read your `.env`). The web app that uses these is being built in `web/` (its foundation, with login, the shell and the themes, is in the repository; the server does not serve it yet, see [The web app](#the-web-app-web)).

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
- **`--scheduler`**: run `diagnose` (with notifications) and take snapshots on a timer inside the server, see [The scheduler](#the-scheduler). It writes files, so it cannot be combined with `--read-only` or `--demo`.
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

- **`POST /api/v1/auth/login`** with `{"username": ..., "password": ...}` (JSON, and an `Origin` header, which a browser sends) returns `{username, role, csrf_token, idle_seconds_left, session_seconds_left, can_change_password}` and sets the session cookie. The only error for a wrong password, an unknown user or a disabled one is `401 invalid_credentials` ("Invalid username or password."), and they cost the same work. The CSRF token goes into `X-CSRF-Token` on every unsafe request.
- **`GET /api/v1/auth/me`** says who is logged in, how long the session has left and `can_change_password` (true for the local accounts, whose passwords are kept here; an authenticator that keeps them elsewhere would say false); **`POST /api/v1/auth/logout`** ends the session. The login answers with the same document.
- **The cookie** is `HttpOnly`, `SameSite=Strict`, `Path=/`, with no `Domain` and no expiry. Over HTTPS it is also `Secure` and named `__Host-hlp_session` (a browser then refuses to let a subdomain or a plain-HTTP page replace it); over HTTP it is `hlp_session`, and each name is accepted only on its own scheme. Its value is 32 random bytes; the server keeps only a hash of it, in memory, so a restart logs everybody out. There is no "remember me".
- **A session ends** after `SESSION_IDLE_MINUTES` without a request (default 30) or `SESSION_MAX_HOURS` after the login (default 12), and **on the next request after its user's password was changed or reset, the user was disabled or deleted, or the role changed**, whether that was done by the `web-user` command or anything else. A new login ends the session it replaces, and each user keeps at most 10 sessions.
- **Guessing is slowed down**, not blocked for good. Failures are counted per address and per username; the first three cost nothing, then the wait doubles (2, 4, 8 ... seconds) up to 5 minutes for an address and **30 seconds for a username**, so nobody can lock a real user out for long. While a wait lasts every attempt, right password or wrong, gets `429 too_many_attempts` with `Retry-After`. A success clears the counts.
- **The audit log** gets `auth.login` (with the role), `auth.login_failed`, `auth.throttled` (once, when a wait begins) and `auth.logout` (and `auth.password_failed` with `user.password_changed`, see [changing your own password](#changing-your-own-password-post-apiv1authpassword)), each with the user and the address. A username that does not exist is written as `(unknown user)`, never as typed (it may be a password typed in the wrong box). A login that cannot be written to the audit log does not happen (500); a failure is still refused if its entry cannot be written. Passwords and session ids never reach the log or the audit trail.

### Changing your own password: `POST /api/v1/auth/password`

Any logged-in user, a viewer included, may change their own password; no administrator is needed (an administrator resetting **someone else's** password uses [`/api/v1/users/{username}/password`](#users-apiv1users)). The body is `{"current_password": ..., "new_password": ...}` (JSON, the CSRF token and the `Origin` like every unsafe request).

- **The current password is checked**, and every wrong one counts in the **same throttle as a failed login** (per address and per user name, the same waits), so a session left open cannot be used to guess it: while a wait lasts the answer is `429 too_many_attempts` with `Retry-After`, whatever the passwords are. A wrong one is `422 invalid_current_password` ("The current password is wrong.").
- **The new password follows the rule of every other path** (at least 12 and at most 1024 characters): `422 invalid_password` with the sentence of the rule that was broken. The rule is checked **before** the current password, so such a request says nothing about whether the current one was right and does not count as a failed guess. A body with anything but the two passwords is `422 invalid_parameter`; a password is never part of an answer.
- **The other sessions of the user end and yours goes on.** The answer is the document of `GET /api/v1/auth/me` for a **new session**: the cookie value and the CSRF token change (`csrf_token` in the answer is the one to send from now on, and the old one is refused with `403 csrf_token`), while the start of the login stays, so changing the password does not lengthen it. Sessions of other users are not touched.
- **Refusals.** `serve --read-only` answers `403 read_only` (the accounts are a file on this machine); an account whose `can_change_password` is false answers `403 password_not_changeable`; `500 audit_unavailable` means the change was not made because its audit entry could not be written.
- **Audit.** `user.password_changed` (the user as the actor and as `user`, with the address), written in the same step as the change; `auth.password_failed` for a wrong current password and `auth.throttled` when a wait begins, as for a login. No password or hash is in the log, the audit trail or an answer. The `last_login` of the account is not changed: this is not a login.

### The API: `/api/v1/unifi`

Every report is a `GET` that returns the same document as the command's `--json` (the dashboard summary, below, has no command), with two more keys, and takes the command's options as query parameters (`?only=wan&only=wifi`, `?days=90`, `?include_offline=true`). `{site}` is a site name, internal reference or UUID (`default` on most controllers).

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
| `.../dashboard` | none: the [dashboard summary](#dashboard-summary-apiv1unifisitessitedashboard) | |
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

### Status and the test notification: `/api/v1/status`

`GET /api/v1/status` says what **this server has seen**, and when. It makes no request of its own to the controller (a page that polls it would otherwise read the controller): it reports the outcome of the reads the server made for its other routes and its scheduler. A server that has read nothing yet says `unknown`, never `ok`.

| Part | What it says |
| ---- | ------------ |
| `controller` | `state`: `ok`, `unreachable` (no answer: connection or timeout), `certificate` (TLS), `key_rejected` (401 or 403) or `unknown`, from the most recent reads, and `last_read_ok_at`. An error status from one endpoint is still an answer, so it does not make the controller `unreachable` |
| `data` | `stale`: the last answer served was an older one, because a read failed, and no read has succeeded since; `warnings`: how many warnings the last response carried (an optional read failed); `last_response_at` |
| `scheduler` | `enabled` and, for each job, `last_run_at`, `result`, `last_success_at` and `next_run_at` |
| `read_only` | whether the server was started with `--read-only` |

- **Who sees what.** Any logged-in user gets the table above. An **administrator** also gets: `controller.last_failure` and `last_failure_at`; each job's fixed `reason` and `duration_ms`; `notifications` (the destinations by **kind** only, and the outcome of the last delivery to each: when, whether, and a fixed reason such as `HTTP 403`); `storage` (the data directory, `audit_log`, `snapshots/` and the free disk space, each `ok`, `absent`, `missing`, `not_writable`, `unsafe` for a symbolic link, `low` for under 100 MB, or `unknown`); and `problems`, what is wrong in fixed words (`controller:unreachable`, `data:stale`, `scheduler.diagnose:failed`, `notifications.ntfy:failed`, `storage.disk:low`...). Never an address, a path, a URL, a token or a message.
- **Deliveries are those this server made** (the scheduler's and the test's). A cron job is another process: its failures are in its own output, not here.

`POST /api/v1/notifications/test` (an administrator, with the CSRF token) sends **one fixed test message** to every configured destination, through the delivery code of `diagnose --notify`, and answers `{"delivered": true, "results": [{"destination": "ntfy", "delivered": true, "reason": "HTTP 200"}]}`: a kind and a fixed reason for each, never a URL or a token. It is **never sent by itself**, at most once every 10 seconds (`429 too_soon`), and `409 no_destination` when none is configured. A webhook receives the usual payload with `events` empty and `"test": true`. It writes no file, so `--read-only` does not stop it; it is audited as `notification.tested` (the kinds and how many were delivered).

### Findings and triage: `/api/v1/unifi/sites/{site}/findings`

`GET .../findings` is the `diagnose` document of the site made ready for a person who has to decide what to look at first, and `PUT .../findings/{id}/triage` records what an administrator did about one. Both roles read; only an administrator changes.

Each finding has:

| Field | What it says |
| ----- | ------------ |
| `id` | A stable 16-character id: a hash of the check (`code`) and of what it is about, the device's **MAC when the finding has one** (so a renamed device keeps its state, whatever spelling the MAC has) or else the subject text (so a finding about something with no MAC is a different finding after a rename). It says nothing about the network |
| `rank`, `priority` | The place in the order and **what put it there**: `reasons` (for example `critical severity`, `the check affects the network as a whole`, `first recorded 10 days ago`), the `scope` of the check (`network`, `device`, `link`, `wireless`, `client`, `events`) and a `score`. The order is: findings nobody has looked at, then acknowledged, then snoozed; within each, severity, then scope, then how long the finding has been known **where there is a record**; then code and subject, so the same input is always in the same order |
| `triage` | `state` (`open`, `acknowledged`, `snoozed`), who, when, the `until` of a snooze and a short plain-text `note` |
| `first_seen_at`, `limitations` | When it was first recorded, or `null`, and then a limitation says that how long it has been happening is **unknown**: there is no invented history. Another limitation says when the read was partial (`no_events`, or an optional read failed) |
| `group` | `null`, or the finding that **probably explains this one**, with the evidence for the claim (see below) |
| `next_checks`, `docs` | **General** guidance for this kind of check (labelled as such in `next_checks_are`) and a link to the finding-code table; not advice specific to the finding |

The top level has `summary` (the counts by severity, as `diagnose` has them, and by triage state), `complete` (every check ran and every read worked: false with `no_events` or when an optional read failed), `triage_available` (false, with a limitation, when the triage file cannot be read: the findings are still shown) and the usual `generated_at` and `warnings`. **A read never writes anything.**

- **A state never hides or resolves a finding.** An acknowledged or snoozed finding stays in the list, after the open ones. **Acknowledged is not resolved.** `PUT` takes `state` (`open` to reopen, `acknowledged`, or `snoozed` with `until`, the last day as `2026-10-10`, at most a year ahead; a snooze ends at the start of the next day UTC and the finding is open again, with nobody writing anything) and an optional `note`. It works only on a finding that **is there now** (`404 finding_not_found`).
- **When a finding clears.** The scheduler ([below](#the-scheduler)) records what each diagnose run finds (first and last seen) and **drops the entry of a finding that a complete read of every check no longer shows**, acknowledged or not. A failed or partial read changes nothing, so it can never imply that something was fixed; a finding that comes back after clearing is a new one, open. Without the scheduler, an acknowledgment lasts until its snooze ends or an administrator reopens it, and "first seen" is known only for findings somebody triaged.
- **Where it is kept.** `snapshots/<site id>/triage.json`, one file per site beside the snapshots and the notification state (so it is in the Docker volume that already covers `snapshots/`), owner-only, written atomically under a lock, never through a symbolic link, and refused if it names another site. It holds the id, the code, the state, who and when, and the first and last seen times: **no name, MAC or address.** At most 5000 findings are tracked per site.
- **Audit.** `triage.changed` with the id, the code, the new state and the `until`: never a name, a MAC or the note. `--read-only` refuses a change (the scheduler cannot run then, so nothing else writes the file).
- **Findings that share a cause.** A finding has a `group` only where the controller's data supports it, never as a guess, and **nothing is hidden or merged**: the grouped finding stays in the list at its own rank (the list, the ranking and the counts are the same with or without it), and `group.cause` is the `id` of another finding of the same list. The one case so far is `offline_behind_offline_uplink`: a `device.offline` finding for a device whose last reported uplink leads, through devices that are all offline too, to an offline device that has its own `device.offline` finding; that device, the one farthest up the chain, is the cause (a gateway rather than the switch below it, when both are offline). A `group` has `kind`, `cause`, `cause_code`, a one-sentence `summary`, `evidence` (`source`, and the `chain` from the grouped device to the cause: each hop has `name`, `mac`, `offline`, the `finding` id of its own offline finding or `null`, and the `uplink_port` of the next device it was plugged into) and `limitations`. **An offline device keeps its last known uplink**, so the chain shows where the devices *were* connected and the claim is "probably", never "confirmed" (a power cut or a cabling fault would look the same): the limitations say so. It is **not** grouped when the device has no known uplink or the uplink names a device the controller does not list, when its parent is online, when the offline devices above it have no finding (an ignore rule hid it: there is nothing to point at), or when the uplinks form a loop. The same group is in the answer of `PUT .../triage`. It is built from the uplinks already read for the findings, so it asks the controller for nothing more.

### Dashboard summary: `/api/v1/unifi/sites/{site}/dashboard`

`GET .../dashboard` is one small document for the first page of a web interface: **how the network is, and how much of that is known.** There is no command for it. It is built from the reads a full `diagnose` makes (the same cache entries, so a page that also shows the findings costs no extra read, and no new controller endpoint) and uses the same builders as `diagnose`, `wan` and `wifi`. Both roles read it; it never writes. Its [schema](schemas.md) is `dashboard.v1.schema.json`.

| Key | What it says |
| --- | --- |
| `status` | `critical` or `warning` when a check found that (a finding that is acknowledged or snoozed still counts: triage never hides a finding); `ok` **only** for a complete, fresh read of a controller that answered; otherwise `unknown`. A partial, old or failed read is never `ok`. |
| `complete` | False when an optional read failed, so some findings or sections may be missing (the `warnings` of the response say which; it is the flag `diagnose` has). |
| `stale` | True when a read failed and an older cached answer (at most 10 minutes old) was used instead; `generated_at` then says how old the data is. |
| `controller.state` | `ok`, `unreachable` (no answer), `certificate` (TLS check failed) or `key_rejected` (the API key was refused). |
| `findings` | `total`, `ignored` (what the [ignore list](diagnose.md) hid), `by_severity`, `by_state` (`open`, `acknowledged`, `snoozed`, from the [triage](#findings-and-triage-apiv1unifisitessitefindings) file: **`null`**, never zero, when it cannot be used, with `triage_available` false) and `attention`: the five open findings to look at first, in the order of the findings list (`id`, `rank`, `severity`, `code`, `subject`, `message`). |
| `devices` | `total`, `online`, `offline` and `other` (adopting, updating, pending adoption...). |
| `clients` | `connected`, `wired`, `wireless` and `offline` (previously seen, not connected; `null` when the client history could not be read). |
| `wan` | The headline of [`wan`](network.md): `status`, `internet_status`, `latency_ms`, `drops`, `availability_pct` (the lowest 24-hour availability), `nat` and the `last_speedtest`. |
| `wifi` | Access points (`access_points`, `access_points_online`), `radios`, `wireless_clients`, and the `lowest_satisfaction` and `highest_utilization` of the radios of online access points. |
| `events` | `total` in the last 24 hours, `truncated`, `notable_total` (above low severity) and the five newest `notable` events. |

- **`available`.** Every section has it. False means the section was **not read** (for example `wan` when the health read failed, `wifi` without the legacy device list, `events` without the event log): the other keys are then absent, and a number is never filled with zero. A number whose source is missing is `null`.
- **A controller that cannot be reached is a `200`**, not a `502`: `controller.state` says why, `status` is `unknown`, `complete` is false, every section is `{"available": false}` and `site` is `null`, with one fixed warning. A dashboard can show that; an error page cannot. A failure that is not about the connection (an unknown site is `404`, an answer that cannot be used `502`) is the usual error. A controller that is down while the cache still holds an older answer gives the old data with `stale` true and the state of the failed connection.
- **Reads:** the same as `diagnose` (devices, clients, the legacy device and client lists, the client history, the network configuration, `stat/health`, speedtests and the one event-log query); `tests/test_needs.py` pins this.

### Notes: `/api/v1/unifi/sites/{site}/notes`

A note is a short plain-text remark an administrator keeps about a finding, a device or a client ("moved to the shelf", "known, waiting for the ISP"). Both roles read; only an administrator adds, changes or deletes.

- **The subject** is `device:<MAC>`, `client:<MAC>` (a MAC in any spelling, kept as `AA:BB:CC:DD:EE:FF`) or `finding:<id>` (the `id` of the [findings list](#findings-and-triage-apiv1unifisitessitefindings)). A name is never a subject, so **a note follows a device when it is renamed**, and a note about a device that is offline now is kept: no subject has to exist.
- **Routes.** `GET .../notes` (all of the site, oldest first, or `?subject=` for one) with `items` and `total`; `POST .../notes` with `subject` and `text` (`201`); `PATCH .../notes/{note}` with `text`; `DELETE .../notes/{note}` (answers with the note that was deleted). A note has `id`, `subject`, `text`, `author`, `created_at`, `modified_at` and `modified_by`; **the author is the login of the request**, never a field the request can set. The findings list shows `note_count` for each finding.
- **The text** is plain text, at most 4000 characters, with its line breaks kept and control, invisible and direction-changing characters removed; it is never interpreted as HTML or markup. At most 200 notes per subject and 5000 per site (`409 notes_full`).
- **Concurrent edits.** Every note has a `revision`, an opaque token that changes with every change. `PATCH` must send the revision it read; when the note was changed since, the answer is `409 notes_conflict` and the saved change is kept (reload, then edit again). A note edited with an unchanged text still gets a new revision.
- **Retained subjects.** `GET .../notes/subjects` (`?q=` searches the reference and the last-known name) lists every subject that has notes with its `kind`, `note_count`, `last_note_at` and `last_known` (`name`, `recorded_at`): the name a person gave when writing or editing a note (`name` in the request, at most 120 characters), kept as **last-known context** and never mistaken for current inventory. It does not depend on the inventory, so a device that is gone, a client that left and a finding that cleared stay listed and editable; nothing is pruned because a subject is absent, and nothing here concludes that a subject is gone (that is for a complete inventory to say). `GET .../notes?subject=` is the detail. Notes are read from the data directory when `{site}` is the id of a site whose notes are already here, so a controller that cannot be read hides nothing; a name or reference such as `default`, and every write, still resolves the site through the controller.
- **Not a controller identity.** A note belongs to the site's id (a UUID the controller made for it), not to the controller's address, so notes cannot cross sites; pointing one data directory at a different controller that reuses a site id is not detected (the controller offers no identity this tool reads). Use a data directory per controller.
- **Findings list.** Each finding shows `note_count`; when the notes file cannot be read the list says `notes_available: false` and gives a limitation instead of showing zero.
- **Where it is kept.** `snapshots/<site id>/notes.json`, per site like the triage state and the snapshots (so a note cannot reach another site, and the Docker volume that covers `snapshots/` covers it), owner-only, written atomically under a lock, never through a symbolic link. A damaged file is `500 notes_unreadable` with a fixed message that names no path. Notes are never sent to the controller or in a notification.
- **Audit.** `note.added`, `note.edited` and `note.deleted` with the note id and the kind of subject (`device`, `client`, `finding`): never the text or a MAC. `--read-only` refuses every change.

### Backup: `/api/v1/backup`

An **encrypted backup of this application's own state**, downloaded by an administrator. It is **not a UniFi controller backup** and cannot restore anything on the controller. `POST /api/v1/backup` makes it, `POST /api/v1/backup/preview` says what restoring one would do. Both are administrators only, with the CSRF token, and both are POSTs because the passphrase travels in the body and never in a URL. Export and preview change nothing in the installation, so both also work with `--read-only`; [restoring](#restoring-a-backup-apiv1backuprestore) is below.

- **Export.** Body: `passphrase`, `confirm` (the same, typed again), and `include`, a list of the optional categories (`snapshots`, `audit`). The passphrase has 12 to 1024 characters. The answer is the file (`application/octet-stream`, `homelab-probe-backup-YYYYMMDD-HHMMSSZ.hlpbackup`, never cached). **Without the passphrase the backup cannot be opened, and nothing in this installation can recover it**: keep it somewhere other than this machine. It is not stored, logged, put in an error or echoed in a response.
- **What is in it.** The archive holds the data directory's files under their own names; the manifest lists them:

  | Category | Files | |
  | -------- | ----- | - |
  | `settings` | `hlp.toml` (the settings file the server uses) | always |
  | `config` | `.env`: the controller address, the API key and the notification destinations | always |
  | `accounts` | `users.json` (usernames, roles, password hashes) | always |
  | `certificates` | `certs/*.pem`: the pinned controller certificate | always |
  | `notes`, `triage` | `snapshots/<site>/notes.json` and `triage.json`, **whatever is in the current inventory**: notes about a device that is gone and the state of a finding that has cleared are kept | always |
  | `snapshots` | `snapshots/<site>/snapshot-*.json` and the older `snapshots/snapshot-*.json` | optional |
  | `audit` | `audit.log` and its rotated files | optional |

  Never included: sessions (they live in memory and end on a restore), caches, lock files and the notification state. A server started with `--env-file`/`HLP_ENV` naming a file outside the data directory has its `.env` there, and that file is not in the backup. A variable set in the environment (a Docker container's `environment:`) is not in any file and is never in a backup. A symbolic link where a file or a site directory belongs refuses the backup (`500 backup_unsafe`); a lock another writer holds for more than five seconds is `503 backup_busy`. Each file is read while the locks of the accounts and of the per-site files are held, so it is whole. The audit log, which its writer does not lock, is read until its files are seen unchanged (an append or a rotation during the read means a second read, and a log that keeps changing is `503 backup_busy`), so the history in a backup is one moment's.
- **The format.** A sealed file is `HLPBACKUP\n`, a 4-byte header length, a JSON header (`format`, `kdf`, `n`, `r`, `p`, `salt`, `cipher`, `nonce`) and the ciphertext. The key is scrypt of the passphrase (N=65536, r=8, p=1, a fresh random salt; the parameters are in the header so a later version can raise them) and the cipher is AES-256-GCM from the `cryptography` package (the `web` extra), with the magic, the length and the header as associated data, so a changed header fails like a changed byte. A wrong passphrase and a modified file are one failure on purpose (`backup_decrypt`): the cipher cannot tell them apart. Parameters outside a fixed bound (`backup_unsupported_format`) are refused before any key is derived. Inside is a ZIP (the manifest, then the files) whose `manifest.json` has `format` (the archive layout), `data_format` (what the files mean), `app_version`, `created_at`, `categories` and, per file, `size` and `sha256`. The same format is made and read by a native install and the Docker image.
- **Compatibility.** This version restores `data_format` 1 (`backup.SUPPORTED_DATA_FORMATS`); an archive of another one is refused (`backup_unsupported_data_format`) before anything is read, and a format this version cannot read is `backup_unsupported_format`. A backup made by a newer application with a data format this one reads is accepted with a warning. A later data format brings its reviewed migration with it.
- **Preview.** Administrators, or the setup token on a server that is not set up. Body: `passphrase` and `archive` (the file, base64; at most 64 MiB). The archive is checked completely first: every member name must be one the table allows (never a path that leaves the directory, a link or a directory), sizes are bounded, the manifest must match the members exactly (size and SHA-256), and each file must parse with the reader the application itself uses (settings, accounts, notes, triage, certificates and snapshots, which the snapshot reader of `hlp diff` validates), and the format versions must be exact integers (`true` or `1.0` is not `1`), with the per-site files naming the site their directory is named for, so **notes cannot cross sites**. A backup without an **enabled administrator** is refused (`backup_no_administrator`), so a restore cannot lock the owner out. The answer has the date, the versions, the compatibility, every category (in the backup or not, its files, the files there now, and what a restore would do to them), the accounts that would replace the present ones (names and roles, never a hash), the counts of notes and triage entries, and the **names, never the values**, of the settings in the backup's `.env` that an environment variable keeps overriding (the environment wins and is never rewritten); a server started with `--env-file` or `HLP_ENV` reads that file instead of the data directory's `.env`, which the preview says (`env_file_named`) and then lists every setting as overridden. It contains no secret and no note text.
- **What a restore does** (see [below](#restoring-a-backup-apiv1backuprestore)): it **replaces the settings, configuration, accounts, certificates, notes and triage as one set**, never merging; it ends every session, so everybody logs in again with the backup's credentials; the optional `snapshots` are added (a file of the same name is replaced, the others stay) and an absent optional category leaves what is there untouched; a backup's `audit` history is saved beside the present log as a separate, marked file and never overwrites it.
- **Audit.** `backup.exported` (the optional categories, the number of files, the size), `backup.previewed` (the date and version of the backup), `backup.restored` and `backup.restore_failed` (below): never a passphrase, a name or any content. Errors are fixed text: `backup_decrypt`, `backup_not_a_backup`, `backup_unsupported_format`, `backup_unsupported_data_format`, `backup_unsafe_member`, `backup_too_large`, `backup_bad_manifest`, `backup_mismatch`, `backup_invalid_content`, `backup_no_administrator`, `backup_unsafe`, `backup_busy`, `invalid_passphrase` and `passphrase_mismatch`.
- **Limits.** At most two backups are made or read at once (the key derivation takes about 64 MiB each): a third is `503 backup_busy`. A file is at most 64 MiB, all of them 256 MiB and 20000 members.

### Restoring a backup: `/api/v1/backup/restore`

`POST /api/v1/backup/restore` replaces this installation's own state with a backup, as one set. Body: `passphrase`, `archive` (base64), `confirm` (must be `true`: the [preview](#backup-apiv1backup) was read) and, when there is a present state to keep, `recovery_passphrase` typed twice as `recovery_confirm`. The same checks as the preview run first, and a backup that fails any of them changes nothing.

- **Who.** An administrator (session and CSRF token). On a server that is **not set up**, or has **no enabled administrator**, the setup token instead: that is how a **fresh installation** is restored (the preview too). `--read-only` refuses (`403 read_only`), and so does `serve --demo`.
- **What is replaced.** The settings (`hlp.toml`, the file the server uses), the configuration (`.env`), the accounts, the certificates, the notes and the triage state are replaced **as a complete set, never merged**: a file the backup does not have is removed (a settings file named with `--config` outside the data directory is written, never removed). The optional categories: the `snapshots` of the backup are **added** (a file of the same name is replaced; the others stay) and an absent optional category leaves what is there alone, nothing is deleted. The `audit` history of a backup goes to `audit-imports/<date of the backup>/audit.log` and never touches the present `audit.log`, so imported history stays separate and marked.
- **Accounts and sessions.** The accounts and passwords of the backup replace the present ones. **Every session ends**, including the caller's: log in again with the credentials of the backup. A backup without an enabled administrator is refused before anything is touched.
- **Settings.** The configuration is read again, the controller service is made anew and the login state is rebuilt (the session lifetimes and the audit log rotation come from the restored settings), so the restored `.env` takes effect without a restart. **A setting the environment sets keeps winning** (the preview names them); the restore never rewrites the environment, and a server that is waiting for its setup leaves the setup mode.
- **A recovery backup first.** Before replacing an existing installation, an encrypted backup of the present state (the always-replaced categories) is written to `recovery/recovery-<UTC time>.hlpbackup` in the data directory, owner-only, with the `recovery_passphrase`. It must be a passphrase you keep: **do not assume the passphrase of the backup being restored**, and it cannot be recovered. The newest **three** are kept: older ones are deleted after a *successful* restore, never after a failed one and never while a restore is unfinished. Open one by sending it to the preview with its passphrase and restoring it. A fresh installation has nothing to keep and needs none. A recovery backup that cannot be written (`500 backup_recovery_failed`) stops the restore before anything changes.
- **One set, and a crash.** A series of file replacements is not one atomic operation, so the restore keeps a journal (`restore-journal.json`) and stages every new file next to its target (`<name>.restore-new`). The journal is written, the files are staged and synced, the journal is committed as `applying`, then each present file is moved to `<name>.restore-old` and its new file is renamed into place. A crash before the commit changes nothing (the staged files are removed at the next start). A crash after it is **completed** at the next start. If it cannot be completed (a staged file is missing, is not a regular file or does not match its checksum, or a file cannot be written) it is **undone** from the `.restore-old` files. `hlp serve` does this before it reads the settings and says so in a warning; a state that can be neither completed nor undone, or a journal that cannot be read, stops the start with exit code 3 and a message naming only `restore-journal.json`, and the files stay for the next try. A failure while the server runs is undone before the answer (`500 backup_restore_failed`, the sessions stay); if even the undo fails the answer is `500 backup_restore_pending` and the next start finishes it.
- **While it runs.** One restore at a time (`503 restore_in_progress`). A route that writes a file answers `503 restore_in_progress`, and the scheduler is waited for: a job that is running ends first (up to a minute, else `503 backup_busy` and nothing changes) and none starts until the restore is over. The restore holds the file locks of every file it replaces (`users.json`, `.env`, the settings file, every site's `notes.json` and `triage.json`, including a site the backup adds), the same locks the other writers take, so `hlp web-user add`, `hlp init`, a settings change or a cron `diagnose --notify` waits for them or is told it is busy. Reads keep working. Snapshots are not locked: a `hlp snapshot` run during a restore can add a file of its own, and the restore only ever adds to or replaces snapshots of the same name.
- **Audit.** `backup.restored` (the date and version of the backup, the number of files, the name of the recovery backup or `none`) written to **this installation's** audit log after the swap, and `backup.restore_failed` with a fixed reason. Never a passphrase, a name or any content.
- **Errors** (fixed text): everything the preview says and `recovery_passphrase_required`, `invalid_recovery_passphrase`, `recovery_passphrase_mismatch`, `backup_recovery_failed`, `backup_restore_failed`, `backup_restore_pending`, `backup_journal_damaged`, `restore_in_progress`, `demo` and `reload_failed` (the files were restored but the settings could not be loaded: restart the server).
- **Not in the backup, so not restored:** the notification state (a restored installation may announce what it sees again), sessions and caches, the controller. Variables set in the environment, and a `.env` named with `--env-file`/`HLP_ENV` outside the data directory, are not touched.

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

## The scheduler

`hlp serve --scheduler` runs two jobs on a background thread of the server, one after the other. It starts when the server is set up (not before) and stops with the server. It is **off** by default (a workstation's `serve`). The [container image](docker.md) starts the server with `--scheduler`; otherwise add the flag to your own `serve` command.

| Job | Every | What it does |
| --- | ----- | ------------ |
| diagnose | `SCHEDULER_DIAGNOSE_MINUTES` (default 15, 1 to 1440) | The health checks of the configured site, with the settings file read again each time, then the notification step of `diagnose --notify` for the destinations in `.env` |
| snapshot | `SCHEDULER_SNAPSHOT_HOURS` (default 24, 0 for never, up to 720) | The snapshot `snapshot` saves, in `snapshots/<site id>/`, keeping the newest `SCHEDULER_SNAPSHOT_KEEP` (default 30) |

- **It shares the notification state with cron.** The state is `snapshots/<site id>/notify-state.json` of the data directory, the file `diagnose --notify` uses from the same directory, and **everything from reading it to saving it happens under one lock** (`notify-state.json.lock`, also taken by the command line). A scheduled run and a cron run, or two of either, take turns and never both announce a finding: the second sees it already reported. A run that cannot get its turn in two minutes sends nothing and says so (exit code 3 from the command line, a failed scheduled run). When the server stops, a job that is waiting for its turn gives up at once, and one that is reading the controller finishes the read but then sends, saves and writes nothing (`skipped`, reason `stopping`). The state of the command line and the scheduler is the same data, so a `--notify-baseline` made by hand is respected.
- **The first run does not announce everything.** When no state exists yet, the first scheduled run records the current findings as already reported (what `--notify-baseline` does). Only what is new, worse or fixed after that is announced.
- **A failed or partial read never means "fixed".** If the controller cannot be read, the run fails and the state is untouched. If an optional read failed (the result is incomplete), the run is `skipped` with the reason `partial_data` and the state is untouched, so a missing answer cannot announce a recovery.
- **Snapshots wait after a restart.** The first snapshot after the server starts is due when the newest saved snapshot of the site is as old as the interval (at once when there is none), so restarting a container does not fill the directory. The scheduler never writes its state or a snapshot through a symbolic link at `snapshots/` or `snapshots/<site id>/` (the run fails with `snapshots_unsafe`); a snapshot is created exclusively, so even a dangling link at its name is never opened. The data directory itself may be a link (a mounted volume is one), and the command line follows the links you give it, as it always did.
- **Every run is logged** as one `scheduler.run` record ([logging](logging.md)) with a **run id**, the job, the result (`ok`, `failed` or `skipped`), a fixed reason (`baseline`, `nothing`, `sent`, `undelivered`, `partial_data`, `no_destination`, `controller_timeout`, `config`, `storage`, `snapshots_unsafe`, `error`...), the milliseconds, the number of findings or of old snapshots removed, and per destination only its kind and whether it was delivered (`ntfy:sent`). Never a finding, a name, an address, a URL or a message. `scheduler.start` says the intervals. Each delivery is also a `notify.delivery` record, as on the command line.
- A job that fails does not stop the scheduler: the next one runs at its time.

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
  | `user.password_changed` | a user changed their own password over the API |
  | `user.password_upgraded` | a login replaced an old hash with one made with the current parameters |
  | `user.deleted` | `delete` |

  A password never reaches the file (a field named like a secret is hidden whatever it holds), and a refused change is not recorded because nothing changed. A failed audit write is reported and the corresponding account change is rolled back. Login and settings events arrive with the features that make them. The same records are also logged at `INFO` as `audit.event` on `homelab_probe.audit` ([logging](logging.md)).

  The log **rotates by size, never by age**: `AUDIT_LOG_MAX_MB` (default 5, 1 to 1024) is the size of one file and `AUDIT_LOG_FILES` (default 10, 2 to 1000) how many are kept in all, the current `audit.log` and `audit.log.1`, `audit.log.2`... `web-user` reads both from the environment (not from `.env`, which it does not read); they are also settings of `.env` for the server to come.

## What is not here yet

The web app (the screens of the setup wizard among them), come with later stages of the roadmap. The login uses the interface the accounts module was built for: an `Authenticator` that turns a username and password into a `Principal(username, role, source)`, with `LocalAccounts` (this page's accounts) as the first implementation. A wrong password, an unknown user and a disabled one all take the same work and give the same answer, so the answer does not reveal which usernames exist. Authentication checks the current account record and records the login under the same file lock, so a concurrent disable, role change or deletion cannot return a stale principal.

## The web app (`web/`)

The browser interface lives in `web/` (React, TypeScript, Vite) and talks to the API above from the same origin: the session cookie, the `X-CSRF-Token` from the login on every unsafe request (kept in memory, never in browser storage) and the `{error, message}` of every failure. This first part has the login and logout, a navigation shell for phone, tablet and desktop, light, dark and system themes, the loading, refreshing, empty, error, stale and partial states, and a home and a profile page; the pages for the reports, findings and settings follow. Controller strings are rendered as text only, with the control, invisible and bidirectional characters that `util.printable` removes also removed here. How to run it against `hlp --demo serve` and what CI checks are in [development.md](development.md#the-web-interface-web).
