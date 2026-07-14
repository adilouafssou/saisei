"""Tests for the deterministic uplift-credibility grounding (depth step 4).

The verifier for the verifier: proves the uplift plausibility ceiling is pure,
hand-derivable arithmetic over the firm's OWN figures, and that the credibility
bands behave correctly across grounded / stretch / implausible.

Every expected figure below is hand-derived in the test docstring so each
assertion is auditable to the yen. All tests are offline, deterministic, and
import only from ``app.*``.
"""

from __future__ import annotations

import datetime as dt

from app.backend.analysis.uplift_grounding import (
    assess_uplift,
    classify_uplift_credibility,
    compute_uplift_headroom,
    uplift_credibility_reason,
)
from app.shared.constants import (
    MONTHS_PER_YEAR,
    UPLIFT_SGA_REDUCTION_CEILING,
    UPLIFT_STRETCH_FACTOR,
    WORKING_CAPITAL_FINANCING_RATE,
)
from app.shared.models.accounting import TrialBalance


def _tb(
    period: dt.date,
    uriage: int,
    uriage_genka: int,
    hanbaihi: int = 0,
) -> TrialBalance:
    return TrialBalance(
        period=period,
        uriage=uriage,
        uriage_genka=uriage_genka,
        hanbaihi=hanbaihi,
    )


#: A two-month history: best margin 40% (month 1, COGS 6M on sales 10M),
#: current margin 20% (month 2, COGS 8M on sales 10M). SG&A 1M in the latest.
#:
#: Hand-derived headroom for the latest month (sales 10M):
#:   margin_recovery_monthly = (0.40 - 0.20) * 10M = 2,000,000
#:   margin_recovery_annual  = 2,000,000 * 12      = 24,000,000
#:   cost_reduction_monthly  = 1,000,000 * 0.20    =   200,000
#:   cost_reduction_annual   =   200,000 * 12      = 2,400,000
#:   ceiling (no WC gap)     = 24,000,000 + 2,400,000 = 26,400,000
def _history() -> list[TrialBalance]:
    return [
        _tb(dt.date(2025, 4, 30), 10_000_000, 6_000_000, 1_000_000),  # best 40%
        _tb(dt.date(2025, 5, 31), 10_000_000, 8_000_000, 1_000_000),  # current 20%
    ]


_EXPECTED_MARGIN_RECOVERY = 24_000_000
_EXPECTED_COST_REDUCTION = 2_400_000
_EXPECTED_CEILING = _EXPECTED_MARGIN_RECOVERY + _EXPECTED_COST_REDUCTION  # 26,400,000


class TestComputeUpliftHeadroom:
    """The headroom is hand-derivable arithmetic over the firm's own figures."""

    def test_margin_recovery_is_best_minus_current(self) -> None:
        h = compute_uplift_headroom(_history())
        assert h.margin_recovery == _EXPECTED_MARGIN_RECOVERY

    def test_cost_reduction_is_bounded_fraction_of_sga(self) -> None:
        h = compute_uplift_headroom(_history())
        assert h.cost_reduction == _EXPECTED_COST_REDUCTION
        # Explicitly tied to the governance constant.
        expected = int(round(1_000_000 * UPLIFT_SGA_REDUCTION_CEILING * MONTHS_PER_YEAR))
        assert h.cost_reduction == expected

    def test_ceiling_is_sum_of_components(self) -> None:
        h = compute_uplift_headroom(_history())
        assert h.ceiling == _EXPECTED_CEILING
        assert h.ceiling == h.margin_recovery + h.cost_reduction + h.wc_financing_relief

    def test_no_margin_compression_yields_no_margin_headroom(self) -> None:
        # Margin never fell: best == current, so margin-recovery headroom is 0.
        rows = [
            _tb(dt.date(2025, 4, 30), 10_000_000, 6_000_000, 1_000_000),  # 40%
            _tb(dt.date(2025, 5, 31), 10_000_000, 6_000_000, 1_000_000),  # 40%
        ]
        h = compute_uplift_headroom(rows)
        assert h.margin_recovery == 0
        assert h.cost_reduction == _EXPECTED_COST_REDUCTION

    def test_empty_history_is_all_zero(self) -> None:
        h = compute_uplift_headroom([])
        assert (h.margin_recovery, h.cost_reduction, h.wc_financing_relief, h.ceiling) == (
            0,
            0,
            0,
            0,
        )

    def test_deterministic(self) -> None:
        assert compute_uplift_headroom(_history()) == compute_uplift_headroom(_history())


class TestWorkingCapitalRelief:
    """The WC financing relief reuses the existing financing-rate constant."""

    def test_deficit_adds_relief_to_ceiling(self) -> None:
        # gap = -20,000,000 -> relief = 20,000,000 * WORKING_CAPITAL_FINANCING_RATE.
        gap = -20_000_000
        expected_relief = int(round(-gap * WORKING_CAPITAL_FINANCING_RATE))
        result = assess_uplift(_history(), claimed_uplift=0, working_capital_gap=gap)
        assert result.headroom.wc_financing_relief == expected_relief
        assert result.headroom.ceiling == _EXPECTED_CEILING + expected_relief

    def test_no_deficit_adds_no_relief(self) -> None:
        result = assess_uplift(_history(), claimed_uplift=0, working_capital_gap=0)
        assert result.headroom.wc_financing_relief == 0
        positive = assess_uplift(_history(), claimed_uplift=0, working_capital_gap=5_000_000)
        assert positive.headroom.wc_financing_relief == 0
        none_gap = assess_uplift(_history(), claimed_uplift=0, working_capital_gap=None)
        assert none_gap.headroom.wc_financing_relief == 0


