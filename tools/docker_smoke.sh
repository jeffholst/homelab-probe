#!/usr/bin/env bash
# Smoke checks of the Docker image (issue #246), run by CI after the build and by hand:
#
#   docker build -t homelab-probe:ci .
#   tools/docker_smoke.sh homelab-probe:ci
#
# It needs docker, curl and python3. It starts containers on 127.0.0.1 only, never pushes or logs in anywhere, never
# contacts a UniFi controller (the CLI and the server run with --demo, or with no configuration at all), and removes
# everything it created when it ends. Every check prints PASS or stops the script with FAIL and the reason.
set -euo pipefail

IMAGE="${1:-homelab-probe:ci}"
PORT="${SMOKE_PORT:-18787}"
SUFFIX="$$"
VOLUME="hlp-smoke-data-$SUFFIX"
CONTAINERS=()

cleanup() {
    for name in "${CONTAINERS[@]:-}"; do
        [ -n "$name" ] && docker rm -f "$name" >/dev/null 2>&1 || true
    done
    docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# The container is read-only except /data and /tmp, with no capabilities: how compose.yaml runs it.
hardened=(--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges)

wait_healthy() {            # container name; the health check of the image, polled quickly
    local name="$1" status=""
    for _ in $(seq 1 60); do
        status="$(docker inspect --format '{{.State.Health.Status}}' "$name")"
        [ "$status" = healthy ] && return 0
        [ "$(docker inspect --format '{{.State.Running}}' "$name")" = true ] || break
        sleep 1
    done
    docker logs "$name" >&2 || true
    fail "$name did not become healthy (last status: $status)"
}

start() {                   # container name, then the arguments of docker run (the image and its command last)
    local name="$1"; shift
    CONTAINERS+=("$name")
    docker run -d --name "$name" "${hardened[@]}" --health-interval 2s --health-start-period 1s \
        -p "127.0.0.1:$PORT:8787" "$@" >/dev/null
}

# 1. The command line: --version is the package's version.
expected="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' homelab_probe/__init__.py)"
[ -n "$expected" ] || fail "cannot read __version__"
actual="$(docker run --rm "${hardened[@]}" "$IMAGE" --version)"
[ "$actual" = "$expected" ] || [ "$actual" = "hlp $expected" ] || fail "--version said '$actual', expected $expected"
pass "--version is $actual"

# 2. Not root, and the root file system is read-only.
uid="$(docker run --rm --entrypoint id "$IMAGE" -u)"
[ "$uid" != 0 ] || fail "the image runs as root"
configured_user="$(docker image inspect --format '{{.Config.User}}' "$IMAGE")"
[ -n "$configured_user" ] && [ "$configured_user" != root ] && [ "$configured_user" != 0 ] \
    || fail "the image does not set a non-root user ($configured_user)"
pass "runs as uid $uid"

# 3. One CLI command, in demo mode (no controller, no configuration), on a read-only file system.
output="$(docker run --rm "${hardened[@]}" "$IMAGE" --demo query devices 2>/dev/null)"
echo "$output" | grep -q "Gateway" || fail "--demo query devices did not list the demo gateway"
pass "--demo query devices lists the synthetic devices"

# 4. No configuration at all: the server starts in the setup mode, healthy, with the token in its log.
docker volume create "$VOLUME" >/dev/null
start "hlp-smoke-setup-$SUFFIX" -v "$VOLUME:/data" "$IMAGE"
name="hlp-smoke-setup-$SUFFIX"
wait_healthy "$name"
pass "healthy (HEALTHCHECK on /healthz)"
meta="$(curl -fsS "http://127.0.0.1:$PORT/api/v1/meta")"
echo "$meta" | python3 -c 'import json, sys; m = json.load(sys.stdin); assert m["needs_setup"] is True and m["setup_mode"] == "setup", m' \
    || fail "/api/v1/meta does not say needs_setup: $meta"
docker logs "$name" 2>&1 | grep -q "finish the setup with this token: " || fail "the log has no setup token line"
pass "no configuration reaches the setup mode, the log shows the token"
# A name the server was not told about is refused (the default allows only localhost).
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: not-allowed.example' "http://127.0.0.1:$PORT/healthz")"
[ "$code" = 400 ] || fail "an unknown Host answered $code, expected 400"
pass "an unknown Host header is refused"
# Only /data and /tmp are writable, and /data is owned by the user of the process.
docker exec "$name" sh -c 'touch /data/.probe && rm /data/.probe && touch /tmp/.probe && rm /tmp/.probe' \
    || fail "/data and /tmp must be writable"
if docker exec "$name" sh -c 'touch /usr/.probe' 2>/dev/null; then fail "the root file system is writable"; fi
[ "$(docker exec "$name" stat -c %u /data)" = "$uid" ] || fail "/data is not owned by uid $uid"
pass "read-only root file system, writable /data and /tmp owned by uid $uid"
docker rm -f "$name" >/dev/null

# 5. The demo server through the published port: log in and read a report.
name="hlp-smoke-demo-$SUFFIX"
start "$name" "$IMAGE" --demo serve --host 0.0.0.0 --allowed-host localhost
wait_healthy "$name"
password="$(docker logs "$name" 2>&1 | sed -n 's/^Demo login: user demo, password \([^ ]*\) .*/\1/p')"
[ -n "$password" ] || fail "no demo login in the log"
jar="$(mktemp)"
curl -fsS -c "$jar" -H "Origin: http://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
    -d "{\"username\": \"demo\", \"password\": \"$password\"}" "http://127.0.0.1:$PORT/api/v1/auth/login" >/dev/null \
    || fail "the demo login failed"
report="$(curl -fsS -b "$jar" "http://127.0.0.1:$PORT/api/v1/unifi/sites/default/wan")"
rm -f "$jar"
echo "$report" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert "version" in d and "generated_at" in d, d' \
    || fail "the wan report is not a document"
pass "demo server: login and /api/v1/unifi/sites/default/wan through the published port"

# 6. The image holds no secret or state: none of the git-ignored files, and no source tree.
found="$(docker run --rm --entrypoint sh "$IMAGE" -c \
    'find / -xdev \( -name .env -o -name hlp.toml -o -name users.json -o -name audit.log -o -name snapshots \) 2>/dev/null || true')"
[ -z "$found" ] || fail "files that must not be in the image: $found"
pass "no .env, hlp.toml, users.json, audit.log or snapshots/ in the image"

echo "All Docker smoke checks passed."
