"""Saisei application entry point.

Initialises the Reflex app (``rx.App``) and mounts the FastAPI health/readiness
probes onto the Reflex API router. The lifespan handler configures structured
logging at startup and emits a shutdown event.

Exports:
- ``app``: the Reflex ``rx.App`` instance (used by ``reflex run``).
- ``create_app``: factory that returns the underlying FastAPI application
  (used by uvicorn: ``uvicorn app.main:create_app --factory``).
- ``asgi_app``: the ASGI application for direct mounting.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import reflex as rx
from fastapi import FastAPI

from app.shared.logging import configure_logging, get_logger
from app.shared.settings import get_settings

__all__ = ["app", "create_app", "asgi_app"]


@asynccontextmanager
async def _lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger(__name__)
    log.info("saisei.startup", use_mocks=settings.use_mocks)
    yield
    log.info("saisei.shutdown")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application with health probes.

    Returns:
        The configured FastAPI application.
    """
    application = FastAPI(
        title="Saisei API",
        version="0.1.0",
        description="Autonomous EWS & Keiei Kaizen Keikakusho orchestrator.",
        lifespan=_lifespan,
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @application.get("/ready")
    async def ready() -> dict[str, str]:
        """Readiness probe."""
        settings = get_settings()
        return {"status": "ready", "mocks": str(settings.use_mocks).lower()}

    return application


#: Underlying FastAPI ASGI application (for uvicorn direct use).
asgi_app: FastAPI = create_app()

# ---------------------------------------------------------------------------
# Reflex app — mounts the FastAPI endpoints onto the Reflex API router.
# ---------------------------------------------------------------------------
# Import pages to register them with the Reflex app.
from app.frontend.pages import index  # noqa: E402 — must come after rx import

app = rx.App(
    api_transformer=asgi_app,
    theme=rx.theme(
        appearance="dark",
        accent_color="indigo",
        gray_color="slate",
        radius="large",
        scaling="100%",
    ),
)
app.add_page(index, title="Saisei 再生 — 経営改善")
