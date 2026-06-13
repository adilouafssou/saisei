.DEFAULT_GOAL := help
.PHONY: help bootstrap setup lock run-dev run-prod stop verify lint type test clean upgrade seed-memory

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install uv if missing (one-time)
	@command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh

lock: bootstrap ## Generate/refresh uv.lock (commit it for reproducible builds)
	uv lock

setup: bootstrap lock ## Install deps, auto-generate .env, build containers, seed DB
	@echo "⚙️  Configuring environment variables..."
	@bash scripts/setup_env.sh dev
	uv sync --extra dev
	docker compose build
	docker compose up -d postgres redis
	@echo "✅ Environment ready. Run 'make run-dev' or 'make run-prod' to start the stack."

upgrade: bootstrap ## 🚀 Upgrade Reflex to latest version and force a clean image rebuild
	uv add reflex@latest
	uv sync --extra dev
	docker compose build --no-cache app
	@echo "✅ Reflex and Node.js environments are fully up to date!"

seed-memory: ## 🧠 Seed the pgvector long-term agent-memory store with the bundled corpus
	uv run python -m app.backend.tools.retrieval_ingest

run-dev: ## 🛠️ Start the stack dynamically in DEV mode
	@bash scripts/setup_env.sh dev
	REFLEX_ENV=dev docker compose up

run-prod: ## 🚀 Start the stack dynamically in PROD mode
	@bash scripts/setup_env.sh prod
	REFLEX_ENV=prod docker compose up -d

stop: ## Stop running containers (keeps data/volumes)
	docker compose stop

verify: lint type test ## Run all quality gates (ruff + mypy + pytest)

lint: ## Run ruff
	uv run ruff check .
	uv run ruff format --check .

type: ## Run mypy (strict)
	uv run mypy app tests

test: ## Run pytest
	uv run pytest

clean: ## Tear down containers and prune volumes (Keeps your .env secrets safe!)
	docker compose down -v --remove-orphans
	docker system prune -f
