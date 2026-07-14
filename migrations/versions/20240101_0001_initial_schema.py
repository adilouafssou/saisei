"""initial application schema (audit, trajectory, portfolio, pgvector memory)

Revision ID: 0001_initial_schema
Revises:
Create Date: 2024-01-01 00:00:00

Creates Saisei's application tables as an ordered, reviewable baseline. To
guarantee the migration and the in-code idempotent bootstrap can never drift,
this revision executes the SAME ``SCHEMA_SQL`` / DDL builders the store modules
export -- it does not re-spell the DDL. The store modules remain the single
source of truth for the schema.

Scope notes:
* The LangGraph checkpointer tables are owned by ``PostgresSaver.setup()`` and
  are intentionally NOT created here.
* ``CREATE EXTENSION IF NOT EXISTS vector`` is a safety net for non-container
  Postgres; in the container it is already enabled by
  ``scripts/init_pgvector.sql``. It is the only added DDL, required before the
  pgvector memory table's ``vector(dim)`` column.

All DDL is idempotent (``IF NOT EXISTS`` / ``CREATE OR REPLACE``), so applying
this revision on a database already bootstrapped in-code is safe and a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.backend.audit.sink_postgres import SCHEMA_SQL as AUDIT_SCHEMA_SQL
from app.backend.portfolio.store_postgres import SCHEMA_SQL as PORTFOLIO_SCHEMA_SQL
from app.backend.tools.retrieval_ingest import build_hnsw_index_sql, build_table_sql
from app.backend.trajectory.store_postgres import SCHEMA_SQL as TRAJECTORY_SCHEMA_SQL
from app.shared.settings import get_settings

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pgvector_memory_sql() -> tuple[str, str]:
    """Return (table DDL, HNSW index DDL) for the pgvector long-term memory table.

    Built from the SAME builders ingest uses, with the table name / dim / HNSW
    parameters from settings, so the migration produces the identical table the
    application reads from.
    """
    s = get_settings()
    table_sql = build_table_sql(s.pgvector_table, s.pgvector_embedding_dim)
    index_sql = build_hnsw_index_sql(
        s.pgvector_table,
        m=s.pgvector_hnsw_m,
        ef_construction=s.pgvector_hnsw_ef_construction,
    )
    return table_sql, index_sql


def upgrade() -> None:
    """Create the application schema (idempotent; reuses the store DDL)."""
    # Append-only ledgers (table + indexes + BEFORE UPDATE/DELETE trigger).
    op.execute(AUDIT_SCHEMA_SQL)
    op.execute(TRAJECTORY_SCHEMA_SQL)
    # Mutable, tenant-scoped current-state watchlist (no immutability trigger).
    op.execute(PORTFOLIO_SCHEMA_SQL)
    # pgvector long-term memory: ensure the extension, then the table + HNSW.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    table_sql, index_sql = _pgvector_memory_sql()
    op.execute(table_sql)
    op.execute(index_sql)


def downgrade() -> None:
    """Drop the application schema created by :func:`upgrade`.

    The append-only ledgers carry a ``BEFORE UPDATE OR DELETE`` trigger that
    blocks row mutation; ``DROP TABLE`` is DDL and is not blocked by that
    row-level trigger, so the drops succeed. ``CASCADE`` removes the triggers
    with the table; the trigger FUNCTIONS are then dropped explicitly.

    Pgvector memory and the watchlist are plain drops. The ``vector`` extension
    is intentionally NOT dropped (it may be shared) and neither are the
    LangGraph checkpointer tables (not owned here).
    """
    s = get_settings()
    op.execute(f"DROP TABLE IF EXISTS {s.pgvector_table}")
    op.execute("DROP TABLE IF EXISTS saisei_portfolio_watchlist")
    op.execute("DROP TABLE IF EXISTS saisei_trajectory CASCADE")
    op.execute("DROP FUNCTION IF EXISTS saisei_trajectory_no_mutate")
    op.execute("DROP TABLE IF EXISTS saisei_audit_log CASCADE")
    op.execute("DROP FUNCTION IF EXISTS saisei_audit_no_mutate")
