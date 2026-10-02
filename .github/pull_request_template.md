Fixes #

## What and why

## Self-review (see "Before opening a PR" in CLAUDE.md)

- [ ] Every acceptance criterion of the issue is listed here with the test that proves it:
- [ ] Each "never / only / every / reads less" claim was attacked by a test (poisoned value, request count, all output paths)
- [ ] Sibling code paths (other commands, collectors, text/JSON) were checked or the omission is explained
- [ ] Edge cases: missing/empty/duplicate values, other spellings (MAC, IP), ids not display names, wrong types
- [ ] Partial failure: what each degraded read still shows is correct and the warning says so
- [ ] Conventions: `CONFIG_VARIABLES`, `test_output_safety`, `_redact`, `normalize_mac`, no stale comments or docs
- [ ] New tests were seen to fail with the feature broken
- [ ] README and CLAUDE.md updated; shell snippets and examples run as written
- [ ] `uv run pytest`, `uv run ruff check .`, and `uv run mypy` pass; coverage still 100%; no real data in code, tests, docs or this text

## Notes for the reviewer (decisions, unverified assumptions, things deliberately left out)
