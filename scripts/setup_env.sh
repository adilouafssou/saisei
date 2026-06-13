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
# This stack runs behind Nginx (docker-compose.yml + nginx.conf): only the
# Nginx port 3000 is published, and Nginx proxies /_event to the internal
# backend (app:8000). The browser reaches everything via port 3000, so
# api_url must point at the Nginx address (3000), NOT the internal 8000.
#
# Lightning.ai exposes a forwarded port at:
#   https://<port>-<space-id>.cloudspaces.litng.ai
# We resolve the host in this order:
#   1. LIGHTNING_CLOUD_SPACE_HOST  (full host, if provided)
#   2. <LIGHTNING_CLOUD_SPACE_ID>.cloudspaces.litng.ai  (constructed from the ID)
#   3. localhost  (local development fallback)
# ---------------------------------------------------------------------------
PROXY_PORT=3000
LITNG_SUFFIX="cloudspaces.litng.ai"
if [ -n "${LIGHTNING_CLOUD_SPACE_HOST:-}" ]; then
    LITNG_HOST="${LIGHTNING_CLOUD_SPACE_HOST}"
elif [ -n "${LIGHTNING_CLOUD_SPACE_ID:-}" ]; then
    LITNG_HOST="${LIGHTNING_CLOUD_SPACE_ID}.${LITNG_SUFFIX}"
else
    LITNG_HOST=""
fi

if [ -n "${LITNG_HOST}" ]; then
    API_URL="https://${PROXY_PORT}-${LITNG_HOST}"
    echo "⚡ Lightning.ai detected — API_URL=${API_URL}"
else
    API_URL="http://localhost:${PROXY_PORT}"
    echo "💻 Local environment — API_URL=${API_URL}"
fi
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
