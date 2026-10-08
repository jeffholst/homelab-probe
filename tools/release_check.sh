#!/usr/bin/env bash
# Preflight for a release tag (a maintainer tool; the owner cuts releases, see MAINTAINING.md "Tag and Publish"):
#
#   tools/release_check.sh X.Y.Z          # read-only checks, prints the release notes
#   tools/release_check.sh X.Y.Z --tag    # the same checks, then creates the LOCAL annotated tag vX.Y.Z
#
# Through make: `make release-check VERSION=X.Y.Z` and `make tag VERSION=X.Y.Z`. It never pushes: creating a tag
# locally publishes nothing, and pushing it (which starts the release workflow) stays a separate command you type.
# It needs git, and `gh` to read the CI result; it stops at the first check that fails, with the reason.
set -euo pipefail

cd "$(dirname "$0")/.."
[ "$#" -le 2 ] || { echo "FAIL: too many arguments (usage: release_check.sh X.Y.Z [--tag])" >&2; exit 1; }
VERSION="${1:-}"
MODE="${2:-}"
REMOTE=origin
PYTHON="${PYTHON:-uv run python}"   # a test replaces it

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "give the version as X.Y.Z (for example: make release-check VERSION=0.5.0)"
[ -z "$MODE" ] || [ "$MODE" = "--tag" ] || fail "unknown option $MODE (the only one is --tag)"
TAG="v$VERSION"

[ -z "$(git status --porcelain)" ] || fail "the working tree is not clean (git status --short)"
pass "working tree is clean"

[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || fail "not on main (git switch main)"
git fetch --quiet "$REMOTE" main || fail "could not fetch $REMOTE/main"
HEAD_SHA="$(git rev-parse HEAD)"
[ "$HEAD_SHA" = "$(git rev-parse "$REMOTE/main")" ] || fail "main is not $REMOTE/main (git pull --ff-only $REMOTE main)"
pass "on main, equal to $REMOTE/main"

[ -z "$(git tag --list "$TAG")" ] || fail "the tag $TAG already exists locally"
# --exit-code: 0 = the tag is there, 2 = no such ref; anything else is a failed query, never "absent"
status=0
git ls-remote --exit-code --tags "$REMOTE" "refs/tags/$TAG" >/dev/null 2>&1 || status=$?
case "$status" in
    0) fail "the tag $TAG already exists on $REMOTE" ;;
    2) ;;
    *) fail "could not check the tags on $REMOTE (git ls-remote exited with $status)" ;;
esac
pass "the tag $TAG does not exist yet"

NOTES="$($PYTHON tools/release_notes.py "$TAG")" || fail "the version or the CHANGELOG.md entry does not match $TAG (see the message above)"
pass "__version__ and a dated CHANGELOG.md entry match $TAG"

command -v gh >/dev/null 2>&1 || fail "gh is needed to read the CI result"
CI="$(gh run list --branch main --workflow ci.yml --limit 1 --json headSha,status,conclusion \
        --jq '.[0] | [.headSha, .status, .conclusion] | @tsv')" || fail "could not read the CI runs with gh"
[ "$CI" = "$(printf '%s\tcompleted\tsuccess' "$HEAD_SHA")" ] || fail "the latest CI run on main is not a success for this commit (sha, status, conclusion: $CI)"
pass "CI passed on this commit"

echo
echo "Commit: $(git log -1 --oneline)"
echo "Release notes for $TAG:"
echo "$NOTES"
echo

if [ "$MODE" = "--tag" ]; then
    git tag -a "$TAG" -m "Release $TAG"
    git show --no-patch "$TAG"
    echo
    echo "Created the local tag $TAG. Nothing is published until you push it:"
    echo "  git push $REMOTE $TAG"
else
    echo "All checks passed. To create the local tag: make tag VERSION=$VERSION"
fi
