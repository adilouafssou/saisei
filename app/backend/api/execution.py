"""Off-request-path execution seam for graph runs (productionise: async + scale).

The run/resume HTTP endpoints drive the LangGraph graph, which can make long
LLM / RAG calls. Doing that work INLINE in the request means the HTTP call
blocks for the whole assessment. This module is the seam that moves the work
OFF the request path while keeping the system offline-safe and the existing
synchronous behaviour as the default.

Two pieces:

* **A run-status registry** (:class:`RunRegistry`) tracking each ``thread_id``'s
  lifecycle phase (``running`` → ``awaiting_decision`` / ``done`` / ``error``).
  This is what lets ``GET /runs/{thread_id}`` report progress while the work is
  in flight, instead of the client having to hold a blocking request open.
* **An executor seam** (:class:`RunExecutor`) with an in-process thread-pool
  default (:class:`ThreadRunExecutor`). It runs the (synchronous, blocking)
  graph work in a worker thread and updates the registry on completion /
  failure. A real DISTRIBUTED worker (Celery / RQ / Arq over the existing Redis)
  drops into this same seam for horizontal scale across processes — the route
  code never changes, exactly like the audit-sink and identity seams.

Offline / default posture
-------------------------
The whole async path is OPT-IN via ``Settings.run_async``. When it is False
(the default), the endpoints keep their original blocking behaviour and this
module's executor is simply unused — so ``make verify``, the existing API tests,
and the demo are byte-for-byte unaffected. The registry is process-local and
in-memory; durable cross-process status belongs with the distributed executor
when a deployment adopts one (documented seam, not shipped speculatively).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.shared.logging import get_logger

__all__ = [
    "RunPhase",
    "RunStatus",
    "RunRegistry",
    "RunExecutor",
    "ThreadRunExecutor",
    "get_run_registry",
    "get_run_executor",
]

_log = get_logger(__name__)


class RunPhase(StrEnum):
    """Lifecycle phase of a run, as tracked off the request path.

    ``RUNNING`` is the new phase the async path introduces: the graph work has
    been dispatched and is executing in the background. The other three mirror
    the existing snapshot vocabulary so the API response shape is unchanged.
    """

    RUNNING = "running"
    AWAITING_DECISION = "awaiting_decision"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class RunStatus:
    """A run's last-known lifecycle status (registry entry).

    Attributes:
        thread_id: The run's thread id.
        phase: The last recorded :class:`RunPhase`.
        error: A short error message when ``phase`` is ERROR, else "".
    """

    thread_id: str
    phase: RunPhase
    error: str = ""


class RunRegistry:
    """Thread-safe, in-process map of ``thread_id`` -> :class:`RunStatus`.

    Tracks lifecycle phase for runs dispatched off the request path so a poll
    (``GET /runs/{thread_id}``) can report ``running`` before any checkpoint
    snapshot exists, and ``error`` when a background run failed (which the
    checkpointer alone cannot express).
    """

    def __init__(self) -> None:
        self._by_thread: dict[str, RunStatus] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, phase: RunPhase, *, error: str = "") -> None:
        """Record ``thread_id``'s phase (and optional error)."""
        with self._lock:
            self._by_thread[thread_id] = RunStatus(thread_id=thread_id, phase=phase, error=error)

    def get(self, thread_id: str) -> RunStatus | None:
        """Return the recorded status for ``thread_id``, or None if unknown."""
        with self._lock:
            return self._by_thread.get(thread_id)

    def clear(self) -> None:
        """Drop all entries (test isolation / process reset)."""
        with self._lock:
            self._by_thread.clear()


@runtime_checkable
class RunExecutor(Protocol):
    """Seam that runs a blocking graph job off the request path.

    Implementations take a no-argument callable (the bound graph work) plus the
    ``thread_id`` it concerns, and arrange for it to run without blocking the
    caller. The default is an in-process thread pool; a distributed worker fits
    the same shape.
    """

    def submit(self, thread_id: str, job: Callable[[], None]) -> None:
        """Schedule ``job`` to run off the request path."""
        ...


class ThreadRunExecutor:
    """In-process thread-pool executor (the offline-safe default).

    Runs each job in a bounded :class:`~concurrent.futures.ThreadPoolExecutor`
    and updates the :class:`RunRegistry` to ERROR if the job raises, so a
    background failure is observable via the status poll. Adequate for a single
    process; swap in a distributed executor for multi-process horizontal scale.
    """

    def __init__(self, registry: RunRegistry, max_workers: int = 4) -> None:
        self._registry = registry
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="saisei-run")

    def submit(self, thread_id: str, job: Callable[[], None]) -> None:
        """Submit ``job`` to the pool, marking the run ERROR if it raises."""

        def _wrapped() -> None:
            try:
                job()
            except Exception as exc:  # noqa: BLE001 - record, never crash the worker
                _log.warning("run.async_job_failed", thread_id=thread_id, error=str(exc))
                self._registry.set(thread_id, RunPhase.ERROR, error=str(exc))

        self._pool.submit(_wrapped)


# ---------------------------------------------------------------------------
# Process-wide singletons (lazily created), mirroring the checkpointer pattern.
# ---------------------------------------------------------------------------

_REGISTRY: RunRegistry | None = None
_EXECUTOR: RunExecutor | None = None
_LOCK = threading.Lock()


def get_run_registry() -> RunRegistry:
    """Return the process-wide :class:`RunRegistry`, creating it once."""
    global _REGISTRY
    if _REGISTRY is None:
        with _LOCK:
            if _REGISTRY is None:
                _REGISTRY = RunRegistry()
    return _REGISTRY


def get_run_executor() -> RunExecutor:
    """Return the process-wide :class:`RunExecutor` (thread pool by default)."""
    global _EXECUTOR
    if _EXECUTOR is None:
        with _LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = ThreadRunExecutor(get_run_registry())
    return _EXECUTOR
