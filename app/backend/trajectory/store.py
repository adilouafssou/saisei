"""Trajectory-store abstraction (Feature 3).

The storage seam for the agent-trajectory data flywheel, mirroring the audit
sink and the ``MockDataProvider`` pattern: call sites use one interface and never
change when a real backend is dropped in. Implementations:

- :class:`NullTrajectoryStore` — the OFFLINE DEFAULT: no-op ``append``, empty
  ``read``. Keeps ``make verify`` / CI fully offline and byte-stable when no
  trajectory backend is configured.
- :class:`InMemoryTrajectoryStore` — for TESTS / in-session use: an ordered
  per-``thread_id`` list.
- ``PostgresTrajectoryStore`` — production, append-only (see
  ``store_postgres.py``), same staging as the audit Postgres sink.

:func:`get_trajectory_store` selects the implementation from settings
(Postgres when ``SAISEI_TRAJECTORY_DSN`` is set, else Null).

Like the audit sink, the interface exposes ONLY append + read — there is
deliberately no update or delete, so the append-only contract is structural.
The corpus is a training signal, so a strict **data-governance** boundary
applies: enabling persistence is the bank's explicit decision (empty DSN stores
nothing), and financial data never leaves the bank's VPC.

This module is pure/offline for Null + InMemory (stdlib + the record model only).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.backend.trajectory.record import TrajectoryRecord
from app.shared.settings import Settings, get_settings

__all__ = [
    "TrajectoryStore",
    "NullTrajectoryStore",
    "InMemoryTrajectoryStore",
    "get_trajectory_store",
]


@runtime_checkable
class TrajectoryStore(Protocol):
    """Append-only trajectory storage seam.

    Implementations expose ONLY append + read — no update or delete — so the
    append-only contract is structural at the interface level.
    """

    def append(self, record: TrajectoryRecord) -> None:
        """Append one trajectory record (best-effort at the call site)."""
        ...

    def read(self, thread_id: str) -> list[TrajectoryRecord]:
        """Return the records for a thread_id in write order."""
        ...


class NullTrajectoryStore:
    """Offline default: a no-op store (no persistence).

    ``append`` discards; ``read`` is always empty. Keeps the system fully
    offline and byte-stable when no trajectory backend is configured — identical
    posture to the audit NullAuditSink / the mock-provider contract.
    """

    def append(self, record: TrajectoryRecord) -> None:  # noqa: D102 - see class doc
        return None

    def read(self, thread_id: str) -> list[TrajectoryRecord]:  # noqa: D102
        return []


class InMemoryTrajectoryStore:
    """In-memory append-only store for tests (ordered per thread_id).

    Stores records grouped by ``thread_id`` in append order. Append-only: there
    is no update or delete. Returned lists are copies so a caller cannot mutate
    the store through the returned reference.
    """

    def __init__(self) -> None:
        self._by_thread: dict[str, list[TrajectoryRecord]] = {}

    def append(self, record: TrajectoryRecord) -> None:  # noqa: D102 - see class doc
        self._by_thread.setdefault(record.thread_id, []).append(record)

    def read(self, thread_id: str) -> list[TrajectoryRecord]:  # noqa: D102
        return list(self._by_thread.get(thread_id, []))


def get_trajectory_store(settings: Settings | None = None) -> TrajectoryStore:
    """Return the configured trajectory store (Postgres when DSN set, else Null).

    Mirrors ``get_audit_sink`` / the ``MockDataProvider`` seam: with no
    ``trajectory_dsn`` configured this returns :class:`NullTrajectoryStore`,
    keeping the system offline-safe. When the DSN is set,
    ``PostgresTrajectoryStore`` (``store_postgres.py``) is constructed; the
    Null store is also the safe fallback if the optional psycopg driver is
    unavailable, so an unconfigured backend never breaks the workflow.

    Args:
        settings: Optional settings override (defaults to cached settings).

    Returns:
        A :class:`TrajectoryStore` implementation.
    """
    settings = settings or get_settings()
    dsn = getattr(settings, "trajectory_dsn", "") or ""
    if not dsn:
        return NullTrajectoryStore()
    try:
        from app.backend.trajectory.store_postgres import PostgresTrajectoryStore
    except ImportError:
        return NullTrajectoryStore()
    return PostgresTrajectoryStore(dsn)
