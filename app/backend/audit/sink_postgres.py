"""Postgres-backed append-only audit sink (Feature 7, spec §5 / §6 / §12 step 6).

The production :class:`AuditSink`: a single append-only table in the existing
Postgres instance (it may reuse the checkpointer DB). Append-only is enforced at
TWO layers, defence in depth:

1. **App layer** — this class issues only ``INSERT`` and ``SELECT``. There is no
   update or delete method (the :class:`~app.backend.audit.sink.AuditSink`
   Protocol does not declare one), so the contract is structural.
2. **DB layer** — a ``BEFORE UPDATE OR DELETE`` trigger that ``RAISE``s, created
   idempotently at init, so even a direct SQL ``UPDATE``/``DELETE`` against the
   table fails. (A dedicated DB role granted only ``INSERT, SELECT`` is the
   recommended further hardening; documented in ``DATA_ARCHITECTURE.md``.)

The table, indexes, function, and trigger are created idempotently on init
(``CREATE ... IF NOT EXISTS`` / ``CREATE OR REPLACE FUNCTION`` + an idempotent
trigger create), matching how the offline-first stack bootstraps until Alembic
(Feature 6) owns migrations.

``psycopg`` (psycopg3, the same driver the LangGraph ``PostgresSaver`` uses) is
imported lazily inside the methods so this module stays importable with no DB
driver present; :func:`~app.backend.audit.sink.get_audit_sink` only constructs
this class when ``SAISEI_AUDIT_DSN`` is set, otherwise it returns the offline
``NullAuditSink``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.sink import AuditQuery, ChainVerdict, verify_chain
from app.shared.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection as _PsycopgConnection

    Connection = _PsycopgConnection[Any]

__all__ = ["PostgresAuditSink", "AUDIT_TABLE", "SCHEMA_SQL"]

_log = get_logger(__name__)

#: The append-only ledger table name.
AUDIT_TABLE = "saisei_audit_log"

#: Idempotent schema bootstrap: table + indexes + append-only trigger.
#: The trigger is the DB-layer guarantee that an event is never mutated or
#: deleted after write — the tamper-evidence the hash chain assumes.
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
    event_id           TEXT PRIMARY KEY,
    thread_id          TEXT NOT NULL,
    tdb_code           TEXT NOT NULL,
    hojin_bango        TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    actor              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    payload            JSONB NOT NULL,
    data_version       TEXT NOT NULL,
    thresholds_version TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    prev_hash          TEXT NOT NULL,
    signature          TEXT NOT NULL DEFAULT '',
    seq                BIGSERIAL
);
-- Idempotent column add so a ledger created before signing landed gains the
-- column without a migration tool (matches the offline-first bootstrap style).
ALTER TABLE {AUDIT_TABLE} ADD COLUMN IF NOT EXISTS signature TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_audit_thread ON {AUDIT_TABLE} (thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_tdb    ON {AUDIT_TABLE} (tdb_code, created_at);

CREATE OR REPLACE FUNCTION saisei_audit_no_mutate()
    RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'saisei_audit_log is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'saisei_audit_no_mutate_trg'
    ) THEN
        CREATE TRIGGER saisei_audit_no_mutate_trg
            BEFORE UPDATE OR DELETE ON {AUDIT_TABLE}
            FOR EACH ROW EXECUTE FUNCTION saisei_audit_no_mutate();
    END IF;
END;
$$;
"""

_INSERT_SQL = f"""
INSERT INTO {AUDIT_TABLE} (
    event_id, thread_id, tdb_code, hojin_bango, event_type, actor, created_at,
    payload, data_version, thresholds_version, content_hash, prev_hash, signature
) VALUES (
    %(event_id)s, %(thread_id)s, %(tdb_code)s, %(hojin_bango)s, %(event_type)s,
    %(actor)s, %(created_at)s, %(payload)s, %(data_version)s,
    %(thresholds_version)s, %(content_hash)s, %(prev_hash)s, %(signature)s
)
ON CONFLICT (event_id) DO NOTHING
"""

_SELECT_SQL = f"""
SELECT event_id, thread_id, tdb_code, hojin_bango, event_type, actor, created_at,
       payload, data_version, thresholds_version, content_hash, prev_hash, signature
FROM {AUDIT_TABLE}
WHERE thread_id = %(thread_id)s
ORDER BY seq ASC
"""

