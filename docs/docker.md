# Docker

One image runs both halves of the tool: the web server (`hlp serve`, the default command) and the command line (`docker run ... diagnose`). It starts **with no configuration at all**, in the [setup mode](web.md#first-run-setup-a-server-with-no-settings), and keeps everything it writes in one volume, `/data`.

The image is **not published yet** (publishing to GHCR comes with a release, [#188](https://github.com/jeffholst/homelab-probe/issues/188)). Until then build it from a checkout of the project or of a tag; the build context holds the code and nothing else (no `.env`, no `snapshots/`, see [What is in the image](#what-is-in-the-image)). The web app itself is not part of the image yet either: the server answers the [API](web.md) (`/` says so in one line), and the image's web build stage is a placeholder for the app. Where this page says to open the server in a browser, that applies once the image includes the app; the API calls shown here work now.

## Run it

```bash
docker build -t homelab-probe .
docker run -d --name hlp -p 127.0.0.1:8787:8787 -v hlp-data:/data homelab-probe
docker logs hlp
```

or with compose, which also drops the container's privileges and makes its root file system read-only ([`compose.yaml`](../compose.yaml)):

```bash
docker compose up -d --build
docker compose logs hlp
```

The log shows the one-time **setup token** in its first lines (it is new at every start and is held in memory only, so it is never in a file or in `compose.yaml`):

```text
Not configured (UNIFI_URL is not set).
Serving on http://0.0.0.0:8787 (Ctrl-C to stop).
Not set up: open the server in a browser to finish the setup with this token: 7Qk...
```

**There is no web app in the image yet**: `http://localhost:8787/` answers a one-line JSON notice (`API only: no web app is built in yet`), and the log line above, which the server prints for every install, says "browser" because the screens come in a later stage. Until the image includes them, finish the setup through the [setup API](web.md#first-run-setup-a-server-with-no-settings): the token goes in an `X-Setup-Token` header, and every `POST` needs a JSON body and an `Origin` that names the server as you reach it. Once the image serves the web app, you open `http://localhost:8787` instead and the same steps are screens. A minimal sequence with `curl` (the API key is read without echo and sent on standard input, so it is neither in the process list nor in the shell history):

```bash
TOKEN=$(docker logs hlp 2>&1 | sed -n 's/.*with this token: //p')
api() { curl -s -H "Origin: http://localhost:8787" -H "Content-Type: application/json" -H "X-Setup-Token: $TOKEN" "$@"; }
api http://localhost:8787/api/v1/setup/status
printf 'API key: ' && read -rs KEY && echo
printf '{"url": "https://controller.example.lan", "site": "default", "api_key": "%s"}' "$KEY" \
    | api -X POST -d @- http://localhost:8787/api/v1/setup/draft
api -X POST -d '{}' http://localhost:8787/api/v1/setup/connection
printf '{"username": "admin", "password": "%s"}' "$ADMIN_PASSWORD" \
    | api -X POST -d @- http://localhost:8787/api/v1/setup/finish
```

`status` shows the draft (never the key), `draft` changes it, `connection` tests the controller and lists its sites, and `finish` writes the settings and creates the first administrator (set `ADMIN_PASSWORD` to a password of at least 12 characters first). `finish` needs a draft whose connection test passed. A controller with a self-signed certificate also needs `certificate` and a `verify` choice before `connection`; every route, body and error is in [the setup API](web.md#first-run-setup-a-server-with-no-settings). The settings are written to `/data/.env`, which is read again at every start, so the next start needs no token. To choose the token yourself, set `HLP_SETUP_TOKEN` (at least 16 characters, in the environment of the container and not in a `.env`); it is then not printed.

You can also skip the guided setup. A `.env` in the volume (or the environment: `-e` or `--env-file`) starts the server configured; with no administrator yet it starts in the [admin mode](web.md#first-run-setup-a-server-with-no-settings), where the same token creates the first one. Create one up front with `docker run --rm -it -v hlp-data:/data homelab-probe web-user add NAME --role admin`.

### What the default command does

`serve --host 0.0.0.0 --allowed-host localhost --scheduler`:

- **`--host 0.0.0.0`**: inside a container the server must listen on every interface or Docker's port publishing cannot reach it. That is why the compose file and the examples publish the port on `127.0.0.1` only.
- **`--allowed-host localhost`**: listening on every address requires at least one allowed `Host` name ([Reaching it from other machines](web.md#reaching-it-from-other-machines)). `localhost` is accepted anyway, so this default answers the browser on the machine that publishes the port and **refuses any other name** (`400`) until you add it. The container's own health check uses `127.0.0.1`, also always accepted.
- **`--scheduler`**: the diagnose and snapshot jobs run inside the server once it is set up ([The scheduler](web.md#the-scheduler)).

A `command` in compose, or arguments after the image name in `docker run`, **replace** this default as a whole. To add a name that other machines use, repeat the whole command with one more `--allowed-host` (never a wildcard):

```bash
docker run -d --name hlp -p 127.0.0.1:8787:8787 -v hlp-data:/data homelab-probe \
    serve --host 0.0.0.0 --allowed-host localhost --allowed-host hlp.example.lan --scheduler
```

(in `compose.yaml` the same words are the `command:` list.) Other options of [`serve`](web.md#running-the-server-serve) go the same way: `--read-only`, `--port`, `--forwarded-allow-ips`. If you change `--port`, the image's health check, which asks port 8787, reports the container unhealthy; override it with the `healthcheck:` of compose or the health options of `docker run`.

### The command line

The entry point is `hlp`, so the arguments after the image name are the command and its options. `--demo` is a global option and goes before the command:

```bash
docker run --rm homelab-probe --version
docker run --rm homelab-probe --demo diagnose              # the synthetic network: no controller, no key
docker run --rm homelab-probe --demo query devices
docker run --rm --env-file .env -v hlp-data:/data homelab-probe diagnose --notify
```

The last line is a scheduled run for [cron or a systemd timer](scheduling.md#docker); it shares the volume (and so the notification state) with the server. `docker run` exits with the tool's own exit code.

## Volumes and permissions

`/data` is the working directory and the **only** place the server writes (besides `/tmp`). It holds `.env`, `hlp.toml`, `users.json` (password hashes), `audit.log*`, `snapshots/` (saved inventories, the notification state, triage and notes) and `certs/` (a pinned controller certificate). It is declared a `VOLUME`, so even without `-v` Docker keeps it in an anonymous volume; use a **named volume** (`-v hlp-data:/data`, as above) so you can find it again.

The process runs as **uid 10001**, not root. A new named volume takes the owner of `/data` in the image, so nothing is needed. A **directory from the host** (`-v /srv/hlp:/data`) keeps its own owner and the server will say it cannot write there until you give it to the container's user (`sudo chown 10001 /srv/hlp`; keep it private, `chmod 700`). A file you mount into it, such as `hlp.toml` or `.env` (`-v "$PWD/hlp.toml:/data/hlp.toml:ro"`), only has to be readable by uid 10001. A file mounted `:ro` cannot be changed by the setup or the settings editor either, which is the point of mounting it that way.

The API key and the passwords are **not in the image**: they are in the volume, in the environment you give the container, or in a file you mount.

- `--env-file .env` of `docker run` and `env_file:` of compose hand the settings over as environment variables. Docker reads that file literally: **no quotes and no `export`** (`UNIFI_URL="https://..."` keeps the quote marks and the tool refuses it with `UNIFI_URL must look like https://host[:port]`; a line starting with `export` makes Docker refuse the file as an `invalid env file`). A variable set this way beats `/data/.env` and the guided setup cannot save over it: it then shows you the finished `.env` to copy instead.
- A controller on your LAN is reached from the container like from any host. For a controller on the Docker host itself use the host's address, not `127.0.0.1` (inside the container that is the container). A self-signed certificate is handled as everywhere else ([configuration](configuration.md#configure)): the guided setup can pin it into `certs/controller.pem`, or mount a CA file and point `UNIFI_VERIFY_SSL` at it.

### Read-only root file system

The image is made to run with `--read-only`: it writes no `.pyc` files and keeps everything in `/data` and `/tmp`. Give it a writable `/tmp` as a `tmpfs`:

```bash
docker run -d --name hlp -p 127.0.0.1:8787:8787 -v hlp-data:/data \
    --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges homelab-probe
```

That is what the compose file does (`read_only`, `tmpfs`, `cap_drop`, `no-new-privileges`), and what the CI smoke test of the image runs. Nothing outside `/data` and `/tmp` is writable, and no Linux capability is needed.

## Backups

Two different things, for two different needs:

- **The application backup** ([`/api/v1/backup`](web.md#backup-apiv1backup)): an encrypted archive of the settings, accounts, notes, triage state and snapshots, made and restored from the web server by an administrator. It is the same format for a native install and for this image, so it moves a setup from one to the other, and it is the one to keep off the machine. It contains no setting that came from the container's environment.
- **A copy of the volume**: everything in `/data`, as it is, including the `.env` with the controller key and the account hashes. Keep it as private as the key. It does not need the server's help:

  ```bash
  # back up (stop the container first for a copy that is not caught mid-write)
  docker run --rm --user 0 --entrypoint tar -v hlp-data:/data:ro -v "$PWD:/backup" homelab-probe \
      czf /backup/hlp-data.tgz -C /data .
  # restore into a new volume
  docker volume create hlp-data-new
  docker run --rm --user 0 --entrypoint tar -v hlp-data-new:/data -v "$PWD:/backup:ro" homelab-probe \
      xzf /backup/hlp-data.tgz -C /data
  docker run --rm --user 0 --entrypoint chown -v hlp-data-new:/data homelab-probe -R 10001 /data
  ```

  (running as user 0 is only for these one-off commands, which must read and give away files the server user owns. Compose names its volume `<project>_hlp-data`, see `docker volume ls`.)

## Update

The image holds the code and nothing else, so an update replaces it and keeps the volume:

```bash
git pull && docker compose build --pull && docker compose up -d     # while the image is built from a checkout
docker compose pull && docker compose up -d                        # once an image is published and compose.yaml names it
```

With plain `docker run`: build or `docker pull` the new image, then `docker stop hlp && docker rm hlp` and run it again with the same `-v hlp-data:/data`. The sessions of the web interface end (they are kept in memory); the settings, accounts and snapshots stay. The scheduler's first snapshot after a restart waits until the newest one is as old as its interval, so a restart does not fill the directory.

## Logs

The image sets `LOG_FORMAT=json` and logs to stderr, so `docker logs` and every logging driver see **one JSON object per line** ([logging](logging.md)). The default level is `WARNING`; `LOG_LEVEL=INFO` (set in `compose.yaml`) adds the scheduler's runs and a line per request, including the health check's own `GET /healthz` every 30 seconds. A few lines are plain text on purpose, because they are for the person who started the server: the setup token and the other start-up notices.

Docker keeps container logs without limit by default, so cap them:

```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

(already in `compose.yaml`; for `docker run` the same two settings are `docker run --log-opt max-size=10m --log-opt max-file=5 ...`). `docker compose logs --no-log-prefix hlp | grep '^{' | jq 'select(.level != "INFO")'` shows only the warnings. The audit log of accounts and settings is a file in the volume (`audit.log`), not part of these logs.

## Reverse proxy and TLS

A login sends a password, so do not publish the port on the network as plain HTTP. Keep `127.0.0.1:8787:8787` and put a proxy that terminates TLS in front, with the headers and the `Host` and `Origin` handling described in [Reaching it from other machines](web.md#reaching-it-from-other-machines) (its Caddy and nginx examples apply unchanged). Two settings of the container matter:

- **The name your users type**: add it with `--allowed-host hlp.example.lan` (repeatable), and have the proxy pass `Host` and `Origin` unchanged.
- **The proxy's address**, so that the client address and the HTTPS scheme come through (the session cookie becomes `Secure`): `--forwarded-allow-ips ADDRESS`. The server sees the **proxy's address as the container sees it**: for a proxy on the Docker host that is the gateway of the Docker network (`docker network inspect` shows it, usually in `172.16.0.0/12`), for a proxy in the same compose project it is that container's address. Name the narrowest address or network that holds only your proxy. `*` and `0.0.0.0/0` are refused, and without the option no `X-Forwarded-*` header is believed.

```yaml
    command:
      - serve
      - --host
      - 0.0.0.0
      - --allowed-host
      - localhost
      - --allowed-host
      - hlp.example.lan
      - --forwarded-allow-ips
      - 172.18.0.0/16         # the network of the proxy, as the container sees it
      - --scheduler
```

If the proxy is another container, do not publish the port at all: put both on one compose network and let the proxy reach `hlp:8787` (add `hlp` to the allowed hosts only if the proxy sends it as `Host`).

## What is in the image

- **Stages.** The first stage is the (placeholder) web build, the second builds one wheel for the tool and for each dependency, the last installs only those wheels into `python:3.13-slim`, without a package index, a compiler or the source tree. The docs, tests and `.git` are not in it.
- **No secret or data.** `.dockerignore` excludes everything and lets back in only what the `COPY` lines name (`pyproject.toml`, `README.md`, `homelab_probe/`, `docs/schemas/`), and then removes `.env` files, `hlp.toml`, `users.json`, `audit.log`, `snapshots/`, `certs/`, caches and `.git` wherever they are. Keep it that way when you add a stage. The CI check of the image also looks for those files in the finished image.
- **Labels and size.** The standard `org.opencontainers.image.*` labels name the source and the licence; the image is about 270 MB, most of it the Python base and the `cryptography` wheel.
- **Health.** `HEALTHCHECK` asks `/healthz` with Python's standard library (there is no `curl` in the image), every 30 seconds.

## Checking an image

`tools/docker_smoke.sh IMAGE` is what CI runs after it builds the image on every pull request (nothing is published, and it never contacts a controller): `--version`; a non-root user; one command in demo mode; a container with no configuration becoming healthy, showing the setup token in its log and answering `needs_setup` on `/api/v1/meta`; a `Host` that was not allowed refused; a read-only root file system with `/data` and `/tmp` writable; a `--demo serve` container reached through the published port with a login and a report; and no `.env`, `hlp.toml`, accounts, audit log or snapshots in the image. Run it yourself after a local build:

```bash
docker build -t homelab-probe:ci .
tools/docker_smoke.sh homelab-probe:ci
```
