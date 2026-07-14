"""Audit-sink abstraction (Feature 7, spec §5 / §12 step 2).

The sink is the storage seam for the audit ledger, mirroring the
``MockDataProvider`` pattern: nodes call one interface and never change when a
real backend is dropped in. Three implementations:

- :class:`NullAuditSink` — the OFFLINE DEFAULT: no-op ``append``, empty ``read``.
  Keeps ``make verify`` / CI fully offline and byte-stable when no audit backend
  is configured.
- :class:`InMemoryAuditSink` — for TESTS: an ordered per-``thread_id`` list.
- ``PostgresAuditSink`` — production, append-only (added in a later step).

:func:`get_audit_sink` selects the implementation from settings (Postgres when
``SAISEI_AUDIT_DSN`` is set, else Null).

:meth:`AuditSink.verify_chain` re-derives each event's ``content_hash`` and
checks the ``prev_hash`` linkage, giving tamper-evidence: any retro-edit of a
stored event (or a broken link) is reported with the first offending event id.

This module is pure/offline for Null + InMemory (stdlib + the audit model only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.backend.audit.audit_log import AuditEvent, AuditEventType, compute_content_hash
from app.backend.secrets import resolve_secret
from app.shared.settings import Settings, get_settings

__all__ = [
    "ChainVerdict",
    "AuditQuery",
    "AuditSink",
    "NullAuditSink",
    "InMemoryAuditSink",
    "verify_chain",
    "get_audit_sink",
]


@dataclass(frozen=True)
class ChainVerdict:
    """Result of verifying a thread's hash chain.

    Attributes:
        ok: True iff every event's content_hash recomputes and links correctly.
        broken_at: The ``event_id`` of the first offending event, or None.
        reason: Human-readable explanation (empty when ``ok``).
    """

    ok: bool
    broken_at: str | None = None
    reason: str = ""


#: Hard ceiling on the number of rows a single cross-thread query may return,
#: so an examiner query can never accidentally pull an unbounded result set.
MAX_QUERY_LIMIT = 1000


@dataclass(frozen=True)
class AuditQuery:
    """Filter for a CROSS-THREAD examiner query over the ledger.

    Every field is optional; an empty query matches all events (subject to
    ``limit``). The ledger is queried in global write order (newest last). This
    is the book-level / regulator view that complements per-thread ``read`` --
    it answers "show me every human_decision by actor X across all borrowers in
    March", which a single ``thread_id`` read cannot.

    READ-ONLY: this only selects. It adds NO mutation path; the append-only
    contract of the ledger is unchanged.

    Attributes:
        tdb_code: Restrict to one borrower's 7-digit code.
        event_type: Restrict to one :class:`AuditEventType`.
        actor: Restrict to one actor (e.g. a banker id).
        since: ISO 8601 lower bound (inclusive) on ``created_at``. ISO 8601
            timestamps sort lexically, so a string compare is a correct time
            compare.
        until: ISO 8601 upper bound (inclusive) on ``created_at``.
        limit: Max rows to return (clamped to :data:`MAX_QUERY_LIMIT`).
    """

    tdb_code: str | None = None
    event_type: AuditEventType | None = None
    actor: str | None = None
    since: str | None = None
    until: str | None = None
    limit: int = 100

    def effective_limit(self) -> int:
        """Return the limit clamped to [1, MAX_QUERY_LIMIT]."""
        return max(1, min(int(self.limit), MAX_QUERY_LIMIT))

    def matches(self, event: AuditEvent) -> bool:
        """Return whether an event satisfies every set filter (for in-memory use).

        The Postgres sink builds an equivalent WHERE clause in SQL; this method
        is the single source of truth for the filter semantics used by the
        in-memory sink and tests, so the two implementations stay aligned.
        """
        if self.tdb_code is not None and event.tdb_code != self.tdb_code:
            return False
        if self.event_type is not None and event.event_type != self.event_type:
            return False
        if self.actor is not None and event.actor != self.actor:
            return False
        if self.since is not None and event.created_at < self.since:
            return False
        return not (self.until is not None and event.created_at > self.until)


def verify_chain(events: list[AuditEvent]) -> ChainVerdict:
    """Verify an ordered list of events forms an intact hash chain.

    Checks, in order, for each event:
      1. its stored ``content_hash`` equals a recomputation (no field tampering);
      2. its ``prev_hash`` equals the previous event's ``content_hash`` (or ""
         for the first/genesis event).

    An empty list is trivially OK.

    Args:
        events: Events in write order (e.g. from :meth:`AuditSink.read`).

    Returns:
        A :class:`ChainVerdict`.
    """
    prev_hash = ""
    for event in events:
        recomputed = compute_content_hash(event)
        if event.content_hash != recomputed:
            return ChainVerdict(
                ok=False,
                broken_at=event.event_id,
                reason=(
                    f"content_hash mismatch for {event.event_id} (event was modified after write)"
                ),
            )
        if event.prev_hash != prev_hash:
            return ChainVerdict(
                ok=False,
                broken_at=event.event_id,
                reason=(
                    f"prev_hash linkage broken at {event.event_id} (missing or reordered event)"
                ),
            )
        prev_hash = event.content_hash
    return ChainVerdict(ok=True)


@runtime_checkable
class AuditSink(Protocol):
    """Append-only audit-ledger storage seam.

    Implementations expose ONLY append + read + verify — there is deliberately no
    update or delete method, so the append-only contract is structural at the
    interface level.
    """

    def append(self, event: AuditEvent) -> None:
        """Append one event to the ledger (best-effort at the call site)."""
        ...

    def read(self, thread_id: str) -> list[AuditEvent]:
        """Return the events for a thread_id in write order."""
        ...

    def query(self, query: AuditQuery) -> list[AuditEvent]:
        """Return events across ALL threads matching ``query`` (write order)."""
        ...

    def verify_chain(self, thread_id: str) -> ChainVerdict:
        """Verify the hash chain for a thread_id."""
        ...


class NullAuditSink:
    """Offline default: a no-op sink (no storage).

    ``append`` discards; ``read`` is always empty; ``verify_chain`` is trivially
    OK. This keeps the system fully offline and byte-stable when no audit backend
    is configured — identical posture to the mock-provider contract.
    """

    def append(self, event: AuditEvent) -> None:  # noqa: D102 - see class doc
        return None

    def read(self, thread_id: str) -> list[AuditEvent]:  # noqa: D102
        return []

    def query(self, query: AuditQuery) -> list[AuditEvent]:  # noqa: D102
        return []

    def verify_chain(self, thread_id: str) -> ChainVerdict:  # noqa: D102
        return ChainVerdict(ok=True)


class InMemoryAuditSink:
    """In-memory append-only sink for tests (ordered per thread_id).

    Stores events grouped by ``thread_id`` in append order. Append-only: there
    is no update or delete. Returned lists are copies so a caller cannot mutate
    the store through the returned reference.
    """

    def __init__(self) -> None:
        self._by_thread: dict[str, list[AuditEvent]] = {}
        self._global: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:  # noqa: D102 - see class doc
        self._by_thread.setdefault(event.thread_id, []).append(event)
        self._global.append(event)

    def read(self, thread_id: str) -> list[AuditEvent]:  # noqa: D102
        return list(self._by_thread.get(thread_id, []))

    def query(self, query: AuditQuery) -> list[AuditEvent]:  # noqa: D102
        matches = [event for event in self._global if query.matches(event)]
        return matches[: query.effective_limit()]

    def verify_chain(self, thread_id: str) -> ChainVerdict:  # noqa: D102
        return verify_chain(self.read(thread_id))


def get_audit_sink(settings: Settings | None = None) -> AuditSink:
    """Return the configured audit sink (Postgres when DSN set, else Null).

    Mirrors ``get_retrieval_provider`` / the ``MockDataProvider`` seam: with no
    ``audit_dsn`` configured this returns :class:`NullAuditSink`, keeping the
    system offline-safe. ``PostgresAuditSink`` is wired in a later step; until
    then an unconfigured (or any) environment resolves to the Null sink.

    Args:
        settings: Optional settings override (defaults to cached settings).

    Returns:
        An :class:`AuditSink` implementation.
    """
    settings = settings or get_settings()
    dsn = resolve_secret(getattr(settings, "audit_dsn", "") or "")
    if not dsn:
        return NullAuditSink()
    # PostgresAuditSink lands in a later step; until then, fail safe to Null so
    # an unconfigured backend never breaks the workflow.
    try:
        from app.backend.audit.sink_postgres import PostgresAuditSink
    except ImportError:
        return NullAuditSink()
    return PostgresAuditSink(dsn)
