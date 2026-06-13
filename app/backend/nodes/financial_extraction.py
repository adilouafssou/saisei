"""Financial extraction node.

Merges the intake node (TDB identity + Shisanhyo) and the macro node
(BOJ rates + working-capital gap estimation) into a single blueprint file.

Public functions preserved for test compatibility:
- ``intake_node``: resolve corporate identity from TDB code.
- ``ews_node``: load Shisanhyo (re-exported from ews_scoring for graph wiring).
- ``macro_node``: load macro data and estimate working-capital gap.
- ``estimate_working_capital_gap``: pure function, testable in isolation.

The graph wires these as separate nodes; this file is the single source of truth
for both intake and macro logic.
"""

from __future__ import annotations

from typing import Any

from app.backend.state import SaiseiState
from app.backend.tools.boj_macro import RatePoint, SettlementMetrics
from app.backend.tools.provider import MockDataProvider
from app.backend.tools.tdb_api import AntiSocialCheck
from app.shared.logging import get_logger

__all__ = [
    "intake_node",
    "macro_node",
    "estimate_working_capital_gap",
]

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Intake node (TDB identity + Shisanhyo identity)
# ---------------------------------------------------------------------------


def intake_node(state: SaiseiState, provider: MockDataProvider | None = None) -> dict[str, Any]:
    """Resolve identity and load the TDB credit report.

    Args:
        state: Current graph state (requires ``tdb_code``).
        provider: Data provider; defaults to the mock engine.

    Returns:
        Partial state update with profile, score, identity, and any errors.
    """
    provider = provider or MockDataProvider()
    try:
        report = provider.credit_report(state.tdb_code)
    except KeyError:
        _log.warning("intake.unknown_tdb_code", tdb_code=state.tdb_code)
        return {"errors": [*state.errors, f"Unknown TDB code: {state.tdb_code}"]}

    errors = list(state.errors)
    if report.anti_social_check is AntiSocialCheck.FLAGGED:
        errors.append("Anti-social-forces check FLAGGED — escalate; no turnaround support.")

    _log.info(
        "intake.resolved",
        tdb_code=state.tdb_code,
        hojin_bango=report.profile.hojin_bango,
        tdb_score=report.tdb_score,
        anti_social=report.anti_social_check.value,
    )

    return {
        "hojin_bango": report.profile.hojin_bango,
        "company_profile": report.profile,
        "tdb_score": report.tdb_score,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Macro node (BOJ rates + working-capital gap)
# ---------------------------------------------------------------------------


def estimate_working_capital_gap(
    monthly_sales: int,
    monthly_cogs: int,
    metrics: SettlementMetrics,
    rate_curve: list[RatePoint],
) -> int:
    """Estimate the working-capital gap (Shikin Kuri) in integer yen.

    The cash-conversion cycle (receivable_days - payable_days) is scaled by the
    daily operating cash burn and stressed by the latest BOJ policy rate. A
    negative result indicates a funding deficit.

    Args:
        monthly_sales: Latest monthly sales (Uriage), JPY.
        monthly_cogs: Latest monthly COGS (Uriage Genka), JPY.
        metrics: Settlement / liquidity metrics.
        rate_curve: BOJ policy-rate curve (latest point used for stress).

    Returns:
        Estimated working-capital gap in integer yen (negative = deficit).
    """
    cash_cycle_days = metrics.receivable_days - metrics.payable_days
    daily_burn = monthly_cogs / 30.0
    base_gap = cash_cycle_days * daily_burn

    latest_bps = rate_curve[-1].policy_rate_bps if rate_curve else 0
    rate_stress = 1.0 + (latest_bps / 10_000.0)

    # Monthly operating margin cushions the gap; a thin margin cannot.
    monthly_margin = monthly_sales - monthly_cogs

    gap = monthly_margin - (base_gap * rate_stress)
    return int(round(gap))


def macro_node(state: SaiseiState, provider: MockDataProvider | None = None) -> dict[str, Any]:
    """Load macro/settlement data and estimate the working-capital gap.

    Args:
        state: Current graph state (uses the latest Shisanhyo row).
        provider: Data provider; defaults to the mock engine.

    Returns:
        Partial state update with rate curve, settlement metrics, and gap.
    """
    provider = provider or MockDataProvider()
    rate_curve = provider.rate_curve()
    metrics = provider.settlement_metrics()

    if not state.shisanhyo:
        _log.warning("macro.no_shisanhyo")
        return {
            "boj_rate_curve": rate_curve,
            "settlement_metrics": metrics,
            "working_capital_gap": None,
        }

    latest = state.shisanhyo[-1]
    gap = estimate_working_capital_gap(
        monthly_sales=int(latest.uriage),
        monthly_cogs=int(latest.uriage_genka),
        metrics=metrics,
        rate_curve=rate_curve,
    )
    _log.info(
        "macro.gap_estimated",
        working_capital_gap=gap,
        latest_rate_bps=rate_curve[-1].policy_rate_bps if rate_curve else 0,
    )
    return {
        "boj_rate_curve": rate_curve,
        "settlement_metrics": metrics,
        "working_capital_gap": gap,
    }
