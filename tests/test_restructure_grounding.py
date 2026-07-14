"""Tests for the deterministic restructure self-curing grounding (depth step 5).

The distress mirror of tests/test_debt_capacity.py. Proves the restructure
relief is hand-derivable arithmetic over the facility's OWN figures, that the
curing bands behave correctly across self_curing / marginal / non_curing, and
that the verdict reuses the SAME EWS recovery projector the recovery curve uses.

The relief arithmetic is pinned to the yen; the band classification is pinned
via the projector's recovery_month_index, which the pnl_recovery tests already
hand-verify, so these assertions stay deterministic and CI-independent.
"""

from __future__ import annotations

import datetime as dt

from app.backend.analysis.restructure_grounding import (
    RestructureRelief,
    assess_restructure,
    classify_restructure_curing,
    compute_restructure_relief,
    restructure_curing_reason,
)
from app.shared.constants import (
    DEBT_CAPACITY_AMORTIZATION_YEARS,
    MIN_RECOVERY_HORIZON_YEARS,
    MONTHS_PER_YEAR,
    RESTRUCTURE_FULL_GRACE_FRACTION,
)
from app.shared.models.accounting import TrialBalance

_HORIZON_MONTHS = MIN_RECOVERY_HORIZON_YEARS * MONTHS_PER_YEAR


def _declining_history() -> list[TrialBalance]:
    """A deteriorating 12-month history (mirrors the pnl_recovery fixture).

    Distressed enough that compute_ews_score returns a clearly sub-floor
    baseline an annual relief can pull back under 40.
    """
    rows: list[TrialBalance] = []
    for i in range(12):
        sales = 150_000_000 - i * 2_500_000
        cogs = int(sales * (0.80 + i * 0.005))
        sga = 20_000_000
        rows.append(
            TrialBalance(
                period=dt.date(2025, 1, 1) + dt.timedelta(days=30 * i),
                uriage=sales,
                uriage_genka=cogs,
                hanbaihi=sga,
            )
        )
    return rows


class TestComputeRestructureRelief:
    """The relief is hand-derivable arithmetic over the facility's own figures."""

    def test_grace_relief_defers_scheduled_annual_principal(self) -> None:
        # outstanding 500M, amortized over 5y -> 100M/yr scheduled principal;
        # full grace fraction (1.0) defers all of it.
        relief = compute_restructure_relief(
            outstanding=500_000_000, grace_months=12, rate_reduction_bps=0
        )
        expected = int(
            round(500_000_000 / DEBT_CAPACITY_AMORTIZATION_YEARS * RESTRUCTURE_FULL_GRACE_FRACTION)
        )
        assert relief.grace_relief == expected == 100_000_000
        assert relief.rate_relief == 0
        assert relief.annual_relief == 100_000_000

    def test_rate_relief_saves_the_cut_interest(self) -> None:
        # outstanding 500M, 200 bps cut -> 500M * 0.02 = 10,000,000 saved/yr.
        relief = compute_restructure_relief(
            outstanding=500_000_000, grace_months=0, rate_reduction_bps=200
        )
        assert relief.grace_relief == 0
        assert relief.rate_relief == 10_000_000
        assert relief.annual_relief == 10_000_000

    def test_both_levers_sum(self) -> None:
        relief = compute_restructure_relief(
            outstanding=500_000_000, grace_months=12, rate_reduction_bps=200
        )
        assert relief.annual_relief == relief.grace_relief + relief.rate_relief
        assert relief.annual_relief == 110_000_000

    def test_no_levers_is_zero(self) -> None:
        relief = compute_restructure_relief(
            outstanding=500_000_000, grace_months=0, rate_reduction_bps=0
        )
        assert relief == RestructureRelief(0, 0, 0)

    def test_non_positive_outstanding_is_zero(self) -> None:
        assert compute_restructure_relief(0, 12, 200) == RestructureRelief(0, 0, 0)
        assert compute_restructure_relief(-1, 12, 200) == RestructureRelief(0, 0, 0)

    def test_deterministic(self) -> None:
        a = compute_restructure_relief(500_000_000, 12, 200)
        b = compute_restructure_relief(500_000_000, 12, 200)
        assert a == b


