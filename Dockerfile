# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Multi-stage build: keep Node + build tooling out of the final image so the
# runtime stage is small. The builder resolves the venv and application; the
# runtime stage copies only what it needs to run.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    TELEMETRY_ENABLED=false

# Node.js is needed only at build time for Reflex's frontend build.
# `unzip` is required by `reflex init`: Reflex downloads and extracts the Bun
# runtime and the frontend template as zip archives, and fails with
# "unzip not installed" if it is missing.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv (Astral).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer (cached on the lockfile). Resolve once here; the runtime
# stage copies the resulting /app/.venv rather than re-running uv sync.
# A BuildKit cache mount keeps uv's download cache warm across builds.
# NOTE: --no-install-project intentionally omits installing THIS package into
# the venv. Reflex runs via `reflex run` from WORKDIR /app, so `import app`
# resolves on sys.path without an installed dist-info. We deliberately skip the
# second `uv sync` the old single-stage build ran. If any runtime code starts
# relying on installed package metadata (importlib.metadata.version("app")) or
# declared console entry points, drop --no-install-project here.
COPY pyproject.toml uv.lock* README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then uv sync --no-dev --frozen --no-install-project; \
    else uv sync --no-dev --no-install-project; fi

# Application code (after deps so code changes don't bust the dependency layer).
# NOTE: app/ includes app/backend/tools/fixtures/ (bundled fixture JSON).
COPY app ./app
COPY assets ./assets
COPY rxconfig.py ./

# Vendor the CJK font for PDF export (Noto Sans JP, OFL 1.1). The PDF exporter
# (app/backend/export/_markdown_pdf.py) embeds a Japanese font or it raises
# PdfFontUnavailableError (it never emits tofu). The multi-MB binary is a
# build/deploy input kept out of git (.gitignore), so fetch it here -- mirroring
# the CI step -- so a freshly built image has a working PDF exporter out of the
# box. fpdf2 subsets the font at render time, keeping generated PDFs small. The
# OFL 1.1 license travels with the repo at assets/fonts/OFL.txt. Deployers who
# supply their own font can instead set SAISEI_PDF_FONT_PATH at runtime (it takes
# precedence over this vendored path).
RUN curl -fsSL -o assets/fonts/NotoSansJP-Regular.ttf \
      "https://github.com/notofonts/noto-cjk/raw/main/Sans/Variable/TTF/Subset/NotoSansJP-VF.ttf" && \
    # Verify it is a real TrueType/OpenType font (sfnt magic), not an error
    # page, so a bad fetch fails the build loudly instead of shipping an image
    # whose PDF export breaks at runtime.
    python - <<'PY'
import sys
with open("assets/fonts/NotoSansJP-Regular.ttf", "rb") as fh:
    magic = fh.read(4)
ok = magic in (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")
print(f"font magic={magic!r} ok={ok}")
sys.exit(0 if ok else 1)
PY

# Pre-install the Reflex frontend toolchain at BUILD time so first run is an
# incremental compile instead of a cold, from-scratch install. `reflex init`
# materialises the .web/ project and installs its Node dependencies; this is
# the slow step that otherwise ran on first request and made cold start slow.
#
# We deliberately do NOT `reflex export`/prebuild the bundle here: rxconfig.py
# bakes api_url into the frontend at build time, but the real API_URL is only
# known at run time (scripts/setup_env.sh auto-detects Lightning.ai). Baking it
# now would freeze a wrong URL. So we warm the toolchain, not the final bundle.
#
# The build must still succeed if pre-warming fails (e.g. npm network timeout):
# we echo a visible WARNING rather than `|| true` so the lost optimisation is
# surfaced in the build log instead of being swallowed silently.
RUN --mount=type=cache,target=/root/.cache/uv \
    reflex init 2>&1 || echo "WARNING: reflex init failed - cold-start pre-warming skipped"

# ---------------------------------------------------------------------------
# Runtime stage: slim Python + Node runtime only (Reflex needs node at run
# time to serve the frontend in prod). No uv, no build caches, no apt lists.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    TELEMETRY_ENABLED=false

# `unzip` is needed at run time too: on first `reflex run` the frontend
# toolchain may re-materialise/extract Bun and the .web project if it was not
# pre-warmed in the builder, so keep it available in the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the resolved environment and application from the builder.
COPY --from=builder /app /app

EXPOSE 3000 8000
# REFLEX_ENV (dev|prod) selects the run mode; defaults to prod.
# docker-compose / `make run-dev` set this to switch modes without rebuilding.
ENV REFLEX_ENV=prod
# FRONTEND_PORT is the port the Reflex frontend binds/serves on. Defaults to
# 3000 (the compose/Nginx + Lightning.ai contract). Hugging Face Spaces route
# public traffic to a single declared app_port and also inject ${PORT}; on HF
# there is NO Nginx, so reflex serves the frontend directly on this port. We
# honour ${PORT} (HF) first, then ${FRONTEND_PORT}, then 3000, so the SAME image
# boots unchanged on local / Lightning.ai / Hugging Face.
ENV FRONTEND_PORT=3000
ENTRYPOINT ["sh", "-c", "exec reflex run --env \"${REFLEX_ENV:-prod}\" --backend-host 0.0.0.0 --frontend-port \"${PORT:-${FRONTEND_PORT:-3000}}\""]
# Alternative: serve only the FastAPI ASGI app (health/readiness + API) via
# uvicorn, e.g. for a backend-only deployment:
# CMD ["uvicorn", "app.app:asgi_app", "--host", "0.0.0.0", "--port", "8000"]
