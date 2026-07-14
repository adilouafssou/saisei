.DEFAULT_GOAL := help
.PHONY: help bootstrap setup lock run-dev run-prod stop verify lint fix type test clean upgrade seed-memory ingest-corpus calibrate export-trajectories migrate migrate-create

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

migrate: ## 🗄️  Apply DB migrations (Alembic upgrade head) using SAISEI_POSTGRES_DSN
	uv run alembic upgrade head

migrate-create: ## ➕ Create a new migration revision (pass message via m="...")
	uv run alembic revision -m "$(m)"

seed-memory: ## 🧠 Seed the pgvector long-term agent-memory store with the bundled corpus
	uv run python -m app.backend.tools.retrieval_ingest

ingest-corpus: ## 📚 Ingest a directory of precedent docs (.md/.txt) into long-term memory (DIR=/path)
	uv run python -m app.backend.tools.precedent_loader $(DIR)

run-dev: ## 🛠️ Start the stack dynamically in DEV mode
	@bash scripts/setup_env.sh dev
	REFLEX_ENV=dev docker compose up

run-prod: ## 🚀 Start the stack dynamically in PROD mode
	@bash scripts/setup_env.sh prod
	REFLEX_ENV=prod docker compose up -d

stop: ## Stop running containers (keeps data/volumes)
	docker compose stop

verify: lint type test ## Run all quality gates (ruff + mypy + pytest)

calibrate: ## 📊 Print the advisory reconciliation-threshold report (pass flags via ARGS)
	uv run python -m app.backend.analysis.calibrate_cli $(ARGS)

export-trajectories: ## 📦 Run the PII-safe in-VPC trajectory export (pass thread ids/flags via ARGS)
	uv run python -m app.backend.trajectory.export_cli $(ARGS)

lint: ## Run ruff (check only — does not modify files)
	uv run ruff check .
	uv run ruff format --check .

fix: ## 🔧 Auto-fix lint + format issues in place (run before committing)
	uv run ruff check . --fix
	uv run ruff format .

type: ## Run mypy (strict)
	uv run mypy app tests

test: ## Run pytest
	uv run pytest

clean: ## Tear down containers and prune volumes (Keeps your .env secrets safe!)
	docker compose down -v --remove-orphans
	docker system prune -f
