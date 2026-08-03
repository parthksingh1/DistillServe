# DistillServe — one entry point for every routine task.
#
# Targets are thin wrappers over uv/pnpm so that what CI runs and what a
# developer runs locally are literally the same command.

.DEFAULT_GOAL := help
SHELL := /bin/sh

UV ?= uv
PNPM ?= pnpm
GATEWAY_HOST ?= 0.0.0.0
GATEWAY_PORT ?= 8000
DEPLOY_CHECK_URL ?= http://localhost:$(GATEWAY_PORT)

.PHONY: help install install-py install-js dev dev-gateway dev-web \
        test test-py test-web lint lint-py lint-js format typecheck \
        schemas reference-refresh deploy-check compose-up compose-down clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Setup -----------------------------------------------------------------

install: install-py install-js ## Install all workspace dependencies
	$(UV) run pre-commit install

install-py: ## Sync the Python workspace (all members, dev group included)
	$(UV) sync --all-packages

install-js: ## Install the JS workspace
	$(PNPM) install --frozen-lockfile

# --- Develop ---------------------------------------------------------------

dev: ## Run gateway + web console together (Ctrl-C stops both)
	$(PNPM) run dev

dev-gateway: ## Run only the FastAPI gateway with reload
	$(UV) run uvicorn distillserve_gateway.main:app \
	  --host $(GATEWAY_HOST) --port $(GATEWAY_PORT) --reload

dev-web: ## Run only the Vite dev server
	$(PNPM) --filter @distillserve/web dev

# --- Verify ----------------------------------------------------------------

test: test-py test-web ## Run all tests

test-py: ## pytest across the Python workspace
	$(UV) run pytest --cov --cov-report=term-missing

test-web: ## vitest in the web workspace
	$(PNPM) --filter @distillserve/web test

lint: lint-py lint-js ## Lint everything

lint-py: ## ruff + black --check
	$(UV) run ruff check .
	$(UV) run black --check .

lint-js: ## ESLint + Prettier --check
	$(PNPM) run lint
	$(PNPM) run format:check

format: ## Auto-format Python and JS in place
	$(UV) run ruff check --fix .
	$(UV) run black .
	$(PNPM) run format

typecheck: ## mypy --strict + tsc --noEmit
	$(UV) run mypy .
	$(PNPM) run typecheck

# --- Data & contracts ------------------------------------------------------

schemas: ## Regenerate TypeScript types from the Pydantic schemas
	$(UV) run python scripts/generate_ts_types.py

reference-refresh: ## Rebuild data/reference/ from its source configs
	$(UV) run python scripts/refresh_reference_dataset.py

# --- Local infra -----------------------------------------------------------

compose-up: ## Start Redis + gateway + web via docker compose
	docker compose -f infra/docker/docker-compose.yml up --build

compose-down: ## Tear down the local compose stack
	docker compose -f infra/docker/docker-compose.yml down -v

# --- Deploy ----------------------------------------------------------------

deploy-check: ## Probe a deployed gateway and print a green/red summary
	$(UV) run python scripts/deploy_check.py --base-url $(DEPLOY_CHECK_URL)

clean: ## Remove build/test caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
