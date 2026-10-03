# Configuration and troubleshooting

Every setting, how the `.env` file is found, what `--verbose` shows, and what to do when something goes wrong.

## Configure

Edit `.env`:

```env
CONTROLLER_URL=https://your-controller-ip:443
API_KEY=your-api-key-here
SITE_ID=default
VERIFY_SSL=true
```

| Variable         | Required | Default   | Description                                                        |
| ---------------- | -------- | --------- | ------------------------------------------------------------------ |
| `CONTROLLER_URL` | Yes      | -         | Controller URL (include protocol and port)                         |
| `API_KEY`        | Yes      | -         | API key from the controller                                        |
| `SITE_ID`        | No       | `default` | Site name, internal reference (e.g. `default`) or UUID (also `--site NAME\|REF\|UUID` before the command) |
| `VERIFY_SSL`     | No       | `true`    | `true`/`yes`/`1`/`on`, `false`/`no`/`0`/`off` (any case), or the path of a CA bundle |
| `TIMEOUT`        | No       | `15`      | Seconds to wait for each request, 1 to 600 (also `--timeout SECONDS` before the command) |
| `PARALLEL_REQUESTS` | No    | `6`       | How many requests to make at once, 1 to 16; 1 means one by one (also `--parallel N` before the command) |
| `NOTIFY_NTFY_URL` | No      | -         | Full ntfy topic URL for `diagnose --notify` (a secret, `https://` only) |
| `NOTIFY_NTFY_TOKEN` | No    | -         | ntfy access token, sent as a bearer token |
| `NOTIFY_WEBHOOK_URL` | No   | -         | Generic webhook URL for `diagnose --notify` (a secret, `https://` only) |
| `NOTIFY_WEBHOOK_TOKEN` | No | -         | Webhook bearer token |
| `NOTIFY_SMTP_HOST` | No     | -         | Mail server for `diagnose --notify` email; setting it turns email on (needs the two address settings) |
| `NOTIFY_SMTP_PORT` | No     | 587 (starttls), 465 (ssl), 25 (none) | Mail server port |
| `NOTIFY_SMTP_SECURITY` | No | `starttls` | `starttls`, `ssl`, or `none` (plain, lab opt-in only, never with a password) |
| `NOTIFY_SMTP_USER`, `NOTIFY_SMTP_PASSWORD` | No | - | Login, both or neither (a secret; an app password for a provider) |
| `NOTIFY_EMAIL_FROM`, `NOTIFY_EMAIL_TO` | With a host | - | Sender, and one or more recipients separated by commas (plain `name@host` addresses) |
| `ALLOW_INSECURE_HTTP` | No  | `false`   | Lab-only opt-in to an `http://` controller URL (same words as `VERIFY_SSL`) |

