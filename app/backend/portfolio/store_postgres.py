"""Postgres-backed Portfolio store (Feature 8.1 — opt-in continuous monitoring).

The production :class:`~app.backend.portfolio.store.PortfolioStore`: a single
tenant-scoped, current-state table in the existing Postgres instance (it may
reuse the checkpointer DB). It is constructed ONLY when ``SAISEI_PORTFOLIO_DSN``
is set; with no DSN the factory returns the offline ``NullPortfolioStore`` and
nothing is stored at rest.

Modelled on ``app/backend/audit/sink_postgres.py`` but with the watchlist's
different shape:

- It is a CURRENT-STATE book, not an append-only ledger, so the write is an
  ``upsert`` (``INSERT ... ON CONFLICT (tenant_id, tdb_code) DO UPDATE``) and the
  table is intentionally mutable — there is NO immutability trigger (unlike the
  audit log). A banker wipe (``clear``) is a real ``DELETE``, which is the
  data-deletion capability a bank's governance review requires.
- Every statement is TENANT-SCOPED (the composite primary key is
  ``(tenant_id, tdb_code)`` and reads/clears filter by ``tenant_id``), so one
  bank can never read or delete another's book.

``psycopg`` (psycopg3, the same driver the LangGraph ``PostgresSaver`` and the
audit sink use) is imported lazily so this module stays importable with no DB
driver present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.backend.portfolio.store import PortfolioSnapshot
from app.shared.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection as _PsycopgConnection

    Connection = _PsycopgConnection[Any]

__all__ = ["PostgresPortfolioStore", "PORTFOLIO_TABLE", "SCHEMA_SQL"]

_log = get_logger(__name__)

#: The tenant-scoped current-state watchlist table name.
PORTFOLIO_TABLE = "saisei_portfolio_watchlist"

#: Idempotent schema bootstrap: one row per (tenant_id, tdb_code). Mutable by
#: design (upsert + banker wipe) — deliberately NO append-only trigger, unlike
#: the audit ledger.
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {PORTFOLIO_TABLE} (
    tenant_id     TEXT NOT NULL,
    tdb_code      TEXT NOT NULL,
    company_name  TEXT NOT NULL DEFAULT '',
    ews           DOUBLE PRECISION NOT NULL DEFAULT 0,
    fsa_kanji     TEXT NOT NULL DEFAULT '',
    ews_series    TEXT NOT NULL DEFAULT '',
    loan_status   TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, tdb_code)
);
CREATE INDEX IF NOT EXISTS idx_portfolio_tenant
    ON {PORTFOLIO_TABLE} (tenant_id, ews DESC);
-- Idempotent column add so an existing watchlist table (created before the
-- loan-lifecycle slice) gains loan_status without a manual migration.
ALTER TABLE {PORTFOLIO_TABLE}
    ADD COLUMN IF NOT EXISTS loan_status TEXT NOT NULL DEFAULT '';
"""

_UPSERT_SQL = f"""
INSERT INTO {PORTFOLIO_TABLE} (
    tenant_id, tdb_code, company_name, ews, fsa_kanji, ews_series, loan_status,
    updated_at
) VALUES (
    %(tenant_id)s, %(tdb_code)s, %(company_name)s, %(ews)s, %(fsa_kanji)s,
    %(ews_series)s, %(loan_status)s, %(updated_at)s
)
ON CONFLICT (tenant_id, tdb_code) DO UPDATE SET
    company_name = EXCLUDED.company_name,
    ews          = EXCLUDED.ews,
    fsa_kanji    = EXCLUDED.fsa_kanji,
    ews_series   = EXCLUDED.ews_series,
    loan_status  = EXCLUDED.loan_status,
    updated_at   = EXCLUDED.updated_at
"""

_SELECT_SQL = f"""
SELECT tenant_id, tdb_code, company_name, ews, fsa_kanji, ews_series,
       loan_status, updated_at
FROM {PORTFOLIO_TABLE}
WHERE tenant_id = %(tenant_id)s
ORDER BY ews DESC, tdb_code ASC
"""

_DELETE_SQL = f"DELETE FROM {PORTFOLIO_TABLE} WHERE tenant_id = %(tenant_id)s"


class PostgresPortfolioStore:
    """Tenant-scoped current-state Postgres watchlist store (production).

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
        """Create the table + index idempotently."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def upsert(self, snapshot: PortfolioSnapshot) -> None:
        """Insert or replace a borrower's latest snapshot (current-state book)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_UPSERT_SQL, self._to_row(snapshot))
            conn.commit()

    def read(self, tenant_id: str) -> list[PortfolioSnapshot]:
        """Return all current snapshots for a tenant (worst-EWS first)."""
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
    def _to_row(snapshot: PortfolioSnapshot) -> dict[str, Any]:
        """Map a snapshot to UPSERT params."""
        return {
            "tenant_id": snapshot.tenant_id,
            "tdb_code": snapshot.tdb_code,
            "company_name": snapshot.company_name,
            "ews": float(snapshot.ews),
            "fsa_kanji": snapshot.fsa_kanji,
            "ews_series": snapshot.ews_series,
            "loan_status": snapshot.loan_status,
            "updated_at": snapshot.updated_at,
        }

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> PortfolioSnapshot:
        """Rebuild a PortfolioSnapshot from a SELECT row (column order matched)."""
        return PortfolioSnapshot(
            tenant_id=row[0],
            tdb_code=row[1],
            company_name=row[2],
            ews=float(row[3]),
            fsa_kanji=row[4],
            ews_series=row[5],
            loan_status=row[6],
            updated_at=row[7],
        )
