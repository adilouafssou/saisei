"""Structured logging setup for Saisei.

Uses ``structlog`` exclusively; ``print()`` is banned project-wide. Call
:func:`configure_logging` once at startup, then obtain loggers via
:func:`get_logger`.

This module is the canonical location under ``app.shared.logging``.
The legacy path ``shared.logging`` re-exports from here.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import FilteringBoundLogger

__all__ = ["configure_logging", "get_logger"]


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog with JSON output suitable for containers.

    Args:
        level: Root log level name (e.g. ``"INFO"``, ``"DEBUG"``).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Return a structured logger.

    Args:
        name: Optional logger name (typically ``__name__``).

    Returns:
        A bound structlog logger.
    """
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger
