"""Offline tests for the live TDB client (Feature 2, slice 1).

Assert the DETERMINISTIC OFFLINE CONTRACT only: with no API key the client is a
pure pass-through to the deterministic TDB mock and never touches the network.
The boundary guard is tested directly with constructed reports. The live HTTP
branch is config-gated and excluded from coverage (marked VERIFY in the source);
it is confirmed against the live service separately.
"""

from __future__ import annotations

import pytest
from app.backend.tools.tdb_api import (
    AntiSocialCheck,
    CompanyProfile,
    TdbCreditReport,
    TdbMockClient,
)
from app.backend.tools.tdb_client import (
    TdbBoundaryError,
    TdbClient,
    guard_credit_report,
)
from app.shared.settings import Settings

_KNOWN_CODE = "2000001"  # normal_service_co fixture


# ---------------------------------------------------------------------------
# TdbClient offline behaviour
# ---------------------------------------------------------------------------


def test_client_unconfigured_returns_mock_report() -> None:
    """With no API key, the client returns the deterministic mock report."""
    client = TdbClient(settings=Settings(tdb_api_key=""))
    assert client.live_enabled is False
    assert client.get_credit_report(_KNOWN_CODE) == TdbMockClient().get_credit_report(_KNOWN_CODE)


def test_client_live_enabled_flag() -> None:
    """live_enabled is True only when a TDB API key is configured."""
    assert TdbClient(settings=Settings(tdb_api_key="secret")).live_enabled is True
    assert TdbClient(settings=Settings(tdb_api_key="")).live_enabled is False


def test_client_is_drop_in_for_mock() -> None:
    """The client exposes the same get_credit_report signature as the mock."""
    client = TdbClient(settings=Settings(tdb_api_key=""))
    report = client.get_credit_report(_KNOWN_CODE)
    assert isinstance(report, TdbCreditReport)
    assert report.tdb_code == _KNOWN_CODE


# ---------------------------------------------------------------------------
# Boundary guard (pure, deterministic)
# ---------------------------------------------------------------------------


def _report(tdb_code: str, profile_code: str | None = None) -> TdbCreditReport:
    """Build a minimal valid credit report for guard tests."""
    profile = CompanyProfile(
        tdb_code=profile_code or tdb_code,
        hojin_bango="1234567890123",
        name="Test KK",
        prefecture="Tokyo",
        industry="Service",
        established_year=2000,
        employees=10,
    )
    return TdbCreditReport(
        tdb_code=tdb_code,
        profile=profile,
        tdb_score=70,
        anti_social_check=AntiSocialCheck.CLEAR,
    )


def test_guard_passes_consistent_report() -> None:
    """A report whose codes all agree passes the guard unchanged."""
    report = _report("1234567")
    assert guard_credit_report("1234567", report) is report


def test_guard_rejects_top_level_code_mismatch() -> None:
    """A report for a different code than requested is rejected."""
    report = _report("7654321")
    with pytest.raises(TdbBoundaryError):
        guard_credit_report("1234567", report)


def test_guard_rejects_profile_code_mismatch() -> None:
    """A report whose embedded profile code disagrees is rejected."""
    report = _report("1234567", profile_code="7654321")
    with pytest.raises(TdbBoundaryError):
        guard_credit_report("1234567", report)
