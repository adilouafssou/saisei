"""Aggregating mock data provider for Saisei.

Exposes a single :class:`MockDataProvider` that fans out to the Core Banking,
TDB, and BOJ/macro mock clients. LangGraph nodes depend on this interface so
the mocks can later be swapped for live clients without graph changes.

This module is the canonical location under ``app.backend.tools.provider``.
The legacy path ``mocks.provider`` re-exports from here.
"""

from __future__ import annotations

from pathlib import Path

from app.backend.tools.boj_macro import EdinetMacroMockClient, RatePoint, SettlementMetrics
from app.backend.tools.core_banking import CoreBankingMockClient
from app.backend.tools.tdb_api import TdbCreditReport, TdbMockClient
from app.shared.models.accounting import TrialBalance

__all__ = ["MockDataProvider"]


class MockDataProvider:
    """Single entry point to all mocked external financial data sources."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self.tdb = TdbMockClient(fixtures_dir)
        self.core_banking = CoreBankingMockClient(fixtures_dir)
        self.macro = EdinetMacroMockClient()

    def credit_report(self, tdb_code: str) -> TdbCreditReport:
        """Return the TDB credit report for a 7-digit TDB code."""
        return self.tdb.get_credit_report(tdb_code)

    def shisanhyo(self, hojin_bango: str) -> list[TrialBalance]:
        """Return monthly J-GAAP trial balances for a 13-digit Hojin Bango."""
        return self.core_banking.get_monthly_shisanhyo(hojin_bango)

    def rate_curve(self) -> list[RatePoint]:
        """Return the BOJ policy-rate curve."""
        return self.macro.get_rate_curve()

    def settlement_metrics(self) -> SettlementMetrics:
        """Return T+1/T+2 settlement liquidity metrics."""
        return self.macro.get_settlement_metrics()
