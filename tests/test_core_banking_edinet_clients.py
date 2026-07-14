"""Offline tests for CoreBankingClient + EdinetMacroClient (Feature 2 slice 3).

Assert the DETERMINISTIC OFFLINE CONTRACT only: with no live config the clients
are pure pass-throughs to their deterministic mocks and never touch the network.
The boundary guard is tested directly. Live HTTP branches are config-gated and
excluded from coverage (marked VERIFY).
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.backend.tools.boj_macro import EdinetMacroMockClient
from app.backend.tools.core_banking import CoreBankingMockClient
from app.backend.tools.core_banking_client import (
    CoreBankingBoundaryError,
    CoreBankingClient,
    guard_shisanhyo,
)
from app.backend.tools.edinet_client import EdinetMacroClient
from app.shared.models.accounting import TrialBalance
from app.shared.settings import Settings

_KNOWN_HOJIN = "2000001000001"  # normal_service_co fixture


# ---------------------------------------------------------------------------
# CoreBankingClient offline behaviour
# ---------------------------------------------------------------------------


def test_core_banking_unconfigured_returns_mock() -> None:
    client = CoreBankingClient(settings=Settings(core_banking_base_url=""))
    assert client.live_enabled is False
    assert client.get_monthly_shisanhyo(
        _KNOWN_HOJIN
    ) == CoreBankingMockClient().get_monthly_shisanhyo(_KNOWN_HOJIN)


def test_core_banking_live_enabled_flag() -> None:
    s = Settings(core_banking_base_url="https://core.bank.internal")
    assert CoreBankingClient(settings=s).live_enabled is True
    assert CoreBankingClient(settings=Settings()).live_enabled is False


# ---------------------------------------------------------------------------
# guard_shisanhyo (pure, deterministic)
# ---------------------------------------------------------------------------


def _row(uriage: int, genka: int) -> TrialBalance:
    return TrialBalance(
        period=dt.date(2025, 5, 31),
        uriage=uriage,
        uriage_genka=genka,
        hanbaihi=1_000_000,
    )


def test_guard_passes_valid_series() -> None:
    rows = [_row(100_000_000, 60_000_000)]
    assert guard_shisanhyo(rows) is rows


def test_guard_rejects_empty_series() -> None:
    with pytest.raises(CoreBankingBoundaryError):
        guard_shisanhyo([])


def test_guard_gross_profit_identity_holds_for_model() -> None:
    """The model computes uriage_sourieki, so the identity always holds; the
    guard accepts well-formed rows and rejects an empty series."""
    row = _row(80_000_000, 61_000_000)
    assert row.uriage_sourieki == 80_000_000 - 61_000_000
    assert guard_shisanhyo([row]) == [row]


# ---------------------------------------------------------------------------
# EdinetMacroClient offline behaviour
# ---------------------------------------------------------------------------


def test_edinet_unconfigured_returns_mock_curve() -> None:
    client = EdinetMacroClient(settings=Settings(edinet_base_url=""))
    assert client.live_enabled is False
    assert client.get_rate_curve() == EdinetMacroMockClient().get_rate_curve()


def test_edinet_settlement_metrics_always_mock() -> None:
    client = EdinetMacroClient(settings=Settings())
    assert client.get_settlement_metrics() == EdinetMacroMockClient().get_settlement_metrics()


def test_edinet_live_enabled_flag() -> None:
    s = Settings(edinet_base_url="https://edinet.test")
    assert EdinetMacroClient(settings=s).live_enabled is True
