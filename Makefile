# Frequent maintainer tasks. `make` lists them. Run the same checks as CI (see MAINTAINING.md, "Routine Checks").

UV_RUN := uv run --extra web --extra pretty

.DEFAULT_GOAL := help
.PHONY: help install test lint fix types lock coverage check web-install web-check web-e2e ci golden demo release-check tag clean clean-all

help: ## List the tasks
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

install: ## Install the locked Python dependencies (dev tools, web and pretty extras)
	uv sync --locked --group dev --extra web --extra pretty

test: ## Run the whole test suite
	$(UV_RUN) python -m pytest -q

lint: ## Ruff: style problems, unused imports, import order
	$(UV_RUN) ruff check .

fix: ## Ruff, repairing the simple problems
	$(UV_RUN) ruff check . --fix

types: ## mypy (CI blocks on it)
	$(UV_RUN) python -m mypy

lock: ## Fail if pyproject.toml and uv.lock disagree
	uv lock --check

coverage: ## Tests with line and branch coverage; fails below 100%
	$(UV_RUN) coverage run -m pytest
	$(UV_RUN) coverage report

check: lock lint types test ## What the CI Python jobs run

web-install: ## Install the web app packages exactly as locked
	npm --prefix web ci

web-check: ## Web: generated types, lint, strict TypeScript, unit tests, build
	npm --prefix web run check

web-e2e: ## Web: Playwright browser tests (first time: npx playwright install chromium)
	npm --prefix web run e2e

ci: check coverage web-check ## Everything CI runs except the browser tests and the Docker job

golden: ## Rewrite the golden files and README samples, then review the git diff
	UPDATE_GOLDEN=1 $(UV_RUN) python -m pytest -q tests/test_golden.py
	UPDATE_README_SAMPLES=1 $(UV_RUN) python -m pytest -q tests/test_docs_drift.py
	@echo "Review: git diff tests/golden README.md docs"

demo: ## Serve the synthetic network (no controller, no .env); prints the demo login
	uv run --extra web hlp.py --demo serve

release-check: ## Read-only preflight for a release tag: make release-check VERSION=X.Y.Z
	tools/release_check.sh "$(VERSION)"

tag: ## Preflight, then create only the LOCAL annotated tag vX.Y.Z (you push it): make tag VERSION=X.Y.Z
	tools/release_check.sh "$(VERSION)" --tag

# Only disposable output. The git-ignored data here (.env, hlp.toml, users.json*, audit.log*, snapshots/) is real:
# never use `git clean` or a wildcard that could match it.
clean: ## Remove caches and build output (keeps .venv, node_modules and all your data)
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage build homelab_probe.egg-info web/dist web/test-results web/playwright-report
	find . -name __pycache__ -type d -not -path ./.venv/\* -not -path ./web/node_modules/\* -prune -exec rm -rf {} +

clean-all: clean ## Also remove .venv and web/node_modules (run install and web-install again)
	rm -rf .venv web/node_modules
