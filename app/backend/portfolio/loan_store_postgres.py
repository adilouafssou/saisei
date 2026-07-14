"""Postgres-backed append-only loan-lifecycle event store.

Persists the loan-lifecycle event log (:class:`~app.shared.models.loan.LoanEvent`)
so a facility's status survives restarts and is durable across runs. The log is
an append-only ledger — a loan advances by *recording a new event*, never by
mutating a prior one — so this store mirrors the audit ledger
(``app/backend/audit/sink_postgres.py``) rather than the mutable, current-state
Portfolio watchlist:

1. **App layer** — this class issues only ``INSERT`` and ``SELECT``; there is no
   update or delete method, so append-only is structural.
2. **DB layer** — a ``BEFORE UPDATE OR DELETE`` trigger that ``RAISE``s, created
   idempotently at init, so even a direct SQL mutation against the table fails.

Every statement is TENANT-SCOPED: the composite key is ``(tenant_id, loan_id,
seq)`` and reads filter by ``(tenant_id, loan_id)``, so one bank can never read
another's facility log.

``psycopg`` (psycopg3, the same driver the checkpointer and audit sink use) is
imported lazily so this module stays importable with no DB driver present. The
factory :func:`get_loan_store` constructs this only when ``SAISEI_LOAN_DSN`` is
set; otherwise it returns the offline ``NullLoanStore`` and nothing is stored at
rest — keeping the system fully testable offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from app.shared.logging import get_logger
from app.shared.models.loan import LoanEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection as _PsycopgConnection

    Connection = _PsycopgConnection[Any]

__all__ = [
    "LoanStore",
    "NullLoanStore",
    "PostgresLoanStore",
    "LOAN_EVENT_TABLE",
    "SCHEMA_SQL",
    "get_loan_store",
    "persist_loan_events",
    "read_loan_events",
]

_log = get_logger(__name__)

#: The append-only loan-event ledger table name.
LOAN_EVENT_TABLE = "saisei_loan_events"

#: Idempotent schema bootstrap: table + index + append-only trigger. The trigger
#: is the DB-layer guarantee that a recorded loan event is never mutated or
#: deleted — the same tamper-evident posture as the audit ledger.
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {LOAN_EVENT_TABLE} (
    tenant_id  TEXT NOT NULL,
    loan_id    TEXT NOT NULL,
    status     TEXT NOT NULL,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    seq        BIGSERIAL,
    PRIMARY KEY (tenant_id, loan_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_loan_events_facility
    ON {LOAN_EVENT_TABLE} (tenant_id, loan_id, seq);

CREATE OR REPLACE FUNCTION saisei_loan_events_no_mutate()
    RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'saisei_loan_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'saisei_loan_events_no_mutate_trg'
    ) THEN
        CREATE TRIGGER saisei_loan_events_no_mutate_trg
            BEFORE UPDATE OR DELETE ON {LOAN_EVENT_TABLE}
            FOR EACH ROW EXECUTE FUNCTION saisei_loan_events_no_mutate();
    END IF;
END;
$$;
"""

_INSERT_SQL = f"""
INSERT INTO {LOAN_EVENT_TABLE} (
    tenant_id, loan_id, status, at, actor, note
) VALUES (
    %(tenant_id)s, %(loan_id)s, %(status)s, %(at)s, %(actor)s, %(note)s
)
"""

_SELECT_SQL = f"""
SELECT status, at, actor, note
FROM {LOAN_EVENT_TABLE}
WHERE tenant_id = %(tenant_id)s AND loan_id = %(loan_id)s
ORDER BY seq ASC
"""


class LoanStore(Protocol):
    """Append-only loan-event store contract (no update/delete by design)."""

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        """Persist one loan-lifecycle event (append-only)."""
        ...

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        """Return a facility's events in write order (oldest first)."""
        ...


class NullLoanStore:
    """Offline no-op store: nothing is stored at rest (no DSN configured)."""

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        """No-op append (offline)."""
        return None

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        """Return an empty log (offline)."""
        return []


