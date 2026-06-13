"""BOJ macro / settlement tool.

Returns a deterministic BOJ policy-rate curve and settlement liquidity metrics
(T+1 / T+2) used to assess the working-capital gap (Shikin Kuri / 資金繰り).

This module is the canonical location under ``app.backend.tools.boj_macro``.
The legacy path ``mocks.edinet_macro`` re-exports from here.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["RatePoint", "SettlementMetrics", "EdinetMacroMockClient"]


class RatePoint(BaseModel):
    """A single point on the BOJ policy-rate curve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: dt.date = Field(description="Observation date.")
    policy_rate_bps: int = Field(description="BOJ policy rate in basis points.")


class SettlementMetrics(BaseModel):
    """Settlement-cycle liquidity metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    t_plus_1_liquidity_ratio: float = Field(
        description="Liquidity coverage for T+1 settlement obligations."
    )
    t_plus_2_liquidity_ratio: float = Field(
        description="Liquidity coverage for T+2 settlement obligations."
    )
    receivable_days: int = Field(ge=0, description="Days sales outstanding / 売掛回収日数.")
    payable_days: int = Field(ge=0, description="Days payable outstanding / 買掛支払日数.")


class EdinetMacroMockClient:
    """Deterministic macro client.

    The rate curve reflects the recent BOJ exit from negative rates: a series of
    hikes that widen working-capital gaps for cost-pressured SMEs.
    """

    def get_rate_curve(self) -> list[RatePoint]:
        """Return the deterministic BOJ policy-rate curve (rising)."""
        base = dt.date(2025, 4, 30)
        bps = [10, 10, 25, 25, 25, 40, 40, 50, 50, 50, 60, 60]
        return [
            RatePoint(
                as_of=_month_end(base, i),
                policy_rate_bps=value,
            )
            for i, value in enumerate(bps)
        ]

    def get_settlement_metrics(self) -> SettlementMetrics:
        """Return deterministic settlement / liquidity metrics under rate stress."""
        return SettlementMetrics(
            t_plus_1_liquidity_ratio=0.82,
            t_plus_2_liquidity_ratio=0.74,
            receivable_days=95,
            payable_days=45,
        )


def _month_end(start: dt.date, months_ahead: int) -> dt.date:
    """Return the month-end date ``months_ahead`` months after ``start``."""
    month_index = start.month - 1 + months_ahead
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, month + 1, 1) - dt.timedelta(days=1)
