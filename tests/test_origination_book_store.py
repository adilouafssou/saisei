"""Tests for the origination credit-signal book store (offline).

The verifier for the opt-in origination-book persistence seam, mirroring
tests/test_loan_store.py / the watchlist store tests. All offline (no DB):

- NullOriginationBookStore: the no-op default contract (nothing at rest).
- InMemoryOriginationBookStore: tenant-scoped current-state semantics (upsert,
  replace-by-key / latest-wins, tenant isolation, clear).
- get_origination_book_store: factory selection (Null when no DSN; reuses the
  watchlist's portfolio_dsn opt-in gate).
- PostgresOriginationBookStore: the _to_row -> _from_row round-trip and the
  schema-bootstrap shape, which need no live database.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.backend.portfolio.origination_store import (
    InMemoryOriginationBookStore,
    NullOriginationBookStore,
    OriginationBookSnapshot,
    get_origination_book_store,
)
from app.backend.portfolio.origination_store_postgres import (
    ORIGINATION_BOOK_TABLE,
    SCHEMA_SQL,
    PostgresOriginationBookStore,
)


def _snap(
    tdb_code: str,
    *,
    tenant_id: str = "bank-a",
    capacity_band: str = "within_capacity",
    coverage_band: str = "well_covered",
    recommendation: str = "approve",
    company: str = "",
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> OriginationBookSnapshot:
    return OriginationBookSnapshot(
        tenant_id=tenant_id,
        tdb_code=tdb_code,
        company=company or f"Co {tdb_code}",
        recommendation=recommendation,
        capacity_band=capacity_band,
        coverage_band=coverage_band,
        updated_at=updated_at,
    )


class TestNullStore:
    """The offline default stores nothing and reads empty."""

    def test_upsert_is_a_noop_and_read_is_empty(self) -> None:
        store = NullOriginationBookStore()
        store.upsert(_snap("1"))
        assert store.read("bank-a") == []

    def test_clear_is_a_noop(self) -> None:
        store = NullOriginationBookStore()
        store.clear("bank-a")  # must not raise
        assert store.read("bank-a") == []


class TestInMemoryStore:
    """Tenant-scoped current-state semantics for tests."""

    def test_upsert_then_read(self) -> None:
        store = InMemoryOriginationBookStore()
        store.upsert(_snap("1"))
        rows = store.read("bank-a")
        assert len(rows) == 1
        assert rows[0].tdb_code == "1"
        assert rows[0].capacity_band == "within_capacity"
        assert rows[0].coverage_band == "well_covered"

    def test_upsert_replaces_by_key_latest_wins(self) -> None:
        store = InMemoryOriginationBookStore()
        store.upsert(_snap("1", capacity_band="over_capacity", coverage_band="uncovered"))
        store.upsert(_snap("1", capacity_band="within_capacity", coverage_band="well_covered"))
        rows = store.read("bank-a")
        assert len(rows) == 1  # replaced, not appended
        assert rows[0].capacity_band == "within_capacity"
        assert rows[0].coverage_band == "well_covered"

    def test_tenants_are_isolated(self) -> None:
        store = InMemoryOriginationBookStore()
        store.upsert(_snap("1", tenant_id="bank-a"))
        store.upsert(_snap("2", tenant_id="bank-b"))
        a = {r.tdb_code for r in store.read("bank-a")}
        b = {r.tdb_code for r in store.read("bank-b")}
        assert a == {"1"}
        assert b == {"2"}

    def test_clear_removes_only_that_tenant(self) -> None:
        store = InMemoryOriginationBookStore()
        store.upsert(_snap("1", tenant_id="bank-a"))
        store.upsert(_snap("2", tenant_id="bank-b"))
        store.clear("bank-a")
        assert store.read("bank-a") == []
        assert {r.tdb_code for r in store.read("bank-b")} == {"2"}


class TestFactory:
    """get_origination_book_store selects Null when no DSN is configured."""

    @dataclass
    class _Settings:
        portfolio_dsn: str = ""

    def test_no_dsn_returns_null_store(self) -> None:
        store = get_origination_book_store(self._Settings(portfolio_dsn=""))  # type: ignore[arg-type]
        assert isinstance(store, NullOriginationBookStore)

    def test_reuses_the_portfolio_dsn_opt_in_gate(self) -> None:
        # An empty portfolio_dsn (the watchlist's gate) keeps the book ephemeral;
        # there is deliberately no separate origination DSN knob.
        store = get_origination_book_store(self._Settings(portfolio_dsn=""))  # type: ignore[arg-type]
        assert isinstance(store, NullOriginationBookStore)
        assert store.read("any") == []


class TestPostgresRowMapping:
    """The Postgres row round-trip + schema shape need no live DB."""

    def test_to_row_from_row_round_trip(self) -> None:
        snap = _snap(
            "7654321",
            tenant_id="bank-z",
            capacity_band="stretch",
            coverage_band="partial",
            recommendation="decline",
            company="Test KK",
            updated_at="2026-06-22T16:00:00+00:00",
        )
        row_params = PostgresOriginationBookStore._to_row(snap)
        # The SELECT column order the store reads back with.
        ordered = (
            row_params["tenant_id"],
            row_params["tdb_code"],
            row_params["company"],
            row_params["recommendation"],
            row_params["capacity_band"],
            row_params["coverage_band"],
            row_params["updated_at"],
        )
        rebuilt = PostgresOriginationBookStore._from_row(ordered)
        assert rebuilt == snap

    def test_schema_targets_the_book_table_with_composite_key(self) -> None:
        assert ORIGINATION_BOOK_TABLE == "saisei_origination_book"
        assert f"CREATE TABLE IF NOT EXISTS {ORIGINATION_BOOK_TABLE}" in SCHEMA_SQL
        assert "PRIMARY KEY (tenant_id, tdb_code)" in SCHEMA_SQL

    def test_schema_has_no_append_only_trigger(self) -> None:
        # A current-state book is mutable by design (upsert + banker wipe),
        # unlike the append-only audit ledger.
        assert "TRIGGER" not in SCHEMA_SQL.upper()