class PostgresLoanStore:
    """Append-only, tenant-scoped Postgres loan-event ledger (production).

    Args:
        dsn: A plain libpq PostgreSQL DSN (``postgresql://...``), typically the
            same instance as the checkpointer. The schema is created
            idempotently on construction.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._setup()

    def _connect(self) -> Connection:
        """Open a new psycopg connection (lazy import keeps the module offline-safe)."""
        import psycopg

        return psycopg.connect(self._dsn)

    def _setup(self) -> None:
        """Create the table, index, and append-only trigger idempotently."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        """Insert one loan-lifecycle event (append-only)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, self._to_row(tenant_id, loan_id, event))
            conn.commit()

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        """Return a facility's events in write order (by ``seq``)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"tenant_id": tenant_id, "loan_id": loan_id})
            rows = cur.fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _to_row(tenant_id: str, loan_id: str, event: LoanEvent) -> dict[str, Any]:
        """Map a loan event to INSERT params."""
        return {
            "tenant_id": tenant_id,
            "loan_id": loan_id,
            "status": event.status.value,
            "at": event.at.isoformat(),
            "actor": event.actor,
            "note": event.note,
        }

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> LoanEvent:
        """Rebuild a frozen LoanEvent from a SELECT row (column order matched)."""
        return LoanEvent.model_validate(
            {"status": row[0], "at": row[1], "actor": row[2], "note": row[3]}
        )


def get_loan_store(dsn: str | None) -> LoanStore:
    """Return the production store when a DSN is set, else the offline no-op.

    Args:
        dsn: The loan-event DSN (``SAISEI_LOAN_DSN``) or ``None`` / empty.

    Returns:
        A :class:`PostgresLoanStore` when ``dsn`` is truthy, otherwise a
        :class:`NullLoanStore` (offline; nothing stored at rest).
    """
    if dsn:
        return PostgresLoanStore(dsn)
    return NullLoanStore()


def persist_loan_events(
    state: Any,
    events: list[dict[str, Any]],
    *,
    log_event: str = "loan.persisted",
) -> None:
    """Best-effort durable append of newly recorded loan events to the store.

    The single shared persistence seam for the loan-lifecycle ledger, mirroring
    the audit ledger's ``record_event`` posture: a strict side-record that NEVER
    affects a node's return, a gate, a route, or a figure, and is NEVER fatal.
    The events are already in the LangGraph checkpointer state (via the node's
    ``loan_events`` return); this additionally persists them to the dedicated,
    append-only, tenant-scoped loan ledger so a facility's lifecycle is durable
    in its OWN store -- which is what lets a facility originated by the breadth
    graph be RESUMED by the turnaround graph in a later session (``intake_node``
    reads this same ledger).

    Offline (no ``SAISEI_LOAN_DSN``) the factory returns ``NullLoanStore`` and
    this is a no-op, keeping the system byte-stable. A no-op also occurs when no
    loan is attached (``state.loan_id`` is falsy) or there are no new events.

    Args:
        state: Current graph state (source of ``loan_id`` + tenant scope). Read
            via ``getattr`` so any state-like object works.
        events: The newly recorded LoanEvent dicts to persist.
        log_event: Structured-log event name for the success line, so each call
            site is attributable (e.g. ``origination.loan_persisted``).
    """
    loan_id = str(getattr(state, "loan_id", "") or "")
    if not events or not loan_id:
        return
    try:
        from app.shared.settings import get_settings

        settings = get_settings()
        store = get_loan_store(settings.loan_dsn)
        tenant_id = settings.loan_tenant_default
        for raw in events:
            store.append(tenant_id, loan_id, LoanEvent.model_validate(raw))
        _log.info(log_event, loan_id=loan_id, count=len(events))
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort, never fatal
        _log.warning("loan.persist_failed", error=str(exc), loan_id=loan_id)


def read_loan_events(
    loan_id: str,
    *,
    log_event: str = "loan.read_failed",
) -> list[dict[str, Any]]:
    """Best-effort read of a facility's persisted loan-event log (oldest-first).

    The read counterpart of :func:`persist_loan_events` and the single shared
    seam for resuming a facility's TRUE cross-run history from the dedicated,
    append-only, tenant-scoped loan ledger. The durable ledger is what lets a
    facility originated by the breadth graph, or moved to 条件変更 / 管理回収 by the
    depth graph, be picked up by ANOTHER graph (servicing / a later assessment)
    in a separate session with its real current status, rather than a re-seeded
    bootstrap.

    Offline (no ``SAISEI_LOAN_DSN``) the factory returns ``NullLoanStore`` whose
    ``read`` is ``[]``, so this returns ``[]`` and the caller falls back to
    whatever it already has (byte-stable default). Any read failure is logged and
    treated as "no durable history" rather than propagated, so a ledger hiccup
    never breaks a run.

    Args:
        loan_id: The facility id to read (``f"L-{hojin_bango}"`` or an
            origination ``f"L-{tdb_code}"``). Empty ``loan_id`` short-circuits to
            ``[]``.
        log_event: Structured-log event name for a read failure, so each call
            site is attributable.

    Returns:
        The persisted LoanEvent dicts (oldest-first), or ``[]`` when there is no
        durable history / no store configured / on any failure.
    """
    loan_id = str(loan_id or "")
    if not loan_id:
        return []
    try:
        from app.shared.settings import get_settings

        settings = get_settings()
        store = get_loan_store(settings.loan_dsn)
        events = store.read(settings.loan_tenant_default, loan_id)
        return [e.model_dump(mode="json") for e in events]
    except Exception as exc:  # noqa: BLE001 - durable read is best-effort, never fatal
        _log.warning(log_event, error=str(exc), loan_id=loan_id)
        return []
