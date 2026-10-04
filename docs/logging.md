# Logging

What the tool says *about itself*, as opposed to its output: a degraded read, a request to the controller, the outcome of a notification. Command output (tables, JSON, CSV) is not logging and never changes with the settings on this page.

Everything is written to **stderr**, one record per line, by the `unifi_sentinel` logger tree (the Python standard library, no extra dependency). A record is always a single clean line: line breaks and control characters in names are removed, a very long text is cut at 2,000 characters, and nothing secret is ever written (see [What never appears](#what-never-appears)).

## Levels

| Level | Meaning | Examples |
|---|---|---|
| `ERROR` | An operation failed | (the command line reports failures with `ERROR: ...` and an exit code, not through the log) |
| `WARNING` | Degraded: an optional read failed, a delivery failed | `warning`, a failed `notify.delivery`, `watch.unavailable` |
| `INFO` | Lifecycle and outcomes | a delivered `notify.delivery`, a `watch.pass` |
| `DEBUG` | The request trace, with identifiers in full | `http.request`, `http.retry`, `snapshot.read`, `run.settings` |

The default is `WARNING`. `--verbose` (before the command) means `DEBUG`. `LOG_LEVEL` in `.env` or the environment (`DEBUG`, `INFO`, `WARNING` or `ERROR`, in any case) sets it without the option; `--verbose` wins.

**Personal data only at DEBUG.** `INFO` and above carry counts, finding codes and fixed words, never a client name, MAC address or IP address. `DEBUG` lines contain paths, the controller's address and the site, so redact them before pasting them into an issue.

## Formats

| `LOG_FORMAT` | What you get |
|---|---|
| (unset) | The command-line format, exactly what the tool always printed: `Warning: ...` for a degraded read and, with `--verbose`, `[verbose] ...` lines. At `INFO` or `DEBUG` this format also shows the other records as `[verbose] ...` lines. |
| `text` | One line per record: UTC time with milliseconds, level, logger, event, message, then `key=value` fields. |
| `json` | One JSON object per line, for a log collector (set by the Docker image). |

An invalid value is a configuration error (exit code 3).

```text
2026-10-04T06:07:42.429Z INFO    unifi_sentinel.notify notify.delivery notify ntfy -> HTTP 200 (212 ms) request_id=3f9c2a71b0de site=default destination=ntfy delivered=true reason="HTTP 200" duration_ms=212
2026-10-04T06:07:42.430Z WARNING unifi_sentinel.logs warning legacy stat/health unavailable, controller health and WAN checks were skipped: HTTP 500 request_id=3f9c2a71b0de site=default
```

```json
{"ts": "2026-10-04T06:07:42.429Z", "level": "INFO", "logger": "unifi_sentinel.notify", "msg": "notify ntfy -> HTTP 200 (212 ms)", "event": "notify.delivery", "request_id": "3f9c2a71b0de", "user": null, "site": "default", "destination": "ntfy", "delivered": true, "reason": "HTTP 200", "duration_ms": 212}
```

The JSON fields are always `ts` (UTC, ISO 8601, milliseconds, `Z`), `level`, `logger`, `msg`, `event`, `request_id`, `user` and `site` (`null` when there is none), followed by the event's own fields. A field named like one of those is renamed with a `field_` prefix. Non-ASCII characters are escaped, so one record is one line in any tool.

- **`request_id`** is a random id of the run (twelve hex digits). It is carried into every record of the run, including those from the threads of a parallel read, so the lines of one run can be picked out of a shared log. `site` is the site the run reads. `user` is empty until the web interface adds accounts.
- **`event`** is a stable name, never renamed or reused; a collector can filter on it. The names are in the next table, and a test fails if the code emits one that is not listed, or one is listed that nothing emits.

## Events

| Event | Level | Fields | Meaning |
|---|---|---|---|
| `warning` | WARNING | | A message the command also prints as `Warning:` (an optional read failed, a setting is loose, a state file was damaged). |
| `http.request` | DEBUG | `method`, `path`, `outcome`, `duration_ms` | One attempt to read from the controller (DEBUG): method, path, outcome, milliseconds. |
| `http.retry` | DEBUG | `method`, `path`, `pause_s`, `attempt`, `attempts` | A read is about to be retried after a pause (DEBUG). |
| `snapshot.read` | DEBUG | | What one collection of the controller's data read (DEBUG): counts only. |
| `run.settings` | DEBUG | | Which settings a run uses (DEBUG, with `--verbose`): the controller address, the site, limits. |
| `run.summary` | DEBUG | | How many requests a run made and how long they took (DEBUG, with `--verbose`). |
| `notify.delivery` | INFO or WARNING | `destination`, `delivered`, `reason`, `duration_ms` | The outcome of one notification destination (INFO delivered, WARNING failed): the kind, a fixed reason, milliseconds; never the message, the URL or a recipient. |
| `watch.pass` | INFO | `findings`, `complete`, `retry_s` | One pass of `diagnose --watch` finished (INFO): how many findings, whether the read was complete. |
| `watch.unavailable` | WARNING | `reason`, `findings`, `complete`, `retry_s` | A pass of `diagnose --watch` could not read the controller or got only part of its data (WARNING). |

## What never appears

Every record passes one filter before it is written, whichever format is in use:

- the **API key**, the notification **URLs and tokens**, the mail account (host, user, password, addresses), and any session id, password or setup token the web interface registers, in the spelling they were given and URL-encoded or JSON-escaped (values shorter than six characters are not matched, because they would garble every line);
- the **value** of an `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie` or `X-API-KEY` header (the rest of that line), a `Bearer` or `Basic` credential, the `user:password@` part of a URL, and anything written as `password=...`, `token: ...`, `"secret": "..."`, `api_key=...` or `session_id=...`;
- the value of any record **field** whose name says it is sensitive (`password`, `token`, `secret`, `api_key`, `authorization`, `cookie`, `session`, `csrf`, `credentials`);
- an exception's text beyond its type and message: no traceback, no local values.

They show as `[redacted]`. This is a safety net, not a licence: the code does not pass secrets to the logger in the first place, and a test pushes poisoned values through every channel (message, arguments, fields, exceptions, headers) in all three formats.

## Docker and compose

The official image will set `LOG_FORMAT=json` and write to stderr, so `docker logs` and the log driver see one JSON object per line. Until then, for the container in [Running on a schedule](scheduling.md#docker):

```yaml
services:
  sentinel:
    environment:
      LOG_FORMAT: json
      LOG_LEVEL: INFO
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

`docker compose logs --no-log-prefix sentinel | jq 'select(.level != "INFO")'` then shows only the warnings.

## Turning DEBUG on safely

```bash
unifi-sentinel --verbose wan > wan.json 2> wan.log      # one run, then read or redact wan.log
LOG_LEVEL=DEBUG LOG_FORMAT=json unifi-sentinel wan      # the same as JSON lines
```

`DEBUG` shows every request path, the controller's address and the site: fine for you, redact them before sharing. The API key is never in it. Do not leave `DEBUG` on in a scheduled job that writes to a shared log.
