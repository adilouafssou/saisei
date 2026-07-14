"""Verifier for the Alembic migration setup (no live DB required).

The migration's whole value is that it owns the application schema WITHOUT
drifting from the in-code idempotent bootstrap each store still runs. The single
source of truth for that is: the initial revision executes the SAME ``SCHEMA_SQL``
/ DDL builders the store modules export. These offline checks pin exactly that,
plus the revision wiring, so the guarantee can't silently rot:

- the initial revision exists with the expected id and no down_revision (it is
  the baseline);
- its ``upgrade`` runs the audit / trajectory / portfolio ``SCHEMA_SQL`` and the
  pgvector table + HNSW builders verbatim (asserted by capturing op.execute);
- it ensures the pgvector extension before the vector-column table;
- ``downgrade`` drops exactly the objects ``upgrade`` created and never the
  shared ``vector`` extension or the LangGraph checkpointer tables;
- ``env.py`` resolves the DSN through the secret seam (no credential in config).

No database connection or Alembic runner is needed: we import the revision
module directly and capture the SQL it would execute.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from app.backend.audit.sink_postgres import SCHEMA_SQL as AUDIT_SCHEMA_SQL
from app.backend.portfolio.store_postgres import SCHEMA_SQL as PORTFOLIO_SCHEMA_SQL
from app.backend.trajectory.store_postgres import SCHEMA_SQL as TRAJECTORY_SCHEMA_SQL

_REVISION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "20240101_0001_initial_schema.py"
)


def _load_revision() -> ModuleType:
    """Import the initial revision module by path (it is not a package member)."""
    spec = importlib.util.spec_from_file_location("_initial_schema", _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CapturingOp:
    """Stand-in for ``alembic.op`` that records the SQL passed to ``execute``."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(sql)


#: A loaded revision module paired with the op-capturing stand-in.
_Revision = tuple[ModuleType, _CapturingOp]


@pytest.fixture
def revision(monkeypatch: pytest.MonkeyPatch) -> _Revision:
    """Load the revision with ``op`` patched to capture executed SQL."""
    module = _load_revision()
    captor = _CapturingOp()
    monkeypatch.setattr(module, "op", captor)
    return module, captor


def test_revision_is_the_baseline() -> None:
    """The initial revision has the expected id and is the chain root."""
    module = _load_revision()
    assert module.revision == "0001_initial_schema"
    assert module.down_revision is None


def test_upgrade_reuses_store_schema_sql(revision: _Revision) -> None:
    """upgrade() executes the store modules' SCHEMA_SQL verbatim (no drift)."""
    module, captor = revision
    module.upgrade()
    executed = "\n".join(captor.statements)
    assert AUDIT_SCHEMA_SQL in captor.statements
    assert TRAJECTORY_SCHEMA_SQL in captor.statements
    assert PORTFOLIO_SCHEMA_SQL in captor.statements
    # pgvector table + HNSW index for the configured memory table.
    assert "USING hnsw" in executed
    assert "vector(" in executed


def test_upgrade_enables_vector_extension_before_table(revision: _Revision) -> None:
    """The vector extension is ensured before the vector-column table is created."""
    module, captor = revision
    module.upgrade()
    ext_idx = next(i for i, s in enumerate(captor.statements) if "CREATE EXTENSION" in s)
    table_idx = next(i for i, s in enumerate(captor.statements) if "vector(" in s)
    assert ext_idx < table_idx


def test_downgrade_drops_created_objects_only(revision: _Revision) -> None:
    """downgrade() drops the four tables but never the shared vector extension."""
    module, captor = revision
    module.downgrade()
    executed = "\n".join(captor.statements)
    assert "DROP TABLE IF EXISTS saisei_audit_log" in executed
    assert "DROP TABLE IF EXISTS saisei_trajectory" in executed
    assert "DROP TABLE IF EXISTS saisei_portfolio_watchlist" in executed
    # Never drop the shared extension or LangGraph's checkpointer tables.
    assert "DROP EXTENSION" not in executed
    assert "checkpoint" not in executed.lower()


def test_env_resolves_dsn_through_secret_seam() -> None:
    """env.py imports resolve_secret, so the DSN flows through the secret seam."""
    env_src = (Path(__file__).resolve().parent.parent / "migrations" / "env.py").read_text(
        encoding="utf-8"
    )
    assert "from app.backend.secrets import resolve_secret" in env_src
    assert "resolve_secret(get_settings().postgres_dsn)" in env_src
