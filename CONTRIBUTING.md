# Contributing

Thanks for helping. Issues and pull requests are welcome; work is tracked in [GitHub issues](https://github.com/jeffholst/unifi-sentinel/issues). For a security problem, do not open an issue: follow [SECURITY.md](SECURITY.md).

## Before you start

- Look for an existing issue, or open one (a [bug report or feature request](https://github.com/jeffholst/unifi-sentinel/issues/new/choose)) and say you want to work on it. One issue per pull request.
- A change that needs an endpoint nobody has probed yet starts with a read-only look at what that endpoint returns on a real controller. Say so in the issue instead of guessing field names; almost every endpoint this tool uses is undocumented.
- The tool is **read-only**. Every request is a GET, with one exception: the event log can only be queried with a POST (see [the one POST](docs/network.md#the-one-post-and-why-it-is-safe)). A change that writes to the controller, or sends data anywhere except a notification destination the user configured, will not be accepted.

## Setting up and checking your work

You need Python 3.10 or newer and [uv](https://docs.astral.sh/uv/). Nothing is installed by hand; `uv run` resolves the dependencies from `pyproject.toml` and `uv.lock`.

```bash
git clone https://github.com/jeffholst/unifi-sentinel
cd unifi-sentinel
uv run pytest               # the tests never contact a controller
uv run ruff check .         # add --fix for import order and unused imports
uv run mypy                 # clean, and blocking in CI
uv lock --check             # after changing a dependency, run `uv lock` and commit uv.lock
```

CI runs all of these on every pull request, with the tests on Python 3.10 to 3.13. Line and branch coverage is 100% and CI checks it (`uv run coverage run -m pytest`, then `uv run coverage report`), so a new branch needs a test. Write a test, then break the code it guards and watch the test fail.

## What a good pull request has

- **Tests.** The default suite uses the synthetic fixture (`tests/fixtures/controller.json`) served by a fake controller and never contacts a real one. The opt-in `uv run pytest -m live` contract tests read a configured real controller, so run them only with the controller owner's approval. Extend the fixture instead of pasting real data.
- **No real data anywhere.** Not in code, tests, docs, commit messages or the pull request text: no real MAC or IP addresses, host names, SSIDs, site IDs, names of your devices or API keys. Example output in the docs comes from the synthetic fixture.
- **Documentation in the same pull request.** The README is a short quickstart (it must stay under 250 lines) and the detail is in [docs/](docs/): a Commands row, a usage example and a section for anything non-obvious. A user-visible change gets a line in the [changelog](CHANGELOG.md). When you change output on purpose, regenerate the golden files and the documented samples (`UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`, then `UPDATE_README_SAMPLES=1 uv run pytest tests/test_docs_drift.py`) and read both diffs.
- **The conventions of the code.** Names that come from the network are untrusted and go through the output-safety helpers; finding codes are never renamed or reused; MACs are compared through `normalize_mac`; secrets never reach a message or a log. They are listed in [CLAUDE.md](CLAUDE.md), which is also the briefing for AI assistants working on this repository, and the layout of the code is in [docs/development.md](docs/development.md#development).
- **A self-review.** The [pull request template](.github/pull_request_template.md) has a checklist taken from the defects reviews keep finding. Read your own diff against it before you ask for a review.

## How a pull request is handled

Work happens on a branch, never on `main`, and the pull request says `Fixes #N`. A pull request normally gets an automated review (GitHub Copilot) first; fix what it finds on the same branch. The maintainer merges.

## License

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), like the rest of the project. UniFi Sentinel is a fork of [ericfitz/unifi-clients-export](https://github.com/ericfitz/unifi-clients-export), and that credit stays in the README.