- **Where the `.env` file is found**, first match wins: the file given with `--env-file FILE` (before the command, for example `unifi-sentinel --env-file lab.env diagnose`); the file named by the `UNIFI_SENTINEL_ENV` environment variable; `.env` in the **current directory**. Parent directories and the installed package's directory are not searched, so an installed copy (`pip install .`) works from whichever directory holds your `.env`, an unrelated project's `.env` is never picked up, and running from a subdirectory of the project does not find the project's `.env` (use `--env-file` or run from the project root). A file named with `--env-file` or `UNIFI_SENTINEL_ENV` must exist. Real environment variables always take precedence over values in the file. The `unifi-sentinel.toml` settings file for `diagnose` is likewise read from the current directory.
- **`VERIFY_SSL`:** the example file ships with `true`. **The better fix for a self-signed certificate is to trust it instead of turning checking off:** point `VERIFY_SSL` at the certificate file (or at the CA that signed it) in PEM format, for example `VERIFY_SSL=/home/me/unifi-ca.pem` (a `~` is expanded, and a relative path is relative to the directory you run from; a directory of certificates also works). The path must exist and be readable, or the command stops with a message naming it, and a word that is neither a yes/no word nor a path is an error. You can export the certificate from your browser's certificate viewer while looking at the controller's address. A UniFi controller usually has a self-signed certificate, so the first run may fail with `TLS certificate verification failed`; then trust that certificate as described next, install a trusted certificate on the controller, or as a last resort set `VERIFY_SSL=false`, which sends your API key without checking who answers (acceptable on a trusted home network, not elsewhere). An unset or empty value verifies certificates. Any other word than the ones above is an error that lists the accepted words, so a typo such as `off-ish` can never silently mean "verify".
- **Protecting the API key:** the key is a credential for your controller, so keep `.env` private with `chmod 600 .env`. If the file that was read is accessible to your group or to other users (any of the group or other permission bits set), the command prints one warning that names the file and the `chmod 600` fix, and carries on. It is only a warning, and it is skipped on Windows where file modes mean little. A symlink is judged by the file it points to. The key is never printed: error messages, warnings and `repr()` of the configuration leave it out, and if a server or proxy echoes it back in an error body it is replaced with `***`.
- **`CONTROLLER_URL`:** it needs a scheme and a host (`https://host` or `https://host:port`; a trailing slash is removed). An `http://` URL is refused, because the API key is sent in a header of every request and would travel in clear text; use `https://` (with `VERIFY_SSL=false` for a self-signed certificate). For a lab network you trust you can opt in with `ALLOW_INSECURE_HTTP=true`; every run then prints a warning that the key travels in clear text. A URL containing a user name, password, query (`?`), fragment (`#`), space, backslash or control character is refused.
- **Timeouts and retries:** every request waits at most `TIMEOUT` seconds (default 15; `--timeout SECONDS` before the command overrides `.env`; a slow gateway may need more). A `GET` that fails with a connection error, a timeout or an HTTP 502, 503 or 504 is **retried twice** with a growing pause (0.5 s, then 1 s), so one brief blip no longer fails the command; anything else (a bad key, a 403, a 404 or 500, a certificate failure, a malformed answer) is reported at once, because trying again cannot change it. The single event-log `POST` is never retried. A message that says `(after 3 attempts)` means the retries were used up. A `403 Forbidden` means the API key is valid but not allowed to make that request.
- **Speed:** the reads a command needs do not depend on each other, so they run side by side: up to `PARALLEL_REQUESTS` at once (default 6; `--parallel N` before the command overrides it; `1` reads one request at a time, the old behavior, which is also handy when reading `--verbose` output). The result is identical either way: the same requests are made, the output keeps the controller's order, and warnings are shown in a fixed order. Still GET only (plus the one event-log query). The analysis itself is linear in the number of clients (8,000 clients take a few hundredths of a second instead of seconds). `client` looks the client up in the devices, the connected clients and the client history first, and reads the network configuration, the groups and the event log (a POST) only for a client that matched, so a name that matches nothing costs three reads and sends no POST. What each command reads is pinned by a test (`tests/test_needs.py`).
- **When a read fails:** required data stops the command with exit code 3; optional data warns and the command carries on with less. The list of connected clients and devices is required everywhere. Every legacy read is optional, **including the client history (`stat/alluser`)** for `query`, `client`, `diagnose` and the other reports (they warn that offline clients and reservations are unavailable), except for the two commands whose answer would be wrong without it: `new-clients` and `snapshot`/`diff` stop with exit code 3, so they never print a misleading list or save an incomplete snapshot.
- **`SITE_ID`:** a site name may contain spaces and non-ASCII letters, but not `/`, `\`, `?`, `#` or control characters, and at most 128 characters; it is also percent-encoded wherever it appears in a URL. **`--site NAME|REF|UUID`** (before the command, like `--timeout`) chooses the site for one run and beats `SITE_ID`: `unifi-sentinel --site Lab diagnose`. It takes the same three kinds of value and is checked the same way (a bad or empty value is a usage error, exit code 64, before any request); an unknown site stops the command with exit code 3 and lists the sites there are (`unifi-sentinel info` shows them). Every command that reads a site uses it. Saved snapshots and the notification state are not kept apart by site, so with several sites give each its own `--notify-state FILE` and `snapshot --dir DIRECTORY`.

## Finding your site: `info`

`info` is the quickest way to test your settings: it reads the controller's application info and its sites, nothing else, so a wrong address, key or certificate shows up here first. The sample is from the synthetic fixture:

```text
$ unifi-sentinel info
Application: {'applicationVersion': '10.0.0'}
Site: Default ref=default id=site-1
```

There is one `Site:` line per site, with its name, internal reference (`ref`) and UUID (`id`). Any of the three is a valid `SITE_ID` or `--site` value; for any other command an unknown one stops it with exit code 3 and lists these. Names are cleaned of control characters before they are printed. `info` has no options of its own and reads only the two Integration API endpoints (every request is a GET).

## Seeing what the tool does: `--verbose`

`--verbose` (or `--debug`), given before the command, logs to **stderr** what the tool does, so the normal output on stdout is unchanged and can still be piped. It is the tool for diagnosing a slow run, a failing request or an undocumented endpoint:

```text
$ unifi-sentinel --verbose wan --json > wan.json
[verbose] unifi-sentinel 0.2.0: settings from /home/me/unifi-sentinel/.env; controller https://192.168.1.1:443, site default, timeout 15 s, TLS verification off
[verbose] GET /proxy/network/integration/v1/sites?offset=0&limit=200 -> 200 (41 ms)
[verbose] GET /proxy/network/api/s/default/stat/health -> 503 (35 ms)
[verbose] GET /proxy/network/api/s/default/stat/health -> retrying in 0.5 s (attempt 2 of 3)
[verbose] GET /proxy/network/api/s/default/stat/health -> 200 (38 ms)
[verbose] read 10 devices, 83 connected clients, 5 health subsystems, 91 speedtests
[verbose] 27 request(s), 1 retried, 0.7 s in requests (added up over all of them, so more than the wall time when they overlap)
```

