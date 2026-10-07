# Maintainer Guide

This is the reference for running and releasing Homelab Probe. Run commands from the project folder.
For submitting changes, see [CONTRIBUTING.md](CONTRIBUTING.md); code conventions are in [CLAUDE.md](CLAUDE.md).
Update this guide in the same PR whenever a maintenance or release procedure changes.

## Project Basics

- `main` holds reviewed, merged work, including changes that may not be released yet.
- A branch holds work in progress; a pull request (PR) asks for review before that work is merged.
- A commit records a set of changes. A release tag bookmarks one exact commit, even as `main` moves forward.
- [CHANGELOG.md](CHANGELOG.md) records user-visible changes under `Unreleased` until a release is prepared.
  Keep temporary tasks in [issues](https://github.com/jeffholst/homelab-probe/issues).
- This repository is a fork. Explicitly target `jeffholst/homelab-probe` when creating a PR with GitHub CLI.

## Issue Labels

Labels group the issues so open work can be found and ordered. The issue forms add only `bug` or `enhancement`,
so add the others yourself when you create or triage an issue. Every open issue should have an area label.
Use the labels GitHub provides (`bug`, `enhancement`, `documentation`, `accessibility`, `question`) as they are
described there; the labels below are this project's own.

| Label | Use it for |
| ----- | ---------- |
| `area: web` | The React app and browser UI (`web/`) |
| `area: api` | The server and HTTP API (`hlp serve`, `homelab_probe/server/`) |
| `area: cli` | The command line and the code behind it |
| `area: docker` | The container image, Compose file and release workflow |
| `tracker` | An issue that tracks child issues and closes when they are done, such as #160 and #187 |
| `launch` | Work required for the first usable public web release (see #187) |
| `terminal` | The web terminal feature group: #269, #270 and their sub-issues |
| `security` | Work that needs security-focused design and review: secrets, authorization, untrusted input, output safety |
| `blocked` | Waiting on another issue. Say which one in the issue description, and remove the label when it merges |
| `needs-approval` | Needs the owner's approval before work continues, for example a probe of the real controller |
| `deferred` | Postponed on purpose until a stated condition is met, such as a second site existing |

An issue can have several labels: an area, plus any of `tracker`, `launch`, `terminal`, `security` and one status
(`blocked`, `needs-approval` or `deferred`). `question` on an idea means the design still needs a decision.

Useful searches (add `--repo jeffholst/homelab-probe` outside the project folder):

```bash
# What can be started now in the web app: open, area web, not blocked
gh issue list --label "area: web" --search "-label:blocked"
# What the first public web release still needs
gh issue list --label launch
# Everything waiting on a decision from the owner
gh issue list --label needs-approval
```

Change a label's wording, color or purpose here and on GitHub together. To recreate one (for example in a copy of
the repository), run `gh label create "area: web" --color 1d76db --description "The React app and browser UI"`;
add `--force` to update an existing label.

## Version Numbers

The app version is stored in [homelab_probe/__init__.py](homelab_probe/__init__.py), as `__version__`.
The inner folder uses an underscore; the project folder uses a hyphen. Packaging reads this value through
[pyproject.toml](pyproject.toml), so do not maintain a second version number there.

Read the version without contacting a controller:

```bash
uv run hlp.py --version
```

The three numbers mean major, minor, and patch. Choose the next release based on everything under `Unreleased`:

| Change | Example | Use for |
| ------ | ------- | ------- |
| Patch | `0.2.0` to `0.2.1` | Bug fixes, dependency/security updates, small compatibility fixes, or documentation affecting released packages |
| Minor | `0.2.0` to `0.3.0` | New commands, options, settings, checks, output columns, or other capabilities |
| Major | `0.9.0` to `1.0.0` | A deliberately chosen stable-interface milestone; later major bumps mark breaking changes |

While the project is `0.x`, a minor release may change existing behavior; describe those changes in the changelog.
Major changes require the owner's approval. Ordinary feature and fix PRs leave the app version unchanged.
The `version` fields in JSON output describe data formats and are separate from the app version;
see [JSON output schemas](docs/schemas.md).

## Routine Checks

Python 3.10 or newer and uv are required. Install the locked dependencies, including development tools and the web
extra, then run the checks:

```bash
# Install exactly what uv.lock pins (it fails instead of updating the lockfile): the dev tools, plus the web and
# pretty extras, which the server, styling and type-check tests need
uv sync --locked --group dev --extra web --extra pretty
# Run the whole test suite in that environment
uv run --extra web --extra pretty python -m pytest
# Lint: style problems, unused imports and import order (add --fix to repair the simple ones)
uv run --extra web --extra pretty ruff check .
# Type-check the package; it must be clean because CI blocks on it
uv run --extra web --extra pretty python -m mypy
# Fail if pyproject.toml and uv.lock disagree (after changing a dependency, run `uv lock` and commit the result)
uv lock --check
```

Tests should pass, Ruff should report no findings, mypy should report no issues, and the lockfile check should
succeed. The default tests use synthetic data; live tests need the controller owner's approval.
CI also checks 100% line and branch coverage; some shell tests skip on a Mac when tools are unavailable.
See [development documentation](docs/development.md#development) for coverage and fixture details.

Run the synthetic network without credentials or a controller:

```bash
uv run hlp.py --demo diagnose
```

Start the local web API, then use Ctrl-C to stop it:

```bash
uv run --extra web hlp.py serve
```

It prints its local address, normally `http://127.0.0.1:8787`.
See [web documentation](docs/web.md#running-the-server-serve) for options and access restrictions.

## Prepare a Release

The owner decides when to release. Assistants can prepare a release PR; the owner creates and pushes the tag.
Choose a release when the intended features are complete and checks pass, rather than after every merged PR.

1. Review `Unreleased`, open PRs, and the latest CI result on `main`. Make sure no unfinished PR belongs to the
   release. Choose the version using the table above.
2. Start from an up-to-date `main` with a clean working tree. Create a release branch.
3. Update `__version__` in `homelab_probe/__init__.py`. In `CHANGELOG.md`, give the changes a heading such as
   `## [0.3.0] - YYYY-MM-DD`, replacing the example version and date with the chosen version and actual release
   date. Leave a fresh `## [Unreleased]` section above it. Run `uv lock` to refresh package metadata if needed,
   and include any resulting lockfile change. The OpenAPI golden file records the version too, so regenerate it:
   `UPDATE_GOLDEN=1 uv run pytest tests/test_server_routes.py`.
4. Run the routine checks and the release-notes check below. It should print the intended release notes;
   it stops with an error if the tag, package version, or dated changelog entry does not agree.
5. Commit the release preparation, open a PR against `main` in `jeffholst/homelab-probe`, and finish review.
   Merge the PR before tagging.

For example, **only if the chosen next version is 0.3.0**, start the branch with:

```bash
git status --short
git switch main
git pull --ff-only origin main
git switch -c release/0.3.0
```

The status command should show no changes before switching branches. After editing the version and changelog,
validate the release notes with the matching example tag:

```bash
uv run python tools/release_notes.py v0.3.0
```

## Tag and Publish

**Pushing a tag beginning with `v` triggers the automated release.** Creating the tag locally does not publish
anything. A tag does not update the version stored in the code.

After the release PR is merged, the owner runs these commands. **0.3.0 is an example, not an instruction to use
that version**; substitute the version prepared by the merged release PR everywhere below.

```bash
git status --short
git switch main
git pull --ff-only origin main
git show HEAD:homelab_probe/__init__.py
uv run python tools/release_notes.py v0.3.0
git log -1 --oneline
git tag --list v0.3.0
```

Confirm the working tree is clean, the version and notes match, and the current commit is the intended release
commit. The tag-list command should print nothing for a new release. Then create an annotated tag, which includes
a description, inspect it, and push that specific tag:

```bash
git tag -a v0.3.0 -m "Release v0.3.0"
git show --no-patch v0.3.0
git push origin v0.3.0
```

The [release workflow](.github/workflows/release.yml) checks the lockfile, validates the tag against the version
and dated changelog, runs tests, builds a wheel and source archive, and creates a GitHub release with the files
and changelog notes. It does not publish to PyPI.

Watch the [Release runs](https://github.com/jeffholst/homelab-probe/actions/workflows/release.yml), then confirm
the new entry, notes, and downloads on the [Releases page](https://github.com/jeffholst/homelab-probe/releases).
A merged PR alone does not publish a release.

## Inspect Release History

Fetch shared tags and list releases:

```bash
git fetch origin --tags
git tag --list 'v*' --sort=-version:refname
```

Inspect a release or compare two released versions. These are examples; both tags must exist for the comparison:

```bash
git show --no-patch v0.2.0
git diff --stat v0.2.0 v0.3.0
```

To inspect old code with a clean working tree, use `git switch --detach v0.2.0`; Git will place you at the tagged
commit rather than on a working branch. Return with `git switch main`. Returning to old code does not restore
data files or undo a published release.

## Common Recovery Steps

### Missing pip or Web Dependencies

Use `uv run --extra web hlp.py serve` from the checkout. There is no need for a separate pip executable.
If development tools are missing, run the sync command under [Routine Checks](#routine-checks).
For a standalone installation rather than a checkout, see [installation](README.md#installation).

### GitHub Rejects a Push

Check the accounts and select the repository owner's account:

```bash
gh auth status
gh auth switch --hostname github.com --user jeffholst
```

If Git still uses an old HTTPS credential, use GitHub CLI's credential for this push. Replace the example branch
with your actual branch:

```bash
git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push -u origin your-branch
```

Keep GitHub tokens out of commands, documentation, and commits.

### TLS Certificate Verification Fails

Check that `UNIFI_VERIFY_SSL` trusts the controller's signing CA and that the certificate covers the hostname
or IP address used by `UNIFI_URL`. Some UniFi self-signed certificates are not valid CA bundles even when the
PEM file matches the controller certificate. Disabling verification removes the identity check;
see [configuration troubleshooting](docs/configuration.md#troubleshooting) before changing it.

### A Release Run Fails

Open the failed Release run and read the first failing step. Version or changelog errors need a correction;
test or build errors need their cause fixed. A rerun uses the same tagged code, so a fix merged into `main`
will not change an existing tag. Rerun only when the failure was temporary and the tagged code needs no change.
For a code correction, prepare a new release version. Do not silently move or reuse a published release tag.

## Project Boundaries

The app reads the controller without changing its settings. The event-log POST is a read-only query, not a write.
Notifications go only to destinations the user explicitly configures. Keep API keys, `.env`, real network data,
and account files out of commits, PR descriptions, and examples. Use the synthetic fixture for shared output.
See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.
