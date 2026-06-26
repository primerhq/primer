# Developer task runner for primer.
#
# These targets call the SAME commands the CI workflow runs
# (.github/workflows/ci.yml), so green locally means green in CI. The repo
# standardizes on uv (see README / CONTRIBUTING.md).

# The narrowed unit sweep shared by `test` and `cov`, kept in one place so
# the two stay identical to each other and to CI.
PYTEST_IGNORES := \
	--ignore=tests/distributed \
	--ignore=tests/ui_e2e \
	--ignore=tests/e2e \
	--ignore=tests/integration \
	--ignore=tests/llm

.PHONY: help setup test lint fmt cov docs-hygiene serve docker-build

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies (uv sync --all-extras)
	uv sync --all-extras

test: ## Run the narrowed unit sweep (excludes e2e/distributed/ui_e2e/integration/llm)
	uv run pytest tests/ -q $(PYTEST_IGNORES) --tb=short

lint: ## Lint with ruff (matches the CI lint job)
	uv run ruff check .

fmt: ## Auto-fix lint findings and sort/format what ruff can fix safely
	uv run ruff check --fix .

cov: ## Run the unit sweep with coverage and enforce the 90% threshold
	uv run pytest tests/ -q $(PYTEST_IGNORES) \
		--cov=primer \
		--cov-report=term-missing:skip-covered \
		--cov-report=xml:coverage.xml \
		--cov-fail-under=90 \
		--tb=short

docs-hygiene: ## Run the docs hygiene suite (em-dash ban, links, frontmatter)
	uv run pytest tests/docs/ -q --tb=short

serve: ## Start the API server (with embedded worker)
	uv run primer api

docker-build: ## Build the primer container image
	docker compose build primer
