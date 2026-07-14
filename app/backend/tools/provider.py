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
from app.backend.tools.core_banking_client import CoreBankingClient
from app.backend.tools.edinet_client import EdinetMacroClient
from app.backend.tools.tdb_api import TdbCreditReport, TdbMockClient
from app.backend.tools.tdb_client import TdbClient
from app.shared.models.accounting import TrialBalance

__all__ = ["MockDataProvider"]


class MockDataProvider:
    """Single entry point to all external financial data sources.

    Each source is served through its live-or-mock client (TdbClient,
    CoreBankingClient, EdinetMacroClient). With no live config set (the default)
    every client is a pure pass-through to its deterministic mock, so behaviour
    is byte-identical to the original mocks and the golden-spine harness and
    offline tests are unaffected. Configuring a source activates its live path
    transparently -- no graph changes.
    """

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self.tdb = TdbMockClient(fixtures_dir)
        self.tdb_client = TdbClient(fixtures_dir=fixtures_dir)
        self.core_banking = CoreBankingMockClient(fixtures_dir)
        self.core_banking_client = CoreBankingClient(fixtures_dir=fixtures_dir)
        self.macro = EdinetMacroMockClient()
        self.macro_client = EdinetMacroClient()

    def credit_report(self, tdb_code: str) -> TdbCreditReport:
        """Return the TDB credit report for a 7-digit TDB code.

        Delegates to :class:`TdbClient` so a configured live TDB API is used
        transparently; with no key configured this is the deterministic mock.
        """
        return self.tdb_client.get_credit_report(tdb_code)

    def shisanhyo(self, hojin_bango: str) -> list[TrialBalance]:
        """Return monthly J-GAAP trial balances for a 13-digit Hojin Bango.

        Delegates to :class:`CoreBankingClient` (live when configured, else mock).
        """
        return self.core_banking_client.get_monthly_shisanhyo(hojin_bango)

    def rate_curve(self) -> list[RatePoint]:
        """Return the BOJ policy-rate curve (live when configured, else mock)."""
        return self.macro_client.get_rate_curve()

    def settlement_metrics(self) -> SettlementMetrics:
        """Return T+1/T+2 settlement liquidity metrics (bank-internal mock)."""
        return self.macro_client.get_settlement_metrics()