class TestClassifyUpliftCredibility:
    """The band boundaries are exact and hand-checkable."""

    def test_within_ceiling_is_grounded(self) -> None:
        band, ratio = classify_uplift_credibility(_EXPECTED_CEILING, _EXPECTED_CEILING)
        assert band == "grounded"
        assert ratio == 1.0

    def test_just_above_ceiling_is_stretch(self) -> None:
        band, _ = classify_uplift_credibility(_EXPECTED_CEILING + 1, _EXPECTED_CEILING)
        assert band == "stretch"

    def test_at_stretch_factor_is_stretch(self) -> None:
        claimed = int(_EXPECTED_CEILING * UPLIFT_STRETCH_FACTOR)
        band, _ = classify_uplift_credibility(claimed, _EXPECTED_CEILING)
        assert band == "stretch"

    def test_beyond_stretch_factor_is_implausible(self) -> None:
        claimed = int(_EXPECTED_CEILING * UPLIFT_STRETCH_FACTOR) + 1_000_000
        band, ratio = classify_uplift_credibility(claimed, _EXPECTED_CEILING)
        assert band == "implausible"
        assert ratio is not None and ratio > UPLIFT_STRETCH_FACTOR

    def test_zero_or_negative_claim_is_grounded(self) -> None:
        assert classify_uplift_credibility(0, _EXPECTED_CEILING) == ("grounded", 0.0)
        assert classify_uplift_credibility(-5_000_000, _EXPECTED_CEILING) == ("grounded", 0.0)

    def test_positive_claim_against_zero_ceiling_is_implausible(self) -> None:
        band, ratio = classify_uplift_credibility(1_000_000, 0)
        assert band == "implausible"
        assert ratio is None  # division undefined when ceiling is 0


class TestAssessUplift:
    """End-to-end: the public entry point composes ceiling + band + reason."""

    def test_grounded_claim(self) -> None:
        # 20M <= 26.4M ceiling -> grounded.
        result = assess_uplift(_history(), claimed_uplift=20_000_000)
        assert result.band == "grounded"
        assert result.headroom.ceiling == _EXPECTED_CEILING
        assert "根拠あり" in result.reason

    def test_stretch_claim(self) -> None:
        # 35M: 26.4M < 35M <= 39.6M (26.4M * 1.5) -> stretch.
        result = assess_uplift(_history(), claimed_uplift=35_000_000)
        assert result.band == "stretch"
        assert "野心的" in result.reason
        assert result.ratio is not None

    def test_implausible_claim(self) -> None:
        # 50M > 39.6M (26.4M * 1.5) -> implausible.
        result = assess_uplift(_history(), claimed_uplift=50_000_000)
        assert result.band == "implausible"
        assert "非現実的" in result.reason
        assert result.ratio is not None and result.ratio > UPLIFT_STRETCH_FACTOR

    def test_reason_names_each_headroom_component(self) -> None:
        result = assess_uplift(_history(), claimed_uplift=20_000_000)
        assert "粗利回復" in result.reason
        assert "販管費削減" in result.reason
        assert "資金繰り改善" in result.reason

    def test_deterministic(self) -> None:
        a = assess_uplift(_history(), claimed_uplift=35_000_000, working_capital_gap=-10_000_000)
        b = assess_uplift(_history(), claimed_uplift=35_000_000, working_capital_gap=-10_000_000)
        assert a == b

    def test_deficit_can_lift_a_stretch_claim_into_grounded(self) -> None:
        """Adding WC financing relief raises the ceiling, so the SAME claim can
        move from stretch to grounded -- proving the relief feeds the ceiling."""
        claim = 35_000_000  # stretch against the 26.4M base ceiling
        assert assess_uplift(_history(), claim).band == "stretch"
        # A large deficit adds relief = 200M * rate; with rate 0.05 -> 10M,
        # lifting the ceiling to 36.4M so 35M is now grounded.
        big_relief = assess_uplift(_history(), claim, working_capital_gap=-200_000_000)
        assert big_relief.headroom.wc_financing_relief == int(
            round(200_000_000 * WORKING_CAPITAL_FINANCING_RATE)
        )
        assert big_relief.band == "grounded"


class TestUpliftCredibilityReason:
    """The reason string is deterministic display prose, decides nothing."""

    def test_no_uplift_claimed(self) -> None:
        result = assess_uplift(_history(), claimed_uplift=0)
        assert "上乗せ主張なし" in result.reason

    def test_zero_ceiling_implausible_reason(self) -> None:
        # Empty history -> zero ceiling; a positive claim is implausible with a
        # ratio-free reason (the ceiling is zero).
        from app.backend.analysis.uplift_grounding import UpliftHeadroom

        headroom = UpliftHeadroom(0, 0, 0, 0)
        reason = uplift_credibility_reason("implausible", 5_000_000, headroom, None)
        assert "非現実的" in reason
        assert "ゼロ" in reason
