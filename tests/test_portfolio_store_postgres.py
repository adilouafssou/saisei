"""Tests for the PostgresPortfolioStore (Feature 8.1, opt-in persistence).

Mirrors tests/test_audit_postgres.py's two layers:

- **Offline (always run):** the pure row-mapping round-trip
  (``_to_row`` -> ``_from_row``) preserves every field incl. a CJK name, and the
  schema bootstrap SQL declares the tenant-scoped composite key and is
  deliberately mutable (no append-only trigger). These need no DB.
- **Network-marked (skipped offline / in CI):** a real upsert/read/clear
  round-trip + tenant isolation, gated on ``SAISEI_PORTFOLIO_DSN`` — mirroring
  the audit-sink live-test posture.

Imports only from ``app.*`` + stdlib + pytest.
"""

from __future__ import annotations

import os

import pytest
from app.backend.portfolio.store import PortfolioSnapshot
from app.backend.portfolio.store_postgres import (
    PORTFOLIO_TABLE,
    SCHEMA_SQL,
    PostgresPortfolioStore,
)


def _snap(**overrides: object) -> PortfolioSnapshot:
    base: dict[str, object] = {
        "tenant_id": "bankA",
        "tdb_code": "1234567",
        "company_name": "製造アイチ株式会社",
        "ews": 62.5,
        "fsa_kanji": "要注意先",
        "ews_series": "70.00,62.50",
        "loan_status": "条件変更",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return PortfolioSnapshot(**base)  # type: ignore[arg-type]


class TestRowMappingRoundTrip:
    """_to_row -> _from_row is loss-less (offline; no DB)."""

    def test_round_trip_preserves_all_fields(self) -> None:
        snap = _snap()
        row_dict = PostgresPortfolioStore._to_row(snap)
        # Build a SELECT-shaped tuple in the documented column order.
        row = (
            row_dict["tenant_id"],
            row_dict["tdb_code"],
            row_dict["company_name"],
            row_dict["ews"],
            row_dict["fsa_kanji"],
            row_dict["ews_series"],
            row_dict["loan_status"],
            row_dict["updated_at"],
        )
        rebuilt = PostgresPortfolioStore._from_row(row)
        assert rebuilt == snap

    def test_round_trip_preserves_cjk_name(self) -> None:
        snap = _snap(company_name="京都精密製作所")
        row_dict = PostgresPortfolioStore._to_row(snap)
        rebuilt = PostgresPortfolioStore._from_row(
            (
                row_dict["tenant_id"],
                row_dict["tdb_code"],
                row_dict["company_name"],
                row_dict["ews"],
                row_dict["fsa_kanji"],
                row_dict["ews_series"],
                row_dict["loan_status"],
                row_dict["updated_at"],
            )
        )
        assert rebuilt.company_name == "京都精密製作所"

    def test_round_trip_preserves_loan_status(self) -> None:
        """The new loan_status field survives the row round-trip."""
        snap = _snap(loan_status="実行")
        row_dict = PostgresPortfolioStore._to_row(snap)
        rebuilt = PostgresPortfolioStore._from_row(
            (
                row_dict["tenant_id"],
                row_dict["tdb_code"],
                row_dict["company_name"],
                row_dict["ews"],
                row_dict["fsa_kanji"],
                row_dict["ews_series"],
                row_dict["loan_status"],
                row_dict["updated_at"],
            )
        )
        assert rebuilt.loan_status == "実行"

    def test_ews_coerced_to_float(self) -> None:
        """An int EWS round-trips as a float (DOUBLE PRECISION column)."""
        row = PostgresPortfolioStore._from_row(("bankA", "1", "co", 60, "", "", "", ""))
        assert isinstance(row.ews, float)
        assert row.ews == 60.0


class TestSchemaSql:
    """The bootstrap SQL is tenant-scoped and intentionally mutable."""

    def test_table_and_composite_key_declared(self) -> None:
        assert PORTFOLIO_TABLE in SCHEMA_SQL
        assert "PRIMARY KEY (tenant_id, tdb_code)" in SCHEMA_SQL

    def test_is_mutable_no_append_only_trigger(self) -> None:
        """Unlike the audit ledger, the watchlist must NOT be append-only."""
        assert "no_mutate" not in SCHEMA_SQL
        assert "BEFORE UPDATE OR DELETE" not in SCHEMA_SQL

    def test_idempotent_bootstrap(self) -> None:
        assert "CREATE TABLE IF NOT EXISTS" in SCHEMA_SQL
        assert "CREATE INDEX IF NOT EXISTS" in SCHEMA_SQL


# ---------------------------------------------------------------------------
# Live round-trip (network-marked; gated on SAISEI_PORTFOLIO_DSN).
# ---------------------------------------------------------------------------

_DSN = os.environ.get("SAISEI_PORTFOLIO_DSN", "")


@pytest.mark.network
@pytest.mark.skipif(not _DSN, reason="SAISEI_PORTFOLIO_DSN not set (offline)")
class TestLiveRoundTrip:
    """Real Postgres upsert/read/clear + tenant isolation (opt-in only)."""

    def test_upsert_read_replace_clear_and_isolation(self) -> None:
        store = PostgresPortfolioStore(_DSN)
        # Use throwaway tenant ids so the test never touches real books.
        ta, tb = "test-bankA", "test-bankB"
        store.clear(ta)
        store.clear(tb)
        try:
            store.upsert(_snap(tenant_id=ta, tdb_code="1111111", ews=40.0))
            store.upsert(_snap(tenant_id=ta, tdb_code="1111111", ews=80.0))  # replace
            store.upsert(_snap(tenant_id=tb, tdb_code="2222222", ews=55.0))

            rows_a = store.read(ta)
            assert [(r.tdb_code, r.ews) for r in rows_a] == [("1111111", 80.0)]
            rows_b = store.read(tb)
            assert [r.tdb_code for r in rows_b] == ["2222222"]
        finally:
            store.clear(ta)
            store.clear(tb)
        assert store.read(ta) == []
        assert store.read(tb) == []
