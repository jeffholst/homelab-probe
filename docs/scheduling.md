# Running on a schedule

`diagnose --notify` is meant to run unattended: every few minutes it checks the network and sends a message only when something changed. This page shows how to run it from **cron**, a **systemd timer**, **launchd** (macOS) and **Docker**, and what is the same for all of them. Every snippet below was run once (the units with `systemd-analyze verify`, the plist with `plutil` and `launchctl`, the container with `docker build` and `docker run`) against an unreachable test controller, never a real one; the [Docker image](docker.md) is also checked in CI.

## Before you pick a scheduler

- **The working directory matters.** The settings are read from `.env` and `hlp.toml` in the **current directory** (or `--env-file`, `HLP_ENV` and `--config`), and the notification state and saved snapshots are written to `snapshots/` under it. A scheduler starts the job somewhere else (cron: your home directory; systemd: `/`), so every example below sets the directory first. Started from the wrong place the tool stops with `ERROR: UNIFI_URL is not set` and exit code 3.
- **Install it, then run the installed command.** A scheduler has a minimal `PATH`: under cron `uv` is usually not found (`uv: command not found`, exit code 127), and `uv run` would also resolve dependencies at every run. The examples use a virtual environment next to the project, which needs no `uv`, no network access to PyPI at run time and no `PATH`:

  ```bash
  sudo install -d -o "$(id -un)" /opt/homelab-probe
  git clone https://github.com/jeffholst/homelab-probe /opt/homelab-probe
  cd /opt/homelab-probe
  python3 -m venv venv && venv/bin/pip install .
  cp example.env .env && chmod 600 .env && mkdir -p snapshots     # then edit .env: address, key, notification settings
  ```

  The command is then `/opt/homelab-probe/venv/bin/hlp`. Use your own path (`/home/me/homelab-probe`, `/Users/me/homelab-probe`) everywhere below.
- **The first run reports everything.** With no notification state yet, today's findings would all be "new". Record them as already reported once, by hand, before you schedule anything:

  ```bash
  cd /opt/homelab-probe && venv/bin/hlp diagnose --notify --notify-baseline
  ```

  Add `--notify-dry-run` to a normal run to see the message without sending it ([Notifications](notifications.md)).
