"""Origination credit-signal book store (opt-in; the origination twin of store.py).

The origination book is the per-facility record of the two ADVISORY origination
credit lenses -- debt-service capacity (返済余力) and collateral/guarantee coverage
(担保・保証) -- for every facility taken to the 稟議 gate. The Reflex UI keeps it
as an EPHEMERAL, session-scoped projection (``origination_book``); this module is
the OPT-IN storage seam that lets a bank persist that book so the credit-signal
roll-up survives sessions, exactly like the watchlist's Portfolio store.

It deliberately mirrors ``app/backend/portfolio/store.py`` (the watchlist seam),
which in turn mirrors the Feature 7 audit-sink seam:

- :class:`NullOriginationBookStore` -- the OFFLINE DEFAULT: no-op ``upsert``,
  empty ``read``. With no portfolio DSN configured nothing is stored at rest and
  the origination book behaves byte-identically to the ephemeral projection.
- :class:`InMemoryOriginationBookStore` -- for TESTS: a tenant-scoped
  current-state map.
- ``PostgresOriginationBookStore`` -- production: returned by the factory when a
  portfolio DSN is set (fails safe to Null if the driver is absent).

Like the watchlist (and unlike the append-only audit ledger), this is a
CURRENT-STATE book: each facility has one latest snapshot per tenant, so the
interface is ``upsert`` (replace-by-key), not ``append``. Storage is
TENANT-SCOPED so one bank can never read another's book.

Persistence reuses the SAME opt-in gate as the watchlist (``portfolio_dsn``) and
the SAME tenant seam (``current_tenant_id``): the origination book is part of the
one Portfolio persistence decision, not a separate governance knob.

The Null + InMemory implementations are pure/offline (stdlib only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.shared.settings import Settings, get_settings

__all__ = [
    "OriginationBookSnapshot",
    "OriginationBookStore",
    "NullOriginationBookStore",
    "InMemoryOriginationBookStore",
    "get_origination_book_store",
]


@dataclass(frozen=True)
class OriginationBookSnapshot:
    """One facility's latest origination credit-signal snapshot (display only).

    Carries only already-computed, bank-owned display strings the credit-signal
    roll-up shows -- the two advisory band keys and the recommendation -- no new
    derivation, no secrets. Tenant-scoped so the store can isolate books.

    Attributes:
        tenant_id: The owning tenant (bank / branch) -- the isolation key.
        tdb_code: The facility applicant's TDB code (per-tenant unique key).
        company: Display name (falls back to the code when unknown).
        recommendation: The 稟議 recommendation key ('approve' / 'decline').
        capacity_band: The debt-service-capacity band key (within_capacity /
            stretch / over_capacity), or '' when a DECLINE omits it.
        coverage_band: The collateral/guarantee coverage band key (well_covered
            / partial / uncovered), or '' when omitted.
        updated_at: ISO-8601 timestamp string of this snapshot (display/order).
    """

    tenant_id: str
    tdb_code: str
    company: str = ""
    recommendation: str = ""
    capacity_band: str = ""
    coverage_band: str = ""
    updated_at: str = ""


@runtime_checkable
class OriginationBookStore(Protocol):
    """Tenant-scoped current-state storage seam for the origination book.

    There is deliberately no cross-tenant read: every method takes a
    ``tenant_id`` so a caller can only ever touch one bank's book.
    """

    def upsert(self, snapshot: OriginationBookSnapshot) -> None:
        """Insert or replace a facility's latest snapshot (best-effort)."""
        ...

    def read(self, tenant_id: str) -> list[OriginationBookSnapshot]:
        """Return all current snapshots for a tenant (unordered)."""
        ...

    def clear(self, tenant_id: str) -> None:
        """Remove all snapshots for a tenant (banker-initiated wipe)."""
        ...


class NullOriginationBookStore:
    """Offline default: a no-op store (nothing persisted at rest).

    ``upsert`` and ``clear`` discard; ``read`` is always empty. This is what
    keeps the origination book ephemeral and the system byte-stable when no
    portfolio backend is configured -- identical posture to NullPortfolioStore.
    """

    def upsert(self, snapshot: OriginationBookSnapshot) -> None:  # noqa: D102
        return None

    def read(self, tenant_id: str) -> list[OriginationBookSnapshot]:  # noqa: D102
        return []

    def clear(self, tenant_id: str) -> None:  # noqa: D102
        return None


class InMemoryOriginationBookStore:
    """In-memory tenant-scoped current-state store for tests.

    Stores the latest snapshot per (tenant_id, tdb_code). ``upsert`` replaces by
    that key; ``read`` returns copies-by-value (frozen dataclasses) so a caller
    cannot mutate the store through the returned references. Tenants are
    isolated: a read for one tenant never returns another's rows.
    """

    def __init__(self) -> None:
        self._by_tenant: dict[str, dict[str, OriginationBookSnapshot]] = {}

    def upsert(self, snapshot: OriginationBookSnapshot) -> None:  # noqa: D102
        book = self._by_tenant.setdefault(snapshot.tenant_id, {})
        book[snapshot.tdb_code] = snapshot

    def read(self, tenant_id: str) -> list[OriginationBookSnapshot]:  # noqa: D102
        return list(self._by_tenant.get(tenant_id, {}).values())

    def clear(self, tenant_id: str) -> None:  # noqa: D102
        self._by_tenant.pop(tenant_id, None)


def get_origination_book_store(
    settings: Settings | None = None,
) -> OriginationBookStore:
    """Return the configured origination book store (Postgres when DSN set, else Null).

    Mirrors ``get_portfolio_store`` and reuses the SAME opt-in gate
    (``portfolio_dsn``): the origination book is part of the one Portfolio
    persistence decision. With no DSN configured this returns
    :class:`NullOriginationBookStore`, so the book stays ephemeral and the system
    offline-safe -- persistence is an explicit, opt-in, bank-owned act, never the
    default. When a DSN is set, ``PostgresOriginationBookStore`` is returned; if
    its optional driver is unavailable the factory fails safe to Null.

    Args:
        settings: Optional settings override (defaults to cached settings).

    Returns:
        An :class:`OriginationBookStore` implementation.
    """
    settings = settings or get_settings()
    dsn = getattr(settings, "portfolio_dsn", "") or ""
    if not dsn:
        return NullOriginationBookStore()
    # Fail safe to Null if the optional psycopg driver is unavailable, so an
    # unconfigured/driver-less environment never breaks the origination book.
    try:
        from app.backend.portfolio.origination_store_postgres import (
            PostgresOriginationBookStore,
        )
    except ImportError:
        return NullOriginationBookStore()
    return PostgresOriginationBookStore(dsn)
