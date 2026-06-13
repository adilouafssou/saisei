"""J-GAAP accounting models for Saisei.

Trial Balances (Shisanhyo / 試算表) use standard Japanese accounts. All monetary
fields are strict integer yen (see :mod:`app.shared.models.money`). The default
fiscal year ends on March 31 (Sangatsu Kessan / 3月決算).

This module is the canonical location under ``app.shared.models.accounting``.
The legacy path ``shared.domain.accounting`` re-exports from here.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.shared.models.money import JPY, format_jpy

__all__ = ["FISCAL_YEAR_END_MONTH", "TrialBalance", "fiscal_year_of"]

#: Japanese statutory fiscal year end month (March).
FISCAL_YEAR_END_MONTH = 3


def fiscal_year_of(period: dt.date) -> int:
    """Return the Japanese fiscal year (ending March 31) for a given date.

    A date in April 2025 .. March 2026 belongs to fiscal year 2025.

    Args:
        period: The calendar date.

    Returns:
        The fiscal year as the year in which the FY started.
    """
    if period.month <= FISCAL_YEAR_END_MONTH:
        return period.year - 1
    return period.year


class TrialBalance(BaseModel):
    """A monthly J-GAAP Trial Balance (Shisanhyo / 試算表).

    Standard accounts:
        * ``uriage`` (売上) — Sales
        * ``uriage_genka`` (売上原価) — COGS
        * ``hanbaihi`` (販売費) — SG&A

    Profit lines are derived. ``keijo_rieki`` (経常利益, Ordinary Profit) is
    computed from the above plus non-operating items.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    period: dt.date = Field(description="Month-end date for this trial balance.")
    uriage: JPY = Field(description="Sales / 売上 (JPY).")
    uriage_genka: JPY = Field(description="Cost of goods sold / 売上原価 (JPY).")
    hanbaihi: JPY = Field(description="Selling, general & admin expenses / 販売費 (JPY).")
    eigai_shueki: JPY = Field(
        default=0, description="Non-operating income / 営業外収益 (JPY)."
    )
    eigai_hiyo: JPY = Field(
        default=0, description="Non-operating expenses / 営業外費用 (JPY)."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uriage_sourieki(self) -> int:
        """Gross profit / 売上総利益 = Sales - COGS."""
        return int(self.uriage) - int(self.uriage_genka)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def eigyo_rieki(self) -> int:
        """Operating profit / 営業利益 = Gross profit - SG&A."""
        return self.uriage_sourieki - int(self.hanbaihi)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def keijo_rieki(self) -> int:
        """Ordinary profit / 経常利益 = Operating profit + non-op income - non-op expense."""
        return self.eigyo_rieki + int(self.eigai_shueki) - int(self.eigai_hiyo)

    @property
    def fiscal_year(self) -> int:
        """Japanese fiscal year (ending March 31) this period belongs to."""
        return fiscal_year_of(self.period)

    def summary(self) -> str:
        """Human-readable one-line summary with ¥-formatted figures."""
        return (
            f"{self.period.isoformat()} | 売上 {format_jpy(int(self.uriage))} | "
            f"経常利益 {format_jpy(self.keijo_rieki)}"
        )
