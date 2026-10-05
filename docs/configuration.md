# Configuration and troubleshooting

Every setting, how the `.env` file is found, what `--verbose` shows, and what to do when something goes wrong.

## Configure

Edit `.env`:

```env
UNIFI_URL=https://your-controller-ip:443
UNIFI_API_KEY=your-api-key-here
UNIFI_SITE_ID=default
UNIFI_VERIFY_SSL=true
```

| Variable         | Required | Default   | Description                                                        |
| ---------------- | -------- | --------- | ------------------------------------------------------------------ |
| `UNIFI_URL` | Yes      | -         | Controller URL (include protocol and port)                         |
| `UNIFI_API_KEY`        | Yes      | -         | API key from the controller                                        |
| `UNIFI_SITE_ID`        | No       | `default` | Site name, internal reference (e.g. `default`) or UUID (also `--site NAME\|REF\|UUID` before the command) |
| `UNIFI_VERIFY_SSL`     | No       | `true`    | `true`/`yes`/`1`/`on`, `false`/`no`/`0`/`off` (any case), or the path of a CA bundle |
| `UNIFI_TIMEOUT`        | No       | `15`      | Seconds to wait for each request, 1 to 600 (also `--timeout SECONDS` before the command) |
| `UNIFI_PARALLEL_REQUESTS` | No    | `6`       | How many requests to make at once, 1 to 16; 1 means one by one (also `--parallel N` before the command) |
| `LOG_LEVEL`      | No       | `WARNING` | `DEBUG`, `INFO`, `WARNING` or `ERROR` (any case); `--verbose` means `DEBUG` ([logging](logging.md)) |
| `LOG_FORMAT`     | No       | (command line) | `text` or `json` for one record per line with fields; unset keeps the command-line format ([logging](logging.md)) |
| `AUDIT_LOG_MAX_MB` | No      | `5`       | Size of one web audit log file in megabytes, 1 to 1024 ([web accounts](web.md#the-files)) |
| `AUDIT_LOG_FILES` | No      | `10`      | How many audit log files are kept in all, 2 to 1000 |
| `SESSION_IDLE_MINUTES` | No | `30`     | A web login ends after this many minutes without a request, 1 to 1440 ([web interface](web.md#logging-in-apiv1auth)) |
| `SESSION_MAX_HOURS` | No    | `12`      | A web login ends this many hours after it began, 1 to 720 |
| `NOTIFY_NTFY_URL` | No      | -         | Full ntfy topic URL for `diagnose --notify` (a secret, `https://` only) |
| `NOTIFY_NTFY_TOKEN` | No    | -         | ntfy access token, sent as a bearer token |
| `NOTIFY_WEBHOOK_URL` | No   | -         | Generic webhook URL for `diagnose --notify` (a secret, `https://` only) |
| `NOTIFY_WEBHOOK_TOKEN` | No | -         | Webhook bearer token |
| `NOTIFY_SMTP_HOST` | No     | -         | Mail server for `diagnose --notify` email; setting it turns email on (needs the two address settings) |
| `NOTIFY_SMTP_PORT` | No     | 587 (starttls), 465 (ssl), 25 (none) | Mail server port |
| `NOTIFY_SMTP_SECURITY` | No | `starttls` | `starttls`, `ssl`, or `none` (plain, lab opt-in only, never with a password) |
| `NOTIFY_SMTP_USER`, `NOTIFY_SMTP_PASSWORD` | No | - | Login, both or neither (a secret; an app password for a provider) |
| `NOTIFY_EMAIL_FROM`, `NOTIFY_EMAIL_TO` | With a host | - | Sender, and one or more recipients separated by commas (plain `name@host` addresses) |
| `ALLOW_INSECURE_HTTP` | No  | `false`   | Lab-only opt-in to an `http://` controller URL (same words as `UNIFI_VERIFY_SSL`) |

- **Where the `.env` file is found**, first match wins: the file given with `--env-file FILE` (before the command, for example `hlp --env-file lab.env diagnose`); the file named by the `HLP_ENV` environment variable; `.env` in the **current directory**. Parent directories and the installed package's directory are not searched, so an installed copy (`pip install .`) works from whichever directory holds your `.env`, an unrelated project's `.env` is never picked up, and running from a subdirectory of the project does not find the project's `.env` (use `--env-file` or run from the project root). A file named with `--env-file` or `HLP_ENV` must exist. Real environment variables always take precedence over values in the file, and a setting listed on several lines uses the **last** one; `hlp doctor` reports both, and misspelled names, in its `config.env_contents` check. The `hlp.toml` settings file for `diagnose` is likewise read from the current directory. `hlp serve` differs in one way: with no `--env-file` and no `HLP_ENV` it reads the `.env` in its `--data-dir`, and without usable settings it starts the [guided setup](web.md#first-run-setup-a-server-with-no-settings) instead of exiting. `HLP_SETUP_TOKEN` (the web setup token, at least 16 characters) is read from the environment only; a `.env` that sets it gets a `config.env_contents` warning.
- **`UNIFI_VERIFY_SSL`:** the example file ships with `true`. **The better fix for a self-signed certificate is to trust it instead of turning checking off:** point `UNIFI_VERIFY_SSL` at the CA that signed the controller's certificate in PEM format, for example `UNIFI_VERIFY_SSL=/home/me/unifi-ca.pem` (a `~` is expanded, and a relative path is relative to the directory you run from; a directory of certificates also works). A self-signed certificate can also work as the file only when OpenSSL accepts it as a CA/trust anchor; some UniFi-generated certificates are self-signed leaf certificates and fail with `invalid CA certificate`, even if the file exactly matches the controller's certificate. The path must exist and be readable, or the command stops with a message naming it, and a word that is neither a yes/no word nor a path is an error. You can export the certificate from your browser's certificate viewer while looking at the controller's address. A UniFi controller usually has a self-signed certificate, so the first run may fail with `TLS certificate verification failed`; then trust the CA as described next, install a trusted certificate on the controller, or as a last resort set `UNIFI_VERIFY_SSL=false`, which sends your API key without checking who answers (acceptable on a trusted home network, not elsewhere). An unset or empty value verifies certificates. Any other word than the ones above is an error that lists the accepted words, so a typo such as `off-ish` can never silently mean "verify".
- **Protecting the API key:** the key is a credential for your controller, so keep `.env` private with `chmod 600 .env`. If the file that was read is accessible to your group or to other users (any of the group or other permission bits set), the command prints one warning that names the file and the `chmod 600` fix, and carries on. It is only a warning, and it is skipped on Windows where file modes mean little. A symlink is judged by the file it points to. The key is never printed: error messages, warnings and `repr()` of the configuration leave it out, and if a server or proxy echoes it back in an error body it is replaced with `***`.
- **`UNIFI_URL`:** it needs a scheme and a host (`https://host` or `https://host:port`; a trailing slash is removed). An `http://` URL is refused, because the API key is sent in a header of every request and would travel in clear text; use `https://` (with `UNIFI_VERIFY_SSL=false` for a self-signed certificate). For a lab network you trust you can opt in with `ALLOW_INSECURE_HTTP=true`; every run then prints a warning that the key travels in clear text. A URL containing a user name, password, query (`?`), fragment (`#`), space, backslash or control character is refused.
- **Timeouts and retries:** every request waits at most `UNIFI_TIMEOUT` seconds (default 15; `--timeout SECONDS` before the command overrides `.env`; a slow gateway may need more). A `GET` that fails with a connection error, a timeout or an HTTP 502, 503 or 504 is **retried twice** with a growing pause (0.5 s, then 1 s), so one brief blip no longer fails the command; anything else (a bad key, a 403, a 404 or 500, a certificate failure, a malformed answer) is reported at once, because trying again cannot change it. The single event-log `POST` is never retried. A message that says `(after 3 attempts)` means the retries were used up. A `403 Forbidden` means the API key is valid but not allowed to make that request.
- **Speed:** the reads a command needs do not depend on each other, so they run side by side: up to `UNIFI_PARALLEL_REQUESTS` at once (default 6; `--parallel N` before the command overrides it; `1` reads one request at a time, the old behavior, which is also handy when reading `--verbose` output). The result is identical either way: the same requests are made, the output keeps the controller's order, and warnings are shown in a fixed order. Still GET only (plus the one event-log query). The analysis itself is linear in the number of clients (8,000 clients take a few hundredths of a second instead of seconds). `client` looks the client up in the devices, the connected clients and the client history first, and reads the network configuration, the groups and the event log (a POST) only for a client that matched, so a name that matches nothing costs three reads and sends no POST. What each command reads is pinned by a test (`tests/test_needs.py`).
- **When a read fails:** required data stops the command with exit code 3; optional data warns and the command carries on with less. The list of connected clients and devices is required everywhere. Every legacy read is optional, **including the client history (`stat/alluser`)** for `query`, `client`, `diagnose` and the other reports (they warn that offline clients and reservations are unavailable), except for the two commands whose answer would be wrong without it: `new-clients` and `snapshot`/`diff` stop with exit code 3, so they never print a misleading list or save an incomplete snapshot.
- **`UNIFI_SITE_ID`:** a site name may contain spaces and non-ASCII letters, but not `/`, `\`, `?`, `#` or control characters, and at most 128 characters; it is also percent-encoded wherever it appears in a URL. **`--site NAME|REF|UUID`** (before the command, like `--timeout`) chooses the site for one run and beats `UNIFI_SITE_ID`: `hlp --site Lab diagnose`. It takes the same three kinds of value and is checked the same way (a bad or empty value is a usage error, exit code 64, before any request); an unknown site stops the command with exit code 3 and lists the sites there are (`hlp info` shows them). Every command that reads a site uses it. Saved snapshots and the notification state are kept apart by site: `snapshots/<site id>/` (see [snapshots](inventory.md#snapshots-and-diff) and [notifications](notifications.md)); an explicit `--dir` or `--notify-state` is used as given.

## Guided setup: `init`

`init` is the quickest way to a working `.env`. It asks for the controller's address, the API key and the site, whether to check the controller's certificate, and writes the files **in the current directory** (or `--dir`): `.env` (readable by you only), a commented `hlp.toml` (only if you have none) and a private `snapshots/` directory. **It contacts nothing**: the address and the key are checked for shape, not tried (add `--check` to try them once).

```bash
hlp init                                              # asks, with the current values as the defaults
cat key.txt | hlp init --url https://192.168.1.1 --api-key-stdin   # no questions
hlp init --url https://192.168.1.1 --no-input         # keep the key that is already in .env
hlp init --check                                      # and read the controller once afterwards
```

- **`--dir DIR`**: where to write (default: the current directory).
- **`--url URL`**, **`--site NAME`**, **`--verify true|false|FILE`**: the answers to the questions. `--verify` is `true` (the default: check the certificate), `false` (do not; the key is then sent to whatever answers), or the path of a CA file that vouches for the controller.
- **`--api-key-stdin`**: read the key from one line of standard input and ask nothing else (it needs `--url`). **A key is never an argument**, so it cannot end up in the process list or a shell history; when asked, it is typed without echo.
- **`--no-input`**: ask nothing, use only the options and what the existing `.env` holds.
- **`--force`**: do not ask before replacing the settings of an existing `.env`, or before turning certificate checking off.
- **`--check`**: afterwards read the controller once and show the `doctor` checks of the address, the key and the site (exit code 3 if one fails; the files are written either way).

An existing `.env` is **merged**, not replaced: the settings `init` manages are updated where they stand, and every comment, blank line and other setting stays. The old file is kept as `.env.bak` (also readable by you only). A `.env` or `hlp.toml` that is a symbolic link is never written through. A value is checked with the same rules every command uses before anything is written, and a refusal writes nothing; a value that a `.env` file cannot hold (a line break, or `${...}`, which would be expanded) is refused by name. The web server's setup wizard uses the same code.

## Checking your setup: `doctor`

`doctor` checks the **tool**, where `diagnose` checks the network: is it installed and configured right, are the settings safe, does the controller answer, accept the API key and have the site, and which optional endpoints does it offer? Run it first when something does not work, and paste its output into an issue.

```bash
hlp doctor                 # everything, including the controller
hlp doctor --offline       # only the installation and the settings files
hlp --env-file lab.env --site Lab doctor --json
```

It never stops at a broken setup (that is what it reports): the settings problems are checks that **fail**, and the controller checks after them are **skipped** with the reason. Each line has a status: `OK`, `WARN` (works, but look at it: a `.env` other users can read, certificate checking turned off, an optional endpoint missing, an expired ignore rule), `FAIL` (something every command needs does not work), `SKIP` (not run, and why) or `INFO`. Most lines come with the thing to do about it. The exit code is `0` unless a check **failed**, then `3` (the code for configuration and connection errors; `1` and `2` stay reserved for findings).

Abridged output (the sample is from the synthetic controller; a real one has one line for each check below):

```text
Configuration
  [OK  ] Required settings: UNIFI_URL and UNIFI_API_KEY are set and valid
  [WARN] TLS verification: certificate checking is off: the key is sent without checking who answers
         -> trust the controller's certificate with UNIFI_VERIFY_SSL=/path/to/its-certificate.pem

Controller
  [OK  ] Controller address: https, port 443 (the host is not shown)
  [OK  ] Controller and API key: answered, and accepted the API key
  [INFO] Controller version: UniFi Network 10.0.0; this tool was tested on 10.6.106 only, so a field may differ ...
  [OK  ] Site: found (1 site on the controller)

What the controller offers
  [OK  ] Devices (Integration API): 4 records
  [WARN] Client history (stat/alluser): unavailable (HTTP 404): without it, offline clients, reservations, `new-clients`, `snapshot` and `diff`
  [OK  ] Event log (the one read-only POST): answered (4 events in the last hour)

1 warning ... 
```

**What it will not do.** It does not change anything: no setting, no file, nothing on the controller. The controller checks make **one GET per read, without retries** (the first attempt tells the truth about a flaky link), and one event-log query for the last hour (the single read-only POST the tool is approved to send; `--no-events` skips it). The notification check is a **dry run**: it builds the message and names the destination kinds, and sends nothing.

**Safe to paste.** The API key is never shown. The controller's host name or address is replaced by `<controller>` everywhere, notification destinations are named by kind only (never a URL, topic, token, host or address), and a failed request is explained from what kind of failure it was, never by repeating the error text that has the address in it. File paths are your own. Options: `--offline`, `--no-events`, `--config FILE` (the settings file to check), `--json` (a [versioned document](schemas.md) with the same checks), and the global `--env-file`, `--site`, `--timeout` and `--parallel`, which `doctor` applies as the other commands do.

**The checks.** The ids are stable (never renamed or reused), so a script can rely on them:

| Check | What it looks at |
| ----- | ---------------- |
| `install.version` | the tool, Python and `requests` versions (information) |
| `config.env_file` | the `.env` file that was found (current directory, `--env-file` or `HLP_ENV`), or that the settings come from the environment; an explicit file that does not exist fails |
| `config.env_permissions` | that only you can read the `.env` (it holds the API key) |
| `config.env_contents` | what is in the `.env` file: a setting listed on several lines (the last one is used), a name that is not a setting (a typo, with a suggestion), a line that cannot be read, an empty setting, and a variable already set in the environment with another value (the environment wins over the file). Names and line numbers only, never a value |
| `config.settings_file` | `hlp.toml` (or `--config FILE`): found, valid, how many ignore rules and how many have expired |
| `config.environment` | that `UNIFI_URL` and `UNIFI_API_KEY` are set and valid (the first problem found, in the words of the usual error) |
| `config.tls` | whether certificates are verified, against what, and a warning when checking is off or the URL is plain `http://` |
| `config.limits` | the timeout, the number of parallel reads and the site that will be used |
| `notify.configured` | which notification destinations are configured, by kind only |
| `notify.dry_run` | that a message can be built for them; **nothing is sent** |
| `controller.address` | the scheme and port of `UNIFI_URL` (the host is never shown) |
| `controller.reachable` | that the controller answers and accepts the API key, with the usual causes explained (certificate, key rejected, no permission, timeout, no connection, not the controller) |
| `controller.version` | the Network version, and a note when it is not the one this tool was tested on |
| `controller.site` | that the site exists (and how many sites there are) |
| `endpoint.integration_devices`, `endpoint.integration_clients` | the two reads every command needs (a failure here is a failed check) |
| `endpoint.integration_device_detail`, `endpoint.integration_device_stats` | the per-device reads, tried on the first device |
| `endpoint.stat_device`, `endpoint.stat_sta`, `endpoint.stat_alluser`, `endpoint.stat_health`, `endpoint.rest_networkconf`, `endpoint.rest_wlanconf`, `endpoint.stat_rogueap` | the legacy reads the commands use for data the Integration API lacks |
| `endpoint.v2_speedtest`, `endpoint.v2_groups`, `endpoint.v2_firewall_policies`, `endpoint.v2_firewall_zone`, `endpoint.v2_firewall_matrix`, `endpoint.rest_portforward` | the v2 and port forward reads |
| `endpoint.events` | the event log, with one query for the last hour (the one read-only POST of the tool) |


## Finding your site: `info`

`info` is the quickest way to test your settings: it reads the controller's application info and its sites, nothing else, so a wrong address, key or certificate shows up here first. The sample is from the synthetic fixture:

```text
$ hlp info
Application: {'applicationVersion': '10.0.0'}
Site: Default ref=default id=site-1
```

There is one `Site:` line per site, with its name, internal reference (`ref`) and UUID (`id`). Any of the three is a valid `UNIFI_SITE_ID` or `--site` value; for any other command an unknown one stops it with exit code 3 and lists these. Names are cleaned of control characters before they are printed. `info` has no options of its own and reads only the two Integration API endpoints (every request is a GET).

## Seeing what the tool does: `--verbose`

`--verbose` (or `--debug`), given before the command, logs to **stderr** what the tool does, so the normal output on stdout is unchanged and can still be piped. It is the tool for diagnosing a slow run, a failing request or an undocumented endpoint:

```text
$ hlp --verbose wan --json > wan.json
[verbose] hlp 0.3.0: settings from /home/me/homelab-probe/.env; controller https://192.168.1.1:443, site default, timeout 15 s, TLS verification off
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
- **Same lines, other formats:** these lines are the `DEBUG` records of the tool's logger. `LOG_LEVEL` and `LOG_FORMAT=text|json` (see [Logging](logging.md)) give them with a timestamp and fields, or as JSON lines for a log collector.

## Trying it without a controller: `--demo`

`--demo` (before the command) runs the command on the synthetic network that the tests use: four devices, two clients, some neighbors, events and speedtests, with ages relative to now so it always looks recent. It is for a first look, for screenshots and for building on the tool without a controller at hand.

```bash
hlp --demo diagnose
hlp --demo topology
hlp --demo --verbose wifi
```

A demo run does not use your controller configuration or send data:

- No `.env`, environment variable, `--env-file`, `HLP_ENV` or `hlp.toml` is read (a settings file is used only if you name it with `--config`), so a real address or API key in your environment cannot end up in a demo.
- No controller is contacted: the address is on the `.invalid` top-level domain, which never resolves, and the answers come from the data packaged with the tool (`homelab_probe/demo/controller.json`).
- Nothing can be sent: `--notify` is refused. `doctor` (it checks your real setup), `snapshot` and `diff` (they would mix synthetic data into your own saved snapshots) are refused too. All of these are usage errors (exit code 64).
- Except for the controller-independent `completion` command, a line on stderr says `Demo mode: synthetic data, no controller is contacted.`, so stdout (and `--json`) stays clean and nobody mistakes the output for a real network.

`--site` is accepted (the demo has one site, `default`), as are `--timeout`, `--parallel` and `--verbose`. The data is the same on every run, apart from the ages, so it is also what the sample output in this documentation is made from.

## Shell completion

`hlp completion bash`, `completion zsh` and `completion fish` print a completion script for the installed `hlp` command: it completes the commands, every option (with its short form), the fixed values (`--fail-on info warning critical`, the query kinds, the Wi-Fi bands, the `diagnose --only` and `--skip` areas one at a time, even in a comma list), and file names for the options that take a file. The command needs no `.env` and never contacts the controller. The scripts are generated from the program's own options, not written by hand, so a new option is completed as soon as it exists; a test checks that every command and option is in every script.

```bash
# bash: for this shell, or save it where bash-completion looks (for example ~/.local/share/bash-completion/completions/hlp)
source <(hlp completion bash)

# zsh: put it in a directory on $fpath, then restart the shell (or run compinit)
hlp completion zsh > "${fpath[1]}/_hlp"

# fish
hlp completion fish > ~/.config/fish/completions/hlp.fish
```

It completes the `hlp` command that `pip install .` or `uv tool install` creates, not `uv run hlp.py` (there the shell sees `uv`). The bash script also works in the old bash 3.2 that macOS ships; free text values (a search, a client name) fall back to the shell's file name completion in bash. The zsh script uses `_arguments`, so descriptions of the options show next to the completions.

## Troubleshooting

- **`UNIFI_URL is not set` / `UNIFI_API_KEY is not set`**: copy `example.env` to `.env` and fill it in, in the directory you run the command from, or point to it with `--env-file FILE` or `HLP_ENV`.
- **`UNIFI_URL uses http://`**: use `https://` (the API key would be sent in clear text), or set `ALLOW_INSECURE_HTTP=true` for a trusted lab network.
- **`... is accessible to other users`**: run the `chmod 600` command in the warning; the file holds your API key. On a file system that does not keep Unix permissions (a Windows drive mounted in WSL, some network shares) the mode cannot be changed and the warning stays; keep the file on a normal Linux or macOS file system, or in your home directory.
- **`env file not found`**: the file named with `--env-file` or `HLP_ENV` does not exist.
- **`UNIFI_VERIFY_SSL must be one of ...` / `UNIFI_SITE_ID ... cannot be part of a site name`**: fix the value in `.env`; the message lists what is accepted.
- **`401 Unauthorized`**: the API key is invalid or was revoked; create a new one.
- **`TLS certificate verification failed`**: point `UNIFI_VERIFY_SSL` at the CA file that signed the controller certificate, or install a valid certificate on the controller; `UNIFI_VERIFY_SSL=false` skips the check and is a last resort. If `UNIFI_VERIFY_SSL` already names a file, the message distinguishes an untrusted certificate, an invalid CA bundle certificate, and a certificate that is trusted but not valid for the host name or IP address in `UNIFI_URL`.
- **A command is slow or fails and you want to see why**: run it again with `--verbose` (see above) to see each request, its status and time, and any retries.
- **Connection errors or timeouts**: check `UNIFI_URL` and that the controller is reachable from this machine. `timed out after 15 s (after 3 attempts)` means a slow gateway: raise `UNIFI_TIMEOUT` or pass `--timeout 60`.
- **`403 Forbidden`**: the API key is valid but lacks access to that request; check the key under Settings > Control Plane > Integrations.
- **`UNIFI_VERIFY_SSL names a CA bundle that does not exist`**: fix the path (it is relative to the directory you run from).
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
