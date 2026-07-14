"""loan-lifecycle event ledger (append-only)

Revision ID: 0002_loan_events
Revises: 0001_initial_schema
Create Date: 2024-01-02 00:00:00

Adds the append-only loan-lifecycle event ledger
(``app/backend/portfolio/loan_store_postgres.py``). Like the baseline revision,
this executes the SAME ``SCHEMA_SQL`` the store module exports rather than
re-spelling the DDL, so the migration and the in-code idempotent bootstrap can
never drift — the store module stays the single source of truth.

The DDL is idempotent (``IF NOT EXISTS`` / ``CREATE OR REPLACE`` + an idempotent
trigger create), so applying this on a database already bootstrapped in-code is
a safe no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.backend.portfolio.loan_store_postgres import SCHEMA_SQL as LOAN_SCHEMA_SQL

# revision identifiers, used by Alembic.
revision: str = "0002_loan_events"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only loan-event ledger (idempotent; reuses store DDL)."""
    op.execute(LOAN_SCHEMA_SQL)


def downgrade() -> None:
    """Drop the loan-event ledger and its append-only trigger function.

    ``DROP TABLE`` is DDL and is not blocked by the row-level BEFORE
    UPDATE/DELETE trigger; ``CASCADE`` removes the trigger with the table, then
    the trigger function is dropped explicitly.
    """
    op.execute("DROP TABLE IF EXISTS saisei_loan_events CASCADE")
    op.execute("DROP FUNCTION IF EXISTS saisei_loan_events_no_mutate")
