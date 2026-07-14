"""Portfolio-store abstraction (Feature 8.1 — opt-in continuous monitoring).

The Portfolio watchlist is GOVERNANCE-LIGHT by default: an in-session view that
persists nothing at rest (see the ephemeral projection in the Reflex UI state).
This module is the OPT-IN storage seam that lets a bank choose — after its own
data-governance / FSA review — to persist the book so the watchlist survives
sessions and supports true continuous monitoring.

It deliberately mirrors the Feature 7 audit-sink seam
(``app/backend/audit/sink.py``):

- :class:`NullPortfolioStore` — the OFFLINE DEFAULT: no-op ``upsert``, empty
  ``read``. With no ``SAISEI_PORTFOLIO_DSN`` configured the system stores
  nothing at rest and behaves byte-identically to the ephemeral watchlist.
- :class:`InMemoryPortfolioStore` — for TESTS: a tenant-scoped current-state map.
- ``PostgresPortfolioStore`` — production: returned by the factory when
  ``SAISEI_PORTFOLIO_DSN`` is set (fails safe to Null if the driver is absent).

Unlike the audit ledger (append-only, immutable history), the watchlist is a
CURRENT-STATE book: each borrower has one latest snapshot per tenant, so the
interface is ``upsert`` (replace-by-key), not ``append``. Storage is
TENANT-SCOPED so one bank can never read another's book.

The Null + InMemory implementations are pure/offline (stdlib only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.shared.settings import Settings, get_settings

__all__ = [
    "PortfolioSnapshot",
    "PortfolioStore",
    "NullPortfolioStore",
    "InMemoryPortfolioStore",
    "get_portfolio_store",
]


@dataclass(frozen=True)
class PortfolioSnapshot:
    """One borrower's latest watchlist snapshot (display figures only).

    Carries only already-computed, bank-owned figures the watchlist shows — no
    new derivation, no secrets. Tenant-scoped so the store can isolate books.

    Attributes:
        tenant_id: The owning tenant (bank / branch) — the isolation key.
        tdb_code: The borrower's TDB code (per-tenant unique key).
        company_name: Display name.
        ews: Latest EWS score (higher = worse health).
        fsa_kanji: FSA classification label (display).
        ews_series: Comma-joined string of REAL computed EWS figures for the
            sparkline (deterministic; never a fabricated trend).
        loan_status: The facility's current loan-lifecycle status as a Japanese
            label (申込 / 審査中 / 承認 / 実行 / 正常 / 条件変更 / 管理回収 / ...),
            or '' when no facility is attached. This is what makes the watchlist
            a single book across the WHOLE lifecycle — a freshly-originated
            facility (実行) and a facility under turnaround (条件変更) sit in the
            same view, not two disconnected lists. Display only; derived from the
            already-recorded loan-event log, never a new judgement.
        updated_at: ISO-8601 timestamp string of this snapshot (display/order).
    """

    tenant_id: str
    tdb_code: str
    company_name: str = ""
    ews: float = 0.0
    fsa_kanji: str = ""
    ews_series: str = ""
    loan_status: str = ""
    updated_at: str = ""


@runtime_checkable
class PortfolioStore(Protocol):
    """Tenant-scoped current-state storage seam for the watchlist.

    There is deliberately no cross-tenant read: every method takes a
    ``tenant_id`` so a caller can only ever touch one bank's book.
    """

    def upsert(self, snapshot: PortfolioSnapshot) -> None:
        """Insert or replace a borrower's latest snapshot (best-effort)."""
        ...

    def read(self, tenant_id: str) -> list[PortfolioSnapshot]:
        """Return all current snapshots for a tenant (unordered)."""
        ...

    def clear(self, tenant_id: str) -> None:
        """Remove all snapshots for a tenant (banker-initiated wipe)."""
        ...


class NullPortfolioStore:
    """Offline default: a no-op store (nothing persisted at rest).

    ``upsert`` and ``clear`` discard; ``read`` is always empty. This is what
    keeps the watchlist ephemeral and the system byte-stable when no portfolio
    backend is configured — identical posture to the NullAuditSink.
    """

    def upsert(self, snapshot: PortfolioSnapshot) -> None:  # noqa: D102
        return None

    def read(self, tenant_id: str) -> list[PortfolioSnapshot]:  # noqa: D102
        return []

    def clear(self, tenant_id: str) -> None:  # noqa: D102
        return None


class InMemoryPortfolioStore:
    """In-memory tenant-scoped current-state store for tests.

    Stores the latest snapshot per (tenant_id, tdb_code). ``upsert`` replaces by
    that key; ``read`` returns copies so a caller cannot mutate the store through
    the returned references. Tenants are isolated: a read for one tenant never
    returns another's rows.
    """

    def __init__(self) -> None:
        self._by_tenant: dict[str, dict[str, PortfolioSnapshot]] = {}

    def upsert(self, snapshot: PortfolioSnapshot) -> None:  # noqa: D102
        book = self._by_tenant.setdefault(snapshot.tenant_id, {})
        book[snapshot.tdb_code] = snapshot

    def read(self, tenant_id: str) -> list[PortfolioSnapshot]:  # noqa: D102
        return list(self._by_tenant.get(tenant_id, {}).values())

    def clear(self, tenant_id: str) -> None:  # noqa: D102
        self._by_tenant.pop(tenant_id, None)


def get_portfolio_store(settings: Settings | None = None) -> PortfolioStore:
    """Return the configured portfolio store (Postgres when DSN set, else Null).

    Mirrors ``get_audit_sink``: with no ``portfolio_dsn`` configured this returns
    :class:`NullPortfolioStore`, so the watchlist stays ephemeral and the system
    offline-safe. Persistence is therefore an explicit, opt-in, bank-owned act,
    never the default. When a DSN is set, ``PostgresPortfolioStore`` is returned;
    if its optional driver is unavailable the factory fails safe to Null.

    Args:
        settings: Optional settings override (defaults to cached settings).

    Returns:
        A :class:`PortfolioStore` implementation.
    """
    settings = settings or get_settings()
    dsn = getattr(settings, "portfolio_dsn", "") or ""
    if not dsn:
        return NullPortfolioStore()
    # Fail safe to Null if the optional psycopg driver is unavailable, so an
    # unconfigured/driver-less environment never breaks the watchlist.
    try:
        from app.backend.portfolio.store_postgres import PostgresPortfolioStore
    except ImportError:
        return NullPortfolioStore()
    return PostgresPortfolioStore(dsn)