- **Exit codes.** `0` means nothing at or above `--fail-on`; `1` (a warning) and `2` (a critical finding) mean the checks ran and **found something**; `3` means the tool could not do its job (bad configuration, the controller could not be reached, or a notification that could not be delivered while the findings gave `0`), `4` and `64` are for `client` and usage errors ([the table](../README.md#exit-codes)). A scheduler should treat `1` and `2` as normal, since the notification is how you hear about findings, and treat `3` and above as "this did not run". The examples below do that.
- **How often.** Every 5 to 15 minutes is plenty. Run the command once with `--verbose` (before the command) to see how many requests a run makes, and raise `--timeout` or lower `--parallel` for a slow gateway. The event checks look back 24 hours by default, so an event-based warning stays in the findings until the event is that old; pass `--since 1h` (or `--no-events`) for a job that should only react to what is wrong now ([Diagnose](diagnose.md#recent-events)).
- **Runs must not overlap.** A run that takes longer than the interval would start a second one on top of it, and two runs share one state file. A systemd timer and launchd never start a job that is still running; cron does, so it needs the guard shown in its section.
- **Keep the key private.** `.env` is `chmod 600`, owned by the user the job runs as, and the key never goes into a crontab line, a unit file or a plist: those are readable by more people than `.env` is.

## cron

```text
*/15 * * * * flock -n /tmp/homelab-probe.lock /bin/sh -c 'cd /opt/homelab-probe 2>/dev/null || { echo "hlp failed: cannot enter /opt/homelab-probe"; exit 3; }; if [ ! -d snapshots ] || [ ! -w snapshots ] || ! ( : >>snapshots/homelab-probe.log ) 2>/dev/null; then echo "hlp failed: snapshots/homelab-probe.log is not writable"; exit 3; fi; venv/bin/hlp diagnose --notify >/dev/null 2>>snapshots/homelab-probe.log; rc=$?; [ "$rc" -lt 3 ] || echo "hlp failed (exit $rc), see snapshots/homelab-probe.log"; exit "$rc"'
```

cron mails everything a job prints, and a `--notify` run always prints (the findings on stdout, a `Notification: ...` line on stderr), so this line sends stdout to nowhere, appends stderr to a log in the git-ignored `snapshots/` directory, and prints one line, which cron mails to you, **only** when the exit code is 3 or more. Findings reach you through the notification, not through cron mail. If notification delivery fails while findings produce exit code 1 or 2, check the log: the finding code is preserved, so cron does not mail an error. Set `MAILTO=you@example.com` at the top of the crontab to choose who gets that mail (a machine with no mail setup silently drops it; then look at the log).

- **Without notifications:** let cron mail you the findings, and fail only on what is serious: `*/15 * * * * cd /opt/homelab-probe && venv/bin/hlp diagnose --fail-on critical || notify-me` (`notify-me` is whatever you use to alert yourself; the exit code is `2` for a critical finding and `3` for an error).
- **Overlap guard (Linux):** the example uses `flock` to run `/bin/sh -c '...'` while holding the lock. The shell builtin `cd` therefore runs inside the locked shell, not as a command for `flock` to execute. A second run that finds the lock held exits at once with code 1 instead of starting. macOS has no `flock`; use launchd there.
- Edit with `crontab -e`; `%` has a special meaning in a crontab, so write `\%` if a command needs one.

## systemd timer

A system service that runs once and a timer that starts it, in `/etc/systemd/system/`:

```ini
[Unit]
Description=Homelab Probe health check
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=homelab-probe
WorkingDirectory=/opt/homelab-probe
ExecStart=/opt/homelab-probe/venv/bin/hlp diagnose --notify
# 1 and 2 mean "findings" (a warning, a critical problem), not that the tool failed: the notification is how you
# hear about them. The unit fails only for 3 (configuration, connection or delivery error), 4 and 64.
SuccessExitStatus=1 2
NoNewPrivileges=true
PrivateTmp=true
```

Save it as `homelab-probe.service`, and the timer as `homelab-probe.timer`:

```ini
[Unit]
Description=Run the Homelab Probe health check every 15 minutes

[Timer]
OnCalendar=*:0/15
RandomizedDelaySec=30
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo useradd --system --home-dir /opt/homelab-probe homelab-probe
sudo chown -R homelab-probe /opt/homelab-probe            # the user must read .env and write snapshots/
systemd-analyze verify /etc/systemd/system/homelab-probe.service /etc/systemd/system/homelab-probe.timer
sudo systemctl daemon-reload && sudo systemctl enable --now homelab-probe.timer
systemctl list-timers homelab-probe.timer                   # when it last ran and runs next
sudo systemctl start homelab-probe.service                  # run it now, without waiting
journalctl -u homelab-probe.service -n 20                   # what it printed
```

`SuccessExitStatus=1 2` is the line that matters: without it every run that finds a warning would show the unit as **failed**, and `systemctl --failed` would be noisy all day. A failed unit can mean a configuration, connection or usage error, or a notification delivery failure when findings otherwise produce exit code 0. But delivery failure does not always fail the unit: when findings produce exit code 1 or 2, that code is preserved even if notification delivery also fails, and the unit is marked successful. A successful unit does not prove delivery; check the journal for `Notification to ...: FAILED` lines. `systemd-analyze verify` printing nothing means the files are well formed. `Persistent=true` runs a missed check after the machine was off; a timer does not start the service again while it is still running, so runs cannot overlap.

## launchd (macOS)

A LaunchAgent in `~/Library/LaunchAgents/com.example.homelab-probe.plist` runs while you are logged in (a LaunchDaemon in `/Library/LaunchDaemons` would run without a login, as root unless you add `UserName`, and is rarely worth it for a home network):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.homelab-probe</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/me/homelab-probe/venv/bin/hlp</string>
    <string>diagnose</string>
    <string>--notify</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/me/homelab-probe</string>
  <key>StartInterval</key>
  <integer>900</integer>
  <key>StandardOutPath</key>
  <string>/dev/null</string>
  <key>StandardErrorPath</key>
  <string>/Users/me/homelab-probe/snapshots/homelab-probe.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
```

```bash
plutil -lint ~/Library/LaunchAgents/com.example.homelab-probe.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.homelab-probe.plist
launchctl kickstart -k gui/$(id -u)/com.example.homelab-probe          # run it now
launchctl print gui/$(id -u)/com.example.homelab-probe | grep -E "last exit code|runs|run interval"
launchctl bootout gui/$(id -u)/com.example.homelab-probe               # stop and remove it
```

launchd has no notion of "findings": `last exit code = 1` or `2` means findings were reported, while `3` or more means a configuration, connection or usage error, or a notification delivery failure when findings otherwise produce exit code 0. Delivery can also fail while a finding's exit code 1 or 2 is preserved, so check `snapshots/homelab-probe.log` for `Notification to ...: FAILED` lines. launchd does not retry based on the exit code; it simply runs again after `StartInterval` (900 seconds). A job is not started again while its previous run is still going (tried with a job that sleeps longer than its interval), so there is no overlap. Name the file and `Label` after yourself instead of `com.example`; keep the key in `.env`, never in the plist.

## Docker

The [image](docker.md) runs the command line as well as the web server, so a scheduler can start it like any other command. Build it from a checkout of the project (or of a tag; nothing is published yet): the image contains the code, no key and no data. The server has its own [scheduler](web.md#the-scheduler) (`serve --scheduler`, the image's default command), which shares the notification state with the runs below, so you need one of the two, not both.

```bash
docker build -t homelab-probe .
docker run --rm --env-file .env -v homelab-probe-data:/data homelab-probe diagnose --notify --notify-baseline   # once
docker run --rm --env-file .env -v homelab-probe-data:/data homelab-probe diagnose --notify                     # the scheduled run
```

- **Name the command.** The image's default command is the web server, so a run with nothing after the image name starts `serve`, not a check. The scheduled run above says `diagnose --notify`.
- **The key stays out of the image.** `--env-file .env` hands the settings to the container as environment variables at run time. Docker reads that file literally: **no quotes and no `export`** (`UNIFI_URL="https://..."` keeps the quote marks and the tool refuses it with `UNIFI_URL must look like https://host[:port]`; a line that starts with `export` makes Docker refuse the file as an `invalid env file`). A `.env` in the volume (`/data/.env`) is read too, but it must be readable by uid 10001.
- **Keep the build directory clean.** Docker sends the build context to the daemon; the image's `.dockerignore` lets in only the files the build copies, so `.env` and `snapshots/` cannot end up in the image or the context ([what is in the image](docker.md#what-is-in-the-image)).
- **State survives in the named volume** mounted at `/data` (the notification state is `snapshots/<site id>/notify-state.json` under it); without it every run would think everything is new. It is the same volume the server uses. A settings file is mounted into it: `-v "$PWD/hlp.toml:/data/hlp.toml:ro"`, and `--config FILE` also works.
- **Exit codes pass through:** `docker run` exits with the tool's code (`3` for an error, as above), so any of the schedulers above can run it. For systemd, `ExecStart=/usr/bin/docker run --rm --env-file /opt/homelab-probe/.env -v homelab-probe-data:/data homelab-probe diagnose --notify` with `SuccessExitStatus=1 2` is the same service as before.
- **A controller on your LAN** is reached from the container like from any host; for a controller on the Docker host itself use the host's address, not `127.0.0.1` (inside the container that is the container). A self-signed certificate is handled as usual with `UNIFI_VERIFY_SSL` pointing at a CA file, which you mount.

## `--watch` or a schedule?

`diagnose --watch SECONDS` is for a terminal you are looking at: it prints the findings once and then only what changed, until Ctrl-C, and it keeps its memory in the process. A schedule is for everything unattended: each run is a fresh process, the memory is the notification state file, a crash or a reboot loses nothing, and the scheduler simply starts the next run. `--watch` cannot be combined with `--notify` or `--json` ([Diagnose](diagnose.md#diagnose)).
