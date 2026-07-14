#!/bin/bash

set -e 

MODE=${1:-dev}
ENV_FILE=".env"
GITIGNORE=".gitignore"
DOCKERIGNORE=".dockerignore"

# echo "🚀 Bootstrapping [$MODE] environment..."

# ---------------------------------------------------------------------------
# Resolve API_URL (backend/event endpoint baked into the Reflex frontend).
#
# Single source of truth: delegate to app/shared/platform.py so local /
# Lightning.ai / Hugging Face detection lives in ONE place (the same module
# rxconfig.py uses at build time). A pure-bash fallback covers the rare case
# where python3 is unavailable in this shell.
#
# This stack runs behind Nginx (docker-compose.yml + nginx.conf): only the
# Nginx port 3000 is published, and Nginx proxies /_event to the backend
# (app:8000). The browser reaches everything via port 3000.
# ---------------------------------------------------------------------------
PROXY_PORT=3000
LITNG_SUFFIX="cloudspaces.litng.ai"

if command -v python3 >/dev/null 2>&1; then
    API_URL="$(python3 -c 'from app.shared.platform import resolve_api_url; print(resolve_api_url())' 2>/dev/null || true)"
fi

if [ -z "${API_URL:-}" ]; then
    # Bash fallback (mirrors platform.resolve_api_url precedence).
    if [ -n "${API_URL_OVERRIDE:-}" ]; then
        API_URL="${API_URL_OVERRIDE}"
    elif [ -n "${SPACE_HOST:-}" ]; then
        HF_HOST="${SPACE_HOST#https://}"; HF_HOST="${HF_HOST#http://}"; HF_HOST="${HF_HOST%/}"
        API_URL="https://${HF_HOST}"
    elif [ -n "${LIGHTNING_CLOUD_SPACE_HOST:-}" ]; then
        API_URL="https://${PROXY_PORT}-${LIGHTNING_CLOUD_SPACE_HOST}"
    elif [ -n "${LIGHTNING_CLOUD_SPACE_ID:-}" ]; then
        API_URL="https://${PROXY_PORT}-${LIGHTNING_CLOUD_SPACE_ID}.${LITNG_SUFFIX}"
    else
        API_URL="http://localhost:${PROXY_PORT}"
    fi
fi
echo "🌐 Resolved API_URL=${API_URL}"
# Export so processes launched from this shell inherit it (compose also reads
# the persisted .env value below).
export API_URL

# DYNAMICALLY GENERATE .env
if [ ! -f "$ENV_FILE" ]; then
    echo "📄 Generating base Saisei template..."
    cat <<EOF > $ENV_FILE
SAISEI_POSTGRES_DSN=postgresql://saisei:${POSTGRES_PASSWORD:-saisei}@postgres:5432/saisei
SAISEI_REDIS_URL=redis://redis:6379/0
SAISEI_LOG_LEVEL=INFO
SAISEI_USE_MOCKS=true
# LLM provider (OpenAI-compatible Chat Completions API). Empty = offline.
# OpenRouter (prototyping): set BASE_URL=https://openrouter.ai/api/v1,
#   API_KEY from https://openrouter.ai/keys, MODEL=<slug> e.g.
#   qwen/qwen3-30b-a3b (verify the slug on openrouter.ai/models first).
# See .env.example for OpenAI / self-hosted options and caveats.
SAISEI_LLM_API_KEY=
SAISEI_LLM_MODEL=
SAISEI_LLM_BASE_URL=https://api.openai.com/v1
# Agent memory (advisory RAG). Empty = mock provider / no precedents (offline).
#   Long-term memory  -> pgvector (durable precedent corpus, in Postgres)
#   Short-term memory -> RediSearch (fast ephemeral recall cache, in Redis)
# pgvector reuses the same Postgres instance (the pgvector/pgvector image).
# RediSearch is opt-in: point it at a Redis with the RediSearch module loaded.
SAISEI_PGVECTOR_DSN=postgresql://saisei:${POSTGRES_PASSWORD:-saisei}@postgres:5432/saisei
SAISEI_REDISEARCH_URL=
SAISEI_EMBEDDING_MODEL=
EOF
fi

# Keep API_URL in sync on every run (it can change between Lightning sessions),
# even when .env already exists. Strip any existing line, then append.
if [ -f "$ENV_FILE" ] && grep -q '^API_URL=' "$ENV_FILE" 2>/dev/null; then
    grep -v '^API_URL=' "$ENV_FILE" > "${ENV_FILE}.tmp"
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
fi
echo "API_URL=${API_URL}" >> "$ENV_FILE"

# AUTOGENERATE .gitignore
echo "📝 Ensuring .gitignore is present..."
cat <<EOF > $GITIGNORE
# Saisei Git Ignore Rules
.env
.venv/
__pycache__/
*.pyc
.reflex/
node_modules/
.pytest_cache/
.ruff_cache/
.mypy_cache/
*.zip
*.tar.gz
.DS_Store
EOF


# AUTOGENERATE .dockerignore 
echo "🐳 Ensuring .dockerignore is present..."
cat <<EOF > $DOCKERIGNORE
# Saisei Docker Ignore Rules
.git
.env
.venv
node_modules
.reflex
*.zip
Makefile
scripts/
tests/
EOF

echo "✅ Architecture fully scaffolded! Ready to run."