#: Columns shared by the per-thread read and the cross-thread query selects.
_SELECT_COLUMNS = (
    "event_id, thread_id, tdb_code, hojin_bango, event_type, actor, created_at, "
    "payload, data_version, thresholds_version, content_hash, prev_hash, signature"
)


class PostgresAuditSink:
    """Append-only Postgres audit ledger (production sink).

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
        """Create the table, indexes, and append-only trigger idempotently."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def append(self, event: AuditEvent) -> None:
        """Insert one sealed, hash-chained event (append-only).

        The event is already content-hashed and linked by ``record_event``; the
        sink only persists it. ``ON CONFLICT DO NOTHING`` makes a duplicate
        ``event_id`` a harmless no-op (idempotent retry), never an update.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, self._to_row(event))
            conn.commit()

    def read(self, thread_id: str) -> list[AuditEvent]:
        """Return the events for a thread_id in write order (by ``seq``)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"thread_id": thread_id})
            rows = cur.fetchall()
        return [self._from_row(row) for row in rows]

    def query(self, query: AuditQuery) -> list[AuditEvent]:
        """Return events across ALL threads matching ``query`` (global write order).

        Builds a parameterised WHERE clause from only the filters that are set
        (so an empty query selects everything up to the limit), orders by the
        global ``seq`` so the result is in true write order, and applies the
        clamped limit. READ-ONLY: a single SELECT, no mutation path.

        The WHERE semantics mirror :meth:`AuditQuery.matches` exactly so the
        Postgres and in-memory sinks agree:
        ``created_at`` bounds are inclusive and compared lexically (valid for
        ISO 8601), and each scalar filter is an equality.
        """
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if query.tdb_code is not None:
            clauses.append("tdb_code = %(tdb_code)s")
            params["tdb_code"] = query.tdb_code
        if query.event_type is not None:
            clauses.append("event_type = %(event_type)s")
            params["event_type"] = query.event_type.value
        if query.actor is not None:
            clauses.append("actor = %(actor)s")
            params["actor"] = query.actor
        if query.since is not None:
            clauses.append("created_at >= %(since)s")
            params["since"] = query.since
        if query.until is not None:
            clauses.append("created_at <= %(until)s")
            params["until"] = query.until
        params["limit"] = query.effective_limit()

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT {_SELECT_COLUMNS} FROM {AUDIT_TABLE}{where} ORDER BY seq ASC LIMIT %(limit)s"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._from_row(row) for row in rows]

    def verify_chain(self, thread_id: str) -> ChainVerdict:
        """Verify the hash chain for a thread_id (reuses the shared verifier)."""
        return verify_chain(self.read(thread_id))

    @staticmethod
    def _to_row(event: AuditEvent) -> dict[str, Any]:
        """Map an event to INSERT params (payload serialised to a JSON string)."""
        return {
            "event_id": event.event_id,
            "thread_id": event.thread_id,
            "tdb_code": event.tdb_code,
            "hojin_bango": event.hojin_bango,
            "event_type": event.event_type.value,
            "actor": event.actor,
            "created_at": event.created_at,
            "payload": json.dumps(event.payload, ensure_ascii=False),
            "data_version": event.data_version,
            "thresholds_version": event.thresholds_version,
            "content_hash": event.content_hash,
            "prev_hash": event.prev_hash,
            "signature": event.signature,
        }

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> AuditEvent:
        """Rebuild a frozen AuditEvent from a SELECT row (column order matched).

        ``payload`` comes back as a dict from a JSONB column (or a JSON string
        if the driver returns text), so it is normalised to a dict here.
        """
        payload = row[7]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return AuditEvent(
            event_id=row[0],
            thread_id=row[1],
            tdb_code=row[2],
            hojin_bango=row[3],
            event_type=AuditEventType(row[4]),
            actor=row[5],
            created_at=row[6],
            payload=payload or {},
            data_version=row[8],
            thresholds_version=row[9],
            content_hash=row[10],
            prev_hash=row[11],
            signature=row[12] if len(row) > 12 and row[12] is not None else "",
        )
