"""Offline tests for the optional real data clients (BOJ rate + Hojin Bango).

These assert the DETERMINISTIC OFFLINE CONTRACT only: with no configuration the
clients return mock data / pure validation and never touch the network. The live
HTTP branches are config-gated and excluded from coverage (marked VERIFY in the
source); they are confirmed against the live services separately.
"""

from __future__ import annotations

from app.backend.tools.boj_macro import BojRateClient, EdinetMacroMockClient
from app.backend.tools.hojin_bango import (
    HojinBangoClient,
    hojin_bango_check_digit,
    is_valid_hojin_bango,
)
from app.shared.settings import Settings

# ---------------------------------------------------------------------------
# BojRateClient offline behaviour
# ---------------------------------------------------------------------------


def test_boj_client_unconfigured_returns_mock_curve() -> None:
    """With no base URL/series id, the client returns the deterministic mock."""
    client = BojRateClient(settings=Settings(boj_api_base_url="", boj_api_series_id=""))
    assert client.live_enabled is False
    assert client.get_rate_curve() == EdinetMacroMockClient().get_rate_curve()


def test_boj_client_settlement_metrics_always_mock() -> None:
    """Settlement metrics are bank-internal: always the deterministic mock."""
    client = BojRateClient(settings=Settings())
    assert client.get_settlement_metrics() == EdinetMacroMockClient().get_settlement_metrics()


def test_boj_client_live_enabled_flag() -> None:
    """live_enabled is True only when both base URL and series id are set."""
    s = Settings(boj_api_base_url="https://example.test", boj_api_series_id="ABC")
    assert BojRateClient(settings=s).live_enabled is True


# ---------------------------------------------------------------------------
# Hojin Bango check-digit validation (pure, deterministic)
# ---------------------------------------------------------------------------


def test_check_digit_round_trip() -> None:
    """A number built with the computed check digit must validate."""
    base_12 = "234567890123"
    check = hojin_bango_check_digit(base_12)
    assert 0 <= check <= 9
    full = f"{check}{base_12}"
    assert is_valid_hojin_bango(full)


def test_invalid_length_and_nondigit_rejected() -> None:
    assert is_valid_hojin_bango("123") is False
    assert is_valid_hojin_bango("12345678901234") is False  # 14 digits
    assert is_valid_hojin_bango("abcdefghijklm") is False


def test_wrong_check_digit_rejected() -> None:
    """Flipping the leading check digit must fail validation."""
    base_12 = "234567890123"
    correct = hojin_bango_check_digit(base_12)
    wrong = (correct + 1) % 10
    assert is_valid_hojin_bango(f"{wrong}{base_12}") is False


# ---------------------------------------------------------------------------
# HojinBangoClient offline behaviour
# ---------------------------------------------------------------------------


def test_hojin_client_unconfigured_lookup_returns_none_for_valid_number() -> None:
    """Valid number, but no app id configured -> lookup returns None (no network)."""
    base_12 = "234567890123"
    full = f"{hojin_bango_check_digit(base_12)}{base_12}"
    client = HojinBangoClient(settings=Settings(hojin_bango_app_id=""))
    assert client.live_enabled is False
    assert client.validate(full) is True
    assert client.lookup(full) is None


def test_hojin_client_lookup_none_for_invalid_number() -> None:
    """Invalid check digit -> lookup short-circuits to None even if configured."""
    client = HojinBangoClient(settings=Settings(hojin_bango_app_id="some-id"))
    assert client.lookup("0000000000000") is None
