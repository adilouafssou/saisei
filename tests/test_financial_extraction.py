"""Tests for the working-capital gap (Shikin Kuri / 資金繰り) estimator.

The gap must be dimensionally coherent: every term is yen over the same
cash-conversion-cycle horizon, so both the SIGN (deficit vs surplus) and the
MAGNITUDE are economically meaningful. These tests pin that contract so the
prior flow-minus-stock regression cannot return undetected.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.backend.nodes.financial_extraction import estimate_working_capital_gap
from app.backend.tools.boj_macro import RatePoint, SettlementMetrics


def _metrics(receivable_days: int = 95, payable_days: int = 45) -> SettlementMetrics:
    return SettlementMetrics(
        t_plus_1_liquidity_ratio=0.82,
        t_plus_2_liquidity_ratio=0.74,
        receivable_days=receivable_days,
        payable_days=payable_days,
    )


def _curve(bps: int = 60) -> list[RatePoint]:
    return [RatePoint(as_of=dt.date(2026, 3, 31), policy_rate_bps=bps)]


def test_deficit_sign_for_loss_making_firm() -> None:
    """A loss-making firm with a positive cash cycle has a funding deficit."""
    # eigyo_rieki negative (loss), 50-day positive cash cycle.
    gap = estimate_working_capital_gap(
        monthly_sales=122_000_000,
        monthly_cogs=129_000_000,
        metrics=_metrics(),
        rate_curve=_curve(),
        monthly_operating_profit=-30_500_000,
    )
    assert gap < 0


def test_magnitude_is_same_order_as_financing_requirement() -> None:
    """The gap magnitude must track the cash-cycle financing requirement.

    cash_cycle_days = 50, daily_cogs = 129M/30 ≈ 4.3M
    financing_requirement ≈ 215M (rate-stressed ≈ 216M). With a negative buffer,
    the deficit must be on the order of hundreds of millions, NOT billions and
    NOT a few million (which the old flow/stock formula could produce).
    """
    gap = estimate_working_capital_gap(
        monthly_sales=122_000_000,
        monthly_cogs=129_000_000,
        metrics=_metrics(),
        rate_curve=_curve(),
        monthly_operating_profit=-30_500_000,
    )
    # Sane band: between -400M and -150M for these inputs.
    assert -400_000_000 < gap < -150_000_000


def test_healthy_firm_with_short_cycle_is_not_in_deficit() -> None:
    """A profitable firm whose payables horizon covers receivables has surplus."""
    # Negative cash cycle (pays suppliers slower than it collects) + profit.
    gap = estimate_working_capital_gap(
        monthly_sales=140_000_000,
        monthly_cogs=100_000_000,
        metrics=_metrics(receivable_days=30, payable_days=60),
        rate_curve=_curve(bps=10),
        monthly_operating_profit=25_000_000,
    )
    assert gap > 0


def test_higher_rate_widens_the_deficit() -> None:
    """Rate stress increases the financing requirement, worsening the gap."""
    kwargs: dict[str, Any] = {
        "monthly_sales": 122_000_000,
        "monthly_cogs": 129_000_000,
        "metrics": _metrics(),
        "monthly_operating_profit": -30_500_000,
    }
    low = estimate_working_capital_gap(rate_curve=_curve(bps=10), **kwargs)
    high = estimate_working_capital_gap(rate_curve=_curve(bps=200), **kwargs)
    assert high < low  # higher rate -> deeper deficit


def test_zero_buffer_default_is_conservative() -> None:
    """Omitting operating profit yields a pure (negative) financing requirement."""
    gap = estimate_working_capital_gap(
        monthly_sales=122_000_000,
        monthly_cogs=129_000_000,
        metrics=_metrics(),
        rate_curve=_curve(),
    )
    # buffer = 0 -> gap = -financing_requirement * rate_stress < 0
    assert gap < 0
