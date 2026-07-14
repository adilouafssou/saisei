"""Tests for the mock data engine and the Aichi fixture."""

from __future__ import annotations

from app.backend.tools.provider import MockDataProvider
from app.backend.tools.tdb_api import AntiSocialCheck

_TDB = "1234567"
_HOJIN = "1234567890123"


def test_credit_report_profile_identity() -> None:
    report = MockDataProvider().credit_report(_TDB)
    assert report.profile.tdb_code == _TDB
    assert report.profile.hojin_bango == _HOJIN
    assert len(report.profile.tdb_code) == 7
    assert len(report.profile.hojin_bango) == 13
    assert report.anti_social_check is AntiSocialCheck.CLEAR
    assert 1 <= report.tdb_score <= 100


def test_shisanhyo_is_ordered_and_full_year() -> None:
    rows = MockDataProvider().shisanhyo(_HOJIN)
    assert len(rows) == 12
    periods = [tb.period for tb in rows]
    assert periods == sorted(periods)


def test_aichi_year_end_is_loss_making() -> None:
    rows = MockDataProvider().shisanhyo(_HOJIN)
    assert rows[-1].keijo_rieki < 0  # genka koutou + failed kakaku tenka


def test_rate_curve_is_non_decreasing() -> None:
    curve = MockDataProvider().rate_curve()
    bps = [p.policy_rate_bps for p in curve]
    assert bps == sorted(bps)
    assert bps[-1] > bps[0]  # BOJ hikes


def test_settlement_metrics_under_stress() -> None:
    metrics = MockDataProvider().settlement_metrics()
    assert metrics.t_plus_1_liquidity_ratio < 1.0
    assert metrics.t_plus_2_liquidity_ratio < 1.0
    assert metrics.receivable_days > metrics.payable_days


def test_provider_is_deterministic() -> None:
    a = MockDataProvider().shisanhyo(_HOJIN)
    b = MockDataProvider().shisanhyo(_HOJIN)
    assert [tb.keijo_rieki for tb in a] == [tb.keijo_rieki for tb in b]
