"""Tests for the opt-in Portfolio store seam (Feature 8.1).

No CI here, so these pin the governance contract: persistence is OFF by default
(Null store — nothing stored at rest), the in-memory store is tenant-scoped and
upsert-by-borrower (current state, not append-only), and tenants are isolated.
Pure/offline — stdlib + the store module only.
"""

from __future__ import annotations

from app.backend.portfolio.store import (
    InMemoryPortfolioStore,
    NullPortfolioStore,
    PortfolioSnapshot,
    get_portfolio_store,
)
from app.shared.settings import Settings


def _snap(tenant: str, code: str, ews: float = 50.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        tenant_id=tenant,
        tdb_code=code,
        company_name=f"co-{code}",
        ews=ews,
        fsa_kanji="要注意先",
        ews_series=f"{ews:.2f}",
        updated_at="2026-01-01T00:00:00Z",
    )


class TestDefaultIsOff:
    def test_no_dsn_returns_null_store(self) -> None:
        """With no portfolio_dsn, the store is the no-op Null store (off default)."""
        store = get_portfolio_store(Settings(portfolio_dsn=""))
        assert isinstance(store, NullPortfolioStore)

    def test_null_store_persists_nothing(self) -> None:
        store = NullPortfolioStore()
        store.upsert(_snap("bankA", "1234567"))
        assert store.read("bankA") == []


class TestInMemoryStore:
    def test_upsert_then_read(self) -> None:
        store = InMemoryPortfolioStore()
        store.upsert(_snap("bankA", "1234567", ews=60.0))
        rows = store.read("bankA")
        assert len(rows) == 1
        assert rows[0].tdb_code == "1234567"
        assert rows[0].ews == 60.0

    def test_upsert_replaces_by_borrower(self) -> None:
        """A second snapshot for the same borrower replaces (current-state book)."""
        store = InMemoryPortfolioStore()
        store.upsert(_snap("bankA", "1234567", ews=40.0))
        store.upsert(_snap("bankA", "1234567", ews=75.0))
        rows = store.read("bankA")
        assert len(rows) == 1
        assert rows[0].ews == 75.0

    def test_tenants_are_isolated(self) -> None:
        store = InMemoryPortfolioStore()
        store.upsert(_snap("bankA", "1111111"))
        store.upsert(_snap("bankB", "2222222"))
        assert [r.tdb_code for r in store.read("bankA")] == ["1111111"]
        assert [r.tdb_code for r in store.read("bankB")] == ["2222222"]

    def test_clear_is_tenant_scoped(self) -> None:
        store = InMemoryPortfolioStore()
        store.upsert(_snap("bankA", "1111111"))
        store.upsert(_snap("bankB", "2222222"))
        store.clear("bankA")
        assert store.read("bankA") == []
        assert [r.tdb_code for r in store.read("bankB")] == ["2222222"]

    def test_read_returns_copies_not_internal_list(self) -> None:
        store = InMemoryPortfolioStore()
        store.upsert(_snap("bankA", "1111111"))
        rows = store.read("bankA")
        rows.clear()  # mutating the returned list must not affect the store
        assert len(store.read("bankA")) == 1

    def test_unknown_tenant_is_empty(self) -> None:
        assert InMemoryPortfolioStore().read("nobody") == []
