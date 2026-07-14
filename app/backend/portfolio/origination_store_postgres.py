"""Postgres-backed origination book store (opt-in; twin of store_postgres.py).

The production
:class:`~app.backend.portfolio.origination_store.OriginationBookStore`: a single
tenant-scoped, current-state table in the existing Postgres instance (it may
reuse the checkpointer DB). It is constructed ONLY when the Portfolio DSN
(``SAISEI_PORTFOLIO_DSN``) is set -- the SAME opt-in gate as the watchlist --
and with no DSN the factory returns the offline ``NullOriginationBookStore`` so
nothing is stored at rest.

Modelled on ``app/backend/portfolio/store_postgres.py`` with the origination
book's shape:

- It is a CURRENT-STATE book, not an append-only ledger, so the write is an
  ``upsert`` (``INSERT ... ON CONFLICT (tenant_id, tdb_code) DO UPDATE``) and the
  table is intentionally mutable -- there is NO immutability trigger (unlike the
  audit log). A banker wipe (``clear``) is a real ``DELETE``.
- Every statement is TENANT-SCOPED (the composite primary key is
  ``(tenant_id, tdb_code)`` and reads/clears filter by ``tenant_id``), so one
  bank can never read or delete another's book.

``psycopg`` (psycopg3, the same driver the LangGraph ``PostgresSaver``, the audit
sink, and the watchlist store use) is imported lazily so this module stays
importable with no DB driver present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.backend.portfolio.origination_store import OriginationBookSnapshot
from app.shared.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection as _PsycopgConnection

    Connection = _PsycopgConnection[Any]

__all__ = ["PostgresOriginationBookStore", "ORIGINATION_BOOK_TABLE", "SCHEMA_SQL"]

_log = get_logger(__name__)

#: The tenant-scoped current-state origination-book table name.
ORIGINATION_BOOK_TABLE = "saisei_origination_book"

#: Idempotent schema bootstrap: one row per (tenant_id, tdb_code). Mutable by
#: design (upsert + banker wipe) -- deliberately NO append-only trigger, unlike
#: the audit ledger.
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {ORIGINATION_BOOK_TABLE} (
    tenant_id       TEXT NOT NULL,
    tdb_code        TEXT NOT NULL,
    company         TEXT NOT NULL DEFAULT '',
    recommendation  TEXT NOT NULL DEFAULT '',
    capacity_band   TEXT NOT NULL DEFAULT '',
    coverage_band   TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, tdb_code)
);
CREATE INDEX IF NOT EXISTS idx_origination_book_tenant
    ON {ORIGINATION_BOOK_TABLE} (tenant_id, updated_at DESC);
"""

_UPSERT_SQL = f"""
INSERT INTO {ORIGINATION_BOOK_TABLE} (
    tenant_id, tdb_code, company, recommendation, capacity_band, coverage_band,
    updated_at
) VALUES (
    %(tenant_id)s, %(tdb_code)s, %(company)s, %(recommendation)s,
    %(capacity_band)s, %(coverage_band)s, %(updated_at)s
)
ON CONFLICT (tenant_id, tdb_code) DO UPDATE SET
    company        = EXCLUDED.company,
    recommendation = EXCLUDED.recommendation,
    capacity_band  = EXCLUDED.capacity_band,
    coverage_band  = EXCLUDED.coverage_band,
    updated_at     = EXCLUDED.updated_at
"""

_SELECT_SQL = f"""
SELECT tenant_id, tdb_code, company, recommendation, capacity_band,
       coverage_band, updated_at
FROM {ORIGINATION_BOOK_TABLE}
WHERE tenant_id = %(tenant_id)s
ORDER BY updated_at DESC, tdb_code ASC
"""

_DELETE_SQL = f"DELETE FROM {ORIGINATION_BOOK_TABLE} WHERE tenant_id = %(tenant_id)s"


class PostgresOriginationBookStore:
    """Tenant-scoped current-state Postgres origination-book store (production).

    Args:
        dsn: A plain libpq PostgreSQL DSN (``postgresql://...``), typically the
            same instance as the checkpointer / watchlist. The schema is created
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
        """Create the table + index idempotently."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def upsert(self, snapshot: OriginationBookSnapshot) -> None:
        """Insert or replace a facility's latest snapshot (current-state book)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_UPSERT_SQL, self._to_row(snapshot))
            conn.commit()

    def read(self, tenant_id: str) -> list[OriginationBookSnapshot]:
        """Return all current snapshots for a tenant (most-recent first)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"tenant_id": tenant_id})
            rows = cur.fetchall()
        return [self._from_row(row) for row in rows]

    def clear(self, tenant_id: str) -> None:
        """Delete all snapshots for a tenant (banker-initiated wipe)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_DELETE_SQL, {"tenant_id": tenant_id})
            conn.commit()

    @staticmethod
    def _to_row(snapshot: OriginationBookSnapshot) -> dict[str, Any]:
        """Map a snapshot to UPSERT params."""
        return {
            "tenant_id": snapshot.tenant_id,
            "tdb_code": snapshot.tdb_code,
            "company": snapshot.company,
            "recommendation": snapshot.recommendation,
            "capacity_band": snapshot.capacity_band,
            "coverage_band": snapshot.coverage_band,
            "updated_at": snapshot.updated_at,
        }

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> OriginationBookSnapshot:
        """Rebuild an OriginationBookSnapshot from a SELECT row (order matched)."""
        return OriginationBookSnapshot(
            tenant_id=row[0],
            tdb_code=row[1],
            company=row[2],
            recommendation=row[3],
            capacity_band=row[4],
            coverage_band=row[5],
            updated_at=row[6],
        )
