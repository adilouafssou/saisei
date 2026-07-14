"""Portfolio persistence package (Feature 8.1, opt-in / offline-safe).

The watchlist is ephemeral by default (an in-session view). This package is the
OPT-IN seam that lets a bank — after its own data-governance / FSA review — choose
to persist the book at rest for continuous monitoring across sessions. It is
modelled exactly on the audit-sink seam: a storage Protocol with an offline
Null default, so nothing is persisted unless a DSN is explicitly configured.
"""

from app.backend.portfolio.monitor import (
    CrossingAlert,
    RefreshItem,
    detect_crossings,
    plan_refresh,
)
from app.backend.portfolio.recorder import build_ews_series, record_snapshot
from app.backend.portfolio.store import (
    InMemoryPortfolioStore,
    NullPortfolioStore,
    PortfolioSnapshot,
    PortfolioStore,
    get_portfolio_store,
)

__all__ = [
    "PortfolioSnapshot",
    "PortfolioStore",
    "NullPortfolioStore",
    "InMemoryPortfolioStore",
    "get_portfolio_store",
    "record_snapshot",
    "build_ews_series",
    "RefreshItem",
    "CrossingAlert",
    "plan_refresh",
    "detect_crossings",
]
