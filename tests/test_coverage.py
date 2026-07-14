"""Tests for the deterministic collateral / guarantee coverage check (breadth).

The breadth twin of tests/test_debt_capacity.py: proves the coverage amount,
ratio, and uncovered tail are pure, hand-derivable arithmetic over the pledged
collateral + guarantee and the proposed facility, and that the bands behave
correctly across well_covered / partial / uncovered.

Every expected figure below is hand-derived in the test docstring/comments so
each assertion is auditable to the yen. All tests are offline, deterministic,
and import only from ``app.*``.
"""

from __future__ import annotations

from app.backend.analysis.coverage import (
    assess_coverage,
    classify_coverage,
    coverage_reason,
    covered_amount,
)
from app.shared.constants import (
    COVERAGE_PARTIAL_FLOOR,
    COVERAGE_WELL_COVERED_FLOOR,
)


class TestCoveredAmount:
    """The covered amount sums the two legs, each floored at 0."""

    def test_sums_collateral_and_guarantee(self) -> None:
        assert covered_amount(60_000_000, 40_000_000) == 100_000_000

    def test_floors_each_leg_at_zero(self) -> None:
        # A negative / malformed leg can never reduce or inflate coverage.
        assert covered_amount(-5, 40_000_000) == 40_000_000
        assert covered_amount(60_000_000, -5) == 60_000_000

    def test_zero_legs_are_zero(self) -> None:
        assert covered_amount(0, 0) == 0


class TestClassifyCoverage:
    """The band boundaries are exact and hand-checkable."""

    def test_at_or_above_well_covered_floor(self) -> None:
        # ratio 1.0 (covered == facility) -> well_covered.
        band, ratio = classify_coverage(100_000_000, 100_000_000)
        assert band == "well_covered"
        assert ratio == COVERAGE_WELL_COVERED_FLOOR

    def test_over_collateralised_is_well_covered(self) -> None:
        band, ratio = classify_coverage(150_000_000, 100_000_000)
        assert band == "well_covered"
        assert ratio is not None and ratio > 1.0

    def test_at_partial_floor_is_partial(self) -> None:
        # ratio exactly 0.5 -> partial (>= partial floor, < well-covered floor).
        band, ratio = classify_coverage(50_000_000, 100_000_000)
        assert band == "partial"
        assert ratio == COVERAGE_PARTIAL_FLOOR

    def test_just_below_well_covered_is_partial(self) -> None:
        band, _ = classify_coverage(99_999_999, 100_000_000)
        assert band == "partial"

    def test_just_below_partial_floor_is_uncovered(self) -> None:
        # ratio just under 0.5 -> uncovered.
        band, _ = classify_coverage(49_999_999, 100_000_000)
        assert band == "uncovered"

    def test_no_coverage_is_uncovered(self) -> None:
        band, ratio = classify_coverage(0, 100_000_000)
        assert band == "uncovered"
        assert ratio == 0.0

    def test_zero_facility_is_well_covered_with_no_ratio(self) -> None:
        # A DECLINE carries a 0 ceiling -> no exposure -> trivially covered.
        band, ratio = classify_coverage(0, 0)
        assert band == "well_covered"
        assert ratio is None  # division undefined when facility is 0


class TestAssessCoverage:
    """End-to-end: the public entry point composes amounts + band + reason."""

    def test_well_covered_facility(self) -> None:
        # facility 100M, collateral 70M + guarantee 40M = 110M covered.
        # ratio 1.1 >= 1.0 -> well_covered; uncovered tail floored at 0.
        result = assess_coverage(100_000_000, 70_000_000, 40_000_000)
        assert result.covered_amount == 110_000_000
        assert result.uncovered_amount == 0
        assert result.band == "well_covered"
        assert result.ratio is not None and result.ratio > 1.0
        assert "保全十分" in result.reason

    def test_partial_facility(self) -> None:
        # facility 100M, collateral 50M + guarantee 10M = 60M covered.
        # ratio 0.6 -> partial; uncovered tail = 40M.
        result = assess_coverage(100_000_000, 50_000_000, 10_000_000)
        assert result.covered_amount == 60_000_000
        assert result.uncovered_amount == 40_000_000
        assert result.band == "partial"
        assert "一部保全" in result.reason
        assert "40,000,000" in result.reason  # the uncovered tail is named

    def test_uncovered_facility(self) -> None:
        # facility 100M, collateral 10M + guarantee 0 = 10M covered.
        # ratio 0.1 -> uncovered; uncovered tail = 90M.
        result = assess_coverage(100_000_000, 10_000_000, 0)
        assert result.covered_amount == 10_000_000
        assert result.uncovered_amount == 90_000_000
        assert result.band == "uncovered"
        assert "保全不足" in result.reason
        assert "90,000,000" in result.reason

    def test_no_coverage_data_bands_uncovered(self) -> None:
        # The prudent-banker default: unknown coverage -> 0 -> uncovered for any
        # positive facility (never assumed secured).
        result = assess_coverage(100_000_000, 0, 0)
        assert result.covered_amount == 0
        assert result.uncovered_amount == 100_000_000
        assert result.band == "uncovered"
        assert result.ratio == 0.0

    def test_zero_facility_is_well_covered(self) -> None:
        # A DECLINE recommendation carries a 0 ceiling -> no exposure to cover.
        result = assess_coverage(0, 0, 0)
        assert result.band == "well_covered"
        assert result.ratio is None
        assert "融資なし" in result.reason

    def test_negative_coverage_is_floored(self) -> None:
        # Malformed negative coverage cannot inflate or go below 0.
        result = assess_coverage(100_000_000, -50_000_000, -10_000_000)
        assert result.covered_amount == 0
        assert result.band == "uncovered"

    def test_reason_names_each_leg(self) -> None:
        result = assess_coverage(100_000_000, 50_000_000, 10_000_000)
        assert "担保" in result.reason
        assert "保証" in result.reason
        assert "カバー率" in result.reason

    def test_deterministic(self) -> None:
        a = assess_coverage(100_000_000, 50_000_000, 10_000_000)
        b = assess_coverage(100_000_000, 50_000_000, 10_000_000)
        assert a == b


class TestCoverageReason:
    """The reason string is deterministic display prose, decides nothing."""

    def test_no_facility_reason(self) -> None:
        reason = coverage_reason("well_covered", 0, 0, 0, 0, 0, None)
        assert "融資なし" in reason

    def test_uncovered_reason_names_the_clean_risk_tail(self) -> None:
        reason = coverage_reason(
            "uncovered", 100_000_000, 10_000_000, 0, 10_000_000, 90_000_000, 0.1
        )
        assert "保全不足" in reason
        assert "90,000,000" in reason