class TestClassifyRestructureCuring:
    """The band boundaries are exact and hand-checkable."""

    def test_recovery_within_horizon_is_self_curing(self) -> None:
        assert classify_restructure_curing(_HORIZON_MONTHS, _HORIZON_MONTHS) == ("self_curing")
        assert classify_restructure_curing(1, _HORIZON_MONTHS) == "self_curing"

    def test_recovery_just_beyond_horizon_is_marginal(self) -> None:
        assert classify_restructure_curing(_HORIZON_MONTHS + 1, _HORIZON_MONTHS) == "marginal"

    def test_no_recovery_is_non_curing(self) -> None:
        assert classify_restructure_curing(None, _HORIZON_MONTHS) == "non_curing"


class TestAssessRestructure:
    """End-to-end: relief -> projection -> band, reusing the EWS projector."""

    def test_zero_relief_never_cures(self) -> None:
        result = assess_restructure(
            _declining_history(),
            outstanding=500_000_000,
            grace_months=0,
            rate_reduction_bps=0,
        )
        assert result.relief.annual_relief == 0
        assert result.recovery_month_index is None
        assert result.band == "non_curing"
        assert "治癒不能" in result.reason

    def test_insufficient_history_is_non_curing(self) -> None:
        one = [
            TrialBalance(
                period=dt.date(2025, 1, 31),
                uriage=100_000_000,
                uriage_genka=80_000_000,
                hanbaihi=10_000_000,
            )
        ]
        result = assess_restructure(
            one, outstanding=500_000_000, grace_months=12, rate_reduction_bps=200
        )
        assert result.recovery_month_index is None
        assert result.band == "non_curing"

    def test_large_relief_self_cures_within_horizon(self) -> None:
        # A large grace + rate cut produces a big annual relief that pulls the
        # distressed borrower back under the floor; the projector decides the
        # exact month, and it must land within the 5-year horizon.
        result = assess_restructure(
            _declining_history(),
            outstanding=2_000_000_000,
            grace_months=12,
            rate_reduction_bps=300,
        )
        assert result.relief.annual_relief > 0
        assert result.recovery_month_index is not None
        assert result.recovery_month_index <= _HORIZON_MONTHS
        assert result.band == "self_curing"
        assert "自己治癒" in result.reason

    def test_band_matches_recovery_month_vs_horizon(self) -> None:
        # The band is purely a function of the projector's recovery month vs the
        # horizon: re-derive it independently and assert they agree.
        result = assess_restructure(
            _declining_history(),
            outstanding=2_000_000_000,
            grace_months=12,
            rate_reduction_bps=300,
        )
        expected = classify_restructure_curing(result.recovery_month_index, _HORIZON_MONTHS)
        assert result.band == expected

    def test_reason_names_each_relief_leg(self) -> None:
        result = assess_restructure(
            _declining_history(),
            outstanding=500_000_000,
            grace_months=12,
            rate_reduction_bps=200,
        )
        assert "元本猶予" in result.reason
        assert "金利軽減" in result.reason

    def test_deterministic(self) -> None:
        a = assess_restructure(_declining_history(), 800_000_000, 12, 200)
        b = assess_restructure(_declining_history(), 800_000_000, 12, 200)
        assert a == b


class TestRestructureCuringReason:
    """The reason string is deterministic display prose, decides nothing."""

    def test_non_curing_zero_relief_reason(self) -> None:
        reason = restructure_curing_reason("non_curing", RestructureRelief(0, 0, 0), None)
        assert "治癒不能" in reason
        assert "ゼロ" in reason

    def test_marginal_reason_names_the_month(self) -> None:
        relief = RestructureRelief(50_000_000, 10_000_000, 60_000_000)
        reason = restructure_curing_reason("marginal", relief, 72)
        assert "限界的" in reason
        assert "72" in reason
