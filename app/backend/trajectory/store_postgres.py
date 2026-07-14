"""Postgres-backed append-only trajectory store (Feature 3, production backend).

The production :class:`~app.backend.trajectory.store.TrajectoryStore`: a single
append-only table in the existing Postgres instance (it may reuse the
checkpointer DB). It is the missing backend that makes ``SAISEI_TRAJECTORY_DSN``
actually persist — without it, ``get_trajectory_store`` falls back to the no-op
``NullTrajectoryStore`` and the data flywheel captures nothing.

Mirrors :class:`app.backend.audit.sink_postgres.PostgresAuditSink` exactly
(same staging, same defence-in-depth append-only contract):

1. **App layer** — this class issues only ``INSERT`` and ``SELECT``. The
   :class:`~app.backend.trajectory.store.TrajectoryStore` Protocol declares no
   update/delete, so the contract is structural.
2. **DB layer** — a ``BEFORE UPDATE OR DELETE`` trigger that ``RAISE``s, created
   idempotently at init, so even a direct SQL ``UPDATE``/``DELETE`` fails.

The whole sealed record (including the Feature 3.1 ``node_trajectory`` +
``interrupt_payload``) is stored loss-lessly as a JSONB ``record`` blob, so a
read reconstructs the exact frozen :class:`TrajectoryRecord` and its
``content_hash`` still validates. The query columns (``thread_id`` / ``tdb_code``
/ ``decision`` / ``created_at``) are denormalised for indexing.

``psycopg`` (psycopg3) is imported lazily inside the methods so this module
stays importable with no DB driver present; ``get_trajectory_store`` only
constructs this class when ``SAISEI_TRAJECTORY_DSN`` is set.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.backend.trajectory.record import TrajectoryRecord
from app.shared.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection as _PsycopgConnection

    Connection = _PsycopgConnection[Any]

__all__ = ["PostgresTrajectoryStore", "TRAJECTORY_TABLE", "SCHEMA_SQL"]

_log = get_logger(__name__)

#: The append-only trajectory table name.
TRAJECTORY_TABLE = "saisei_trajectory"

#: Idempotent schema bootstrap: table + indexes + append-only trigger.
#: The trigger is the DB-layer guarantee that a record is never mutated or
#: deleted after write — the same append-only contract as the audit ledger.
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TRAJECTORY_TABLE} (
    trajectory_id  TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    tdb_code       TEXT NOT NULL,
    decision       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    record         JSONB NOT NULL,
    seq            BIGSERIAL
);
CREATE INDEX IF NOT EXISTS idx_trajectory_thread ON {TRAJECTORY_TABLE} (thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_trajectory_tdb    ON {TRAJECTORY_TABLE} (tdb_code, created_at);

CREATE OR REPLACE FUNCTION saisei_trajectory_no_mutate()
    RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'saisei_trajectory is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'saisei_trajectory_no_mutate_trg'
    ) THEN
        CREATE TRIGGER saisei_trajectory_no_mutate_trg
            BEFORE UPDATE OR DELETE ON {TRAJECTORY_TABLE}
            FOR EACH ROW EXECUTE FUNCTION saisei_trajectory_no_mutate();
    END IF;
END;
$$;
"""

_INSERT_SQL = f"""
INSERT INTO {TRAJECTORY_TABLE} (
    trajectory_id, thread_id, tdb_code, decision, created_at, content_hash, record
) VALUES (
    %(trajectory_id)s, %(thread_id)s, %(tdb_code)s, %(decision)s, %(created_at)s,
    %(content_hash)s, %(record)s
)
ON CONFLICT (trajectory_id) DO NOTHING
"""

_SELECT_SQL = f"""
SELECT record
FROM {TRAJECTORY_TABLE}
WHERE thread_id = %(thread_id)s
ORDER BY seq ASC
"""


class PostgresTrajectoryStore:
    """Append-only Postgres trajectory store (production backend).

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

    def append(self, record: TrajectoryRecord) -> None:
        """Insert one sealed trajectory record (append-only).

        The record is already content-hashed by ``record_trajectory``; the store
        only persists it. ``ON CONFLICT DO NOTHING`` makes a duplicate
        ``trajectory_id`` a harmless no-op (idempotent retry), never an update.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, self._to_row(record))
            conn.commit()

    def read(self, thread_id: str) -> list[TrajectoryRecord]:
        """Return the records for a thread_id in write order (by ``seq``)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"thread_id": thread_id})
            rows = cur.fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _to_row(record: TrajectoryRecord) -> dict[str, Any]:
        """Map a record to INSERT params (the full record serialised to JSON).

        The query columns are denormalised from the record; the loss-less
        ``record`` JSONB blob is the canonical source rebuilt on read.
        """
        return {
            "trajectory_id": record.trajectory_id,
            "thread_id": record.thread_id,
            "tdb_code": record.tdb_code,
            "decision": record.decision.value,
            "created_at": record.created_at,
            "content_hash": record.content_hash,
            "record": json.dumps(record.model_dump(mode="json"), ensure_ascii=False),
        }

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> TrajectoryRecord:
        """Rebuild a frozen TrajectoryRecord from the JSONB ``record`` blob.

        The blob comes back as a dict from a JSONB column (or a JSON string if
        the driver returns text), so it is normalised to a dict before
        validation. The reconstructed record's ``content_hash`` still validates
        because the blob is the exact sealed record.
        """
        blob = row[0]
        if isinstance(blob, str):
            blob = json.loads(blob)
        return TrajectoryRecord.model_validate(blob)
