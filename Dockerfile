# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    TELEMETRY_ENABLED=false

# Install system dependencies & Node.js (Required for Reflex prod builds)
RUN apt-get update && apt-get install -y curl unzip && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv (Astral)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer (cached). The lockfile is copied when present so builds are
# reproducible; `uv sync` resolves from pyproject.toml if no lock exists yet.
COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --no-install-project --no-dev

# Application code
# NOTE: app/ includes app/backend/tools/fixtures/ (bundled fixture JSON).
# The fixtures ship inside the app package so the image does not depend on
# the legacy top-level mocks/fixtures/ directory at runtime.
COPY app ./app
COPY assets ./assets
COPY rxconfig.py ./

# Use the committed lockfile for byte-reproducible installs when present
# (`make setup` runs `uv lock`; commit uv.lock once and CI + images resolve
# identically). Fall back to a plain resolve so a lockless first clone
# (e.g. fresh zip -> Codespace) still builds instead of erroring on --frozen.
RUN if [ -f uv.lock ]; then uv sync --no-dev --frozen; else uv sync --no-dev; fi

EXPOSE 3000 8000
# REFLEX_ENV (dev|prod) selects the run mode; defaults to prod.
# docker-compose / `make run-dev` set this to switch modes without rebuilding.
ENV REFLEX_ENV=prod
ENTRYPOINT ["sh", "-c", "exec reflex run --env \"${REFLEX_ENV:-prod}\" --backend-host 0.0.0.0"]
# Alternative: serve only the FastAPI ASGI app (health/readiness + API) via
# uvicorn, e.g. for a backend-only deployment:
# CMD ["uvicorn", "app.app:asgi_app", "--host", "0.0.0.0", "--port", "8000"]