- **The first line** shows which `.env` was read (or that only environment variables were used), the controller, site, timeout and how TLS is verified (`on`, `off`, or the CA bundle in use), so a command that talks to the wrong controller is obvious.
- **One line per request attempt:** method, path (with the paging parameters), the HTTP status or what went wrong (`timed out`, `connection error`, `TLS certificate verification failed`), and the time in milliseconds. A retry shows the wait and the attempt number. The event-log `POST` shows the names of the query fields, never their values.
- **`read ...`** lists what a snapshot collected (counts only), and the last line is the total number of requests, retries and time spent in requests (added up over all of them, so more than the wall time when they overlap). The last line is also printed when a request fails.
- **Never logged:** the API key, response bodies, and query values. The paths do contain the site and device IDs, and the first line your controller's address, so **redact them before pasting the output into an issue**.

## Shell completion

`unifi-sentinel completion bash`, `completion zsh` and `completion fish` print a completion script for the installed `unifi-sentinel` command: it completes the commands, every option (with its short form), the fixed values (`--fail-on info warning critical`, the query kinds, the Wi-Fi bands, the `diagnose --only` and `--skip` areas one at a time, even in a comma list), and file names for the options that take a file. The command needs no `.env` and never contacts the controller. The scripts are generated from the program's own options, not written by hand, so a new option is completed as soon as it exists; a test checks that every command and option is in every script.

```bash
# bash: for this shell, or save it where bash-completion looks (for example ~/.local/share/bash-completion/completions/unifi-sentinel)
source <(unifi-sentinel completion bash)

# zsh: put it in a directory on $fpath, then restart the shell (or run compinit)
unifi-sentinel completion zsh > "${fpath[1]}/_unifi-sentinel"

# fish
unifi-sentinel completion fish > ~/.config/fish/completions/unifi-sentinel.fish
```

It completes the `unifi-sentinel` command that `pip install .` or `uv tool install` creates, not `uv run unifi-sentinel.py` (there the shell sees `uv`). The bash script also works in the old bash 3.2 that macOS ships; free text values (a search, a client name) fall back to the shell's file name completion in bash. The zsh script uses `_arguments`, so descriptions of the options show next to the completions.

## Troubleshooting

- **`CONTROLLER_URL is not set` / `API_KEY is not set`**: copy `example.env` to `.env` and fill it in, in the directory you run the command from, or point to it with `--env-file FILE` or `UNIFI_SENTINEL_ENV`.
- **`CONTROLLER_URL uses http://`**: use `https://` (the API key would be sent in clear text), or set `ALLOW_INSECURE_HTTP=true` for a trusted lab network.
- **`... is accessible to other users`**: run the `chmod 600` command in the warning; the file holds your API key. On a file system that does not keep Unix permissions (a Windows drive mounted in WSL, some network shares) the mode cannot be changed and the warning stays; keep the file on a normal Linux or macOS file system, or in your home directory.
- **`env file not found`**: the file named with `--env-file` or `UNIFI_SENTINEL_ENV` does not exist.
- **`VERIFY_SSL must be one of ...` / `SITE_ID ... cannot be part of a site name`**: fix the value in `.env`; the message lists what is accepted.
- **`401 Unauthorized`**: the API key is invalid or was revoked; create a new one.
- **`TLS certificate verification failed`**: for a self-signed certificate point `VERIFY_SSL` at the certificate (or CA) file, or install a valid certificate on the controller; `VERIFY_SSL=false` skips the check and is a last resort. If `VERIFY_SSL` already names a file, the message says the certificate is not signed by anything in it.
- **A command is slow or fails and you want to see why**: run it again with `--verbose` (see above) to see each request, its status and time, and any retries.
- **Connection errors or timeouts**: check `CONTROLLER_URL` and that the controller is reachable from this machine. `timed out after 15 s (after 3 attempts)` means a slow gateway: raise `TIMEOUT` or pass `--timeout 60`.
- **`403 Forbidden`**: the API key is valid but lacks access to that request; check the key under Settings > Control Plane > Integrations.
- **`VERIFY_SSL names a CA bundle that does not exist`**: fix the path (it is relative to the directory you run from).
- **`Site '...' not found`**: run `info` to list site names, references and IDs.
- **`... unavailable` warnings**: the tool degrades instead of failing. The rest of the command still runs, with less data:
  - `legacy stat/... unavailable`: switch port mapping, port counters and offline clients are incomplete
  - `legacy rest/... unavailable`: network names and VLANs are missing (reservations, subnet checks)
  - `legacy v2 ... unavailable`: group names are missing; `new-clients` trusts each client's own group list
  - `legacy stat/health unavailable`: `diagnose` skips the controller health and WAN checks
  - `neighboring networks unavailable`: `wifi` shows the radios and channel table with neighbor counts marked unavailable; neighbor-based channel comparisons are skipped
  - `speedtest history unavailable`: `wan` shows no speedtests and `diagnose` skips the speedtest check
  - `event log unavailable`: `events` shows nothing, and `diagnose` and `client` skip their event parts
  - `detail/statistics unavailable for N device(s)`: no uptime, heartbeat or CPU/memory for those devices (normal for offline devices)
