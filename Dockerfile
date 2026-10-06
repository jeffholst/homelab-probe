# syntax=docker/dockerfile:1
#
# homelab-probe: the command line and the web server in one image.
#
#   docker build -t homelab-probe .
#   docker run --rm homelab-probe --demo diagnose                          # the command line (entry point: `hlp`)
#   docker run -p 127.0.0.1:8787:8787 -v hlp-data:/data homelab-probe      # the web server (the default command)
#
# Only the files named in the COPY lines below go into the image, and .dockerignore keeps everything else (.env,
# snapshots/, accounts, .git) out of the build context as well. The API key is never part of an image: it reaches the
# container at run time, through the guided setup, a mounted .env or the environment. See docs/docker.md.

# --- Stage 1: the web app -----------------------------------------------------------------------------------------
# PLACEHOLDER. There is no web app to build yet (the React app is a later part of #188). This stage exists so that
# the structure of this file does not change when it arrives: only this stage is replaced. It must leave the finished
# bundle in /out; until then /out is an empty directory and the image has no web app. To be replaced by something like
#
#   FROM node:<version>-slim AS web
#   WORKDIR /web
#   COPY web/package.json web/package-lock.json ./
#   RUN npm ci
#   COPY web/ ./
#   RUN npm run build && mkdir /out && cp -r dist/. /out/
#
# (and `!web` in .dockerignore).
FROM scratch AS web
WORKDIR /out

# --- Stage 2: build the wheels ------------------------------------------------------------------------------------
FROM python:3.13-slim AS build

WORKDIR /src
# The files `pip` needs, in the order they change: a change to the code does not invalidate the layers before it.
# docs/schemas holds the JSON Schemas that pyproject.toml ships inside the wheel.
COPY pyproject.toml README.md ./
COPY docs/schemas docs/schemas
COPY homelab_probe homelab_probe
# The bundle of stage 1, copied into the package only when stage 1 produced one (the placeholder produces nothing).
COPY --from=web /out /web-out
RUN if [ -n "$(ls -A /web-out)" ]; then mkdir -p homelab_probe/web && cp -r /web-out/. homelab_probe/web/; fi
# One wheel for the tool (with the web extra) and one for each dependency. The cache mount keeps the downloads out
# of the image and makes a rebuild fast.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels ".[web]"

# --- Stage 3: the image -------------------------------------------------------------------------------------------
FROM python:3.13-slim

LABEL org.opencontainers.image.title="homelab-probe" \
      org.opencontainers.image.description="Query, troubleshoot and inventory a UniFi Network controller: the hlp command line and its web server" \
      org.opencontainers.image.source="https://github.com/jeffholst/homelab-probe" \
      org.opencontainers.image.licenses="Apache-2.0"

# Installed from the wheels of stage 2 only (no package index is contacted); the wheels are not kept in the image.
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links /wheels "homelab-probe[web]"

# An ordinary user. /data is the working directory and the one place that is written: .env, hlp.toml, users.json,
# audit.log, snapshots/ and certs/. A named volume mounted there takes this owner; a directory from the host must be
# made writable for uid 10001 (docs/docker.md).
RUN useradd --system --uid 10001 --home-dir /data --no-create-home --shell /usr/sbin/nologin hlp \
    && mkdir /data && chown hlp /data && chmod 700 /data
USER hlp
WORKDIR /data
VOLUME /data

# Logs are one JSON object per line on stderr (`docker logs`, the logging driver). No .pyc files: nothing may be
# written but /data and /tmp, so the root file system can be read-only.
ENV LOG_FORMAT=json \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787
# /healthz answers without a login and reads nothing from the controller. The check runs inside the container, so the
# Host is a loopback name, which the server always accepts. It follows the default port: with --port, change it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"]

# A wildcard bind needs an allowed Host. `localhost` is accepted anyway, so this default answers the browser of the
# machine that publishes the port (`-p 127.0.0.1:8787:8787`) and nothing else: a name used from another machine is
# refused until it is added with another --allowed-host (the compose example and docs/docker.md show where).
ENTRYPOINT ["hlp"]
CMD ["serve", "--host", "0.0.0.0", "--allowed-host", "localhost", "--scheduler"]
