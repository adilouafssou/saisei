"""Tests for the deterministic debt-service-capacity check (origination breadth).

The verifier for the verifier: proves the prudent debt-service ceiling and the
facility's implied annual debt service are pure, hand-derivable arithmetic over
the firm's OWN ordinary profit, and that the capacity bands behave correctly
across within_capacity / stretch / over_capacity.

Every expected figure below is hand-derived in the test docstring so each
assertion is auditable to the yen. All tests are offline, deterministic, and
import only from ``app.*``.
"""

from __future__ import annotations

import datetime as dt

from app.backend.analysis.debt_capacity import (
    assess_debt_capacity,
    capacity_bounded_ceiling,
    classify_debt_capacity,
    debt_capacity_reason,
    demonstrated_ordinary_profit,
    implied_annual_debt_service,
)
from app.shared.constants import (
    DEBT_CAPACITY_AMORTIZATION_YEARS,
    DEBT_CAPACITY_DSCR_FRACTION,
    DEBT_CAPACITY_STRETCH_FACTOR,
    MONTHS_PER_YEAR,
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


#: A two-month history with a FLAT ordinary profit of 1,000,000/month:
#:   sales 10M, COGS 8M (gross 2M), SG&A 1M -> operating/ordinary 1,000,000.
#: Both months identical, so trailing-average == latest == 1,000,000.
#:
#: Hand-derived ordinary-profit base:
#:   base_monthly = min(avg 1,000,000, latest 1,000,000) = 1,000,000
#:   annual_ordinary_profit = 1,000,000 * 12 = 12,000,000
#:   prudent_service_ceiling = 12,000,000 * 0.5 = 6,000,000
def _flat_history() -> list[TrialBalance]:
    return [
        _tb(dt.date(2025, 4, 30), 10_000_000, 8_000_000, 1_000_000),
        _tb(dt.date(2025, 5, 31), 10_000_000, 8_000_000, 1_000_000),
    ]


_EXPECTED_ANNUAL_OP = 12_000_000
_EXPECTED_CEILING = int(round(_EXPECTED_ANNUAL_OP * DEBT_CAPACITY_DSCR_FRACTION))


def _debt_service(facility: int) -> int:
    principal = int(round(facility / DEBT_CAPACITY_AMORTIZATION_YEARS))
    interest = int(round(facility * WORKING_CAPITAL_FINANCING_RATE))
    return principal + interest


class TestDemonstratedOrdinaryProfit:
    """The ordinary-profit base is the conservative min(avg, latest), annualised."""

    def test_flat_history_is_monthly_times_twelve(self) -> None:
        assert demonstrated_ordinary_profit(_flat_history()) == _EXPECTED_ANNUAL_OP
        expected = 1_000_000 * MONTHS_PER_YEAR
        assert demonstrated_ordinary_profit(_flat_history()) == expected

    def test_uses_min_of_average_and_latest_when_latest_is_weaker(self) -> None:
        # Strong month 1 (op 3,000,000), weak latest month 2 (op 1,000,000).
        # avg = 2,000,000 ; latest = 1,000,000 ; min = 1,000,000 -> 12,000,000.
        rows = [
            _tb(dt.date(2025, 4, 30), 10_000_000, 6_000_000, 1_000_000),  # op 3M
            _tb(dt.date(2025, 5, 31), 10_000_000, 8_000_000, 1_000_000),  # op 1M
        ]
        assert demonstrated_ordinary_profit(rows) == 12_000_000

    def test_uses_min_when_average_is_weaker_than_latest(self) -> None:
        # Weak month 1 (op 1,000,000), strong latest month 2 (op 3,000,000).
        # avg = 2,000,000 ; latest = 3,000,000 ; min = 2,000,000 -> 24,000,000.
        # A single strong latest month cannot inflate capacity above the average.
        rows = [
            _tb(dt.date(2025, 4, 30), 10_000_000, 8_000_000, 1_000_000),  # op 1M
            _tb(dt.date(2025, 5, 31), 10_000_000, 6_000_000, 1_000_000),  # op 3M
        ]
        assert demonstrated_ordinary_profit(rows) == 24_000_000

    def test_negative_ordinary_profit_is_floored_at_zero(self) -> None:
        # SG&A 3M on gross 2M -> operating/ordinary -1,000,000/month.
        rows = [
            _tb(dt.date(2025, 5, 31), 10_000_000, 8_000_000, 3_000_000),
        ]
        assert demonstrated_ordinary_profit(rows) == 0

    def test_empty_history_is_zero(self) -> None:
        assert demonstrated_ordinary_profit([]) == 0

    def test_deterministic(self) -> None:
        assert demonstrated_ordinary_profit(_flat_history()) == demonstrated_ordinary_profit(
            _flat_history()
        )


class TestImpliedAnnualDebtService:
    """The debt-service legs are hand-derivable; interest reuses the WC rate."""

    def test_principal_leg_amortizes_over_the_horizon(self) -> None:
        principal, _ = implied_annual_debt_service(50_000_000)
        assert principal == int(round(50_000_000 / DEBT_CAPACITY_AMORTIZATION_YEARS))

    def test_interest_leg_reuses_working_capital_financing_rate(self) -> None:
        _, interest = implied_annual_debt_service(50_000_000)
        assert interest == int(round(50_000_000 * WORKING_CAPITAL_FINANCING_RATE))

    def test_non_positive_facility_implies_no_service(self) -> None:
        assert implied_annual_debt_service(0) == (0, 0)
        assert implied_annual_debt_service(-1) == (0, 0)


class TestCapacityBoundedCeiling:
    """The capacity-bounded ceiling is the exact inverse of the debt service."""

    def test_is_prudent_ceiling_over_the_service_rate(self) -> None:
        # service_rate = 1/5 + 0.05 = 0.25, so F_max = ceiling / 0.25 = ceiling*4.
        # ceiling 6,000,000 -> 24,000,000.
        assert capacity_bounded_ceiling(6_000_000) == 24_000_000

    def test_round_trips_within_capacity(self) -> None:
        # The bounded ceiling's own implied service must NOT exceed the prudent
        # ceiling it was derived from (the floor guarantees this).
        ceiling = 3_000_000
        bounded = capacity_bounded_ceiling(ceiling)
        principal, interest = implied_annual_debt_service(bounded)
        assert principal + interest <= ceiling

    def test_zero_ceiling_is_zero(self) -> None:
        assert capacity_bounded_ceiling(0) == 0
        assert capacity_bounded_ceiling(-1) == 0


class TestClassifyDebtCapacity:
    """The band boundaries are exact and hand-checkable."""

    def test_within_ceiling_is_within_capacity(self) -> None:
        band, ratio = classify_debt_capacity(_EXPECTED_CEILING, _EXPECTED_CEILING)
        assert band == "within_capacity"
        assert ratio == 1.0

    def test_just_above_ceiling_is_stretch(self) -> None:
        band, _ = classify_debt_capacity(_EXPECTED_CEILING + 1, _EXPECTED_CEILING)
        assert band == "stretch"

    def test_at_stretch_factor_is_stretch(self) -> None:
        service = int(_EXPECTED_CEILING * DEBT_CAPACITY_STRETCH_FACTOR)
        band, _ = classify_debt_capacity(service, _EXPECTED_CEILING)
        assert band == "stretch"

    def test_beyond_stretch_factor_is_over_capacity(self) -> None:
        service = int(_EXPECTED_CEILING * DEBT_CAPACITY_STRETCH_FACTOR) + 1_000_000
        band, ratio = classify_debt_capacity(service, _EXPECTED_CEILING)
        assert band == "over_capacity"
        assert ratio is not None and ratio > DEBT_CAPACITY_STRETCH_FACTOR

    def test_zero_or_negative_service_is_within_capacity(self) -> None:
        assert classify_debt_capacity(0, _EXPECTED_CEILING) == ("within_capacity", 0.0)
        assert classify_debt_capacity(-5, _EXPECTED_CEILING) == ("within_capacity", 0.0)

    def test_positive_service_against_zero_ceiling_is_over_capacity(self) -> None:
        band, ratio = classify_debt_capacity(1_000_000, 0)
        assert band == "over_capacity"
        assert ratio is None  # division undefined when ceiling is 0


class TestAssessDebtCapacity:
    """End-to-end: the public entry point composes profile + band + reason."""

    def test_within_capacity_facility(self) -> None:
        # Ceiling 6,000,000/yr. Pick a facility whose service <= 6,000,000.
        # facility 20M -> principal 4M + interest 1M = 5,000,000 <= 6,000,000.
        result = assess_debt_capacity(_flat_history(), 20_000_000)
        assert result.profile.annual_ordinary_profit == _EXPECTED_ANNUAL_OP
        assert result.profile.prudent_service_ceiling == _EXPECTED_CEILING
        assert result.profile.annual_debt_service == _debt_service(20_000_000)
        assert result.band == "within_capacity"
        assert "余力内" in result.reason

    def test_stretch_facility(self) -> None:
        # facility 30M -> principal 6M + interest 1.5M = 7,500,000.
        # 6,000,000 < 7,500,000 <= 9,000,000 (6M * 1.5) -> stretch.
        result = assess_debt_capacity(_flat_history(), 30_000_000)
        assert result.profile.annual_debt_service == _debt_service(30_000_000)
        assert result.band == "stretch"
        assert "余力上限" in result.reason
        assert result.ratio is not None

    def test_over_capacity_facility(self) -> None:
        # facility 50M -> principal 10M + interest 2.5M = 12,500,000.
        # 12,500,000 > 9,000,000 (6M * 1.5) -> over_capacity.
        result = assess_debt_capacity(_flat_history(), 50_000_000)
        assert result.profile.annual_debt_service == _debt_service(50_000_000)
        assert result.band == "over_capacity"
        assert "余力超過" in result.reason
        assert result.ratio is not None and result.ratio > DEBT_CAPACITY_STRETCH_FACTOR

    def test_over_capacity_surfaces_the_bounded_ceiling(self) -> None:
        # The over-sized warning comes WITH a prudent number: the capacity-bounded
        # ceiling (prudent ceiling 6,000,000 -> 24,000,000) is on the verdict and
        # named in the reason.
        result = assess_debt_capacity(_flat_history(), 50_000_000)
        assert result.capacity_bounded_ceiling == 24_000_000
        assert "余力相当融資" in result.reason
        # The bounded ceiling itself round-trips within capacity.
        principal, interest = implied_annual_debt_service(result.capacity_bounded_ceiling)
        assert principal + interest <= result.profile.prudent_service_ceiling

    def test_negative_ordinary_profit_makes_any_facility_over_capacity(self) -> None:
        # Loss-making firm: ordinary-profit base 0 -> ceiling 0 -> any positive
        # facility is over_capacity with a ratio-free reason (the parallel of the
        # uplift 'ceiling <= 0 -> implausible' rule).
        loss = [_tb(dt.date(2025, 5, 31), 10_000_000, 8_000_000, 3_000_000)]
        result = assess_debt_capacity(loss, 20_000_000)
        assert result.profile.prudent_service_ceiling == 0
        assert result.band == "over_capacity"
        assert result.ratio is None
        assert "ゼロ" in result.reason

    def test_zero_facility_is_within_capacity(self) -> None:
        # A DECLINE recommendation carries a 0 ceiling -> no debt service.
        result = assess_debt_capacity(_flat_history(), 0)
        assert result.profile.annual_debt_service == 0
        assert result.band == "within_capacity"

    def test_reason_names_each_service_leg(self) -> None:
        result = assess_debt_capacity(_flat_history(), 30_000_000)
        assert "元本" in result.reason
        assert "金利" in result.reason
        assert "経常利益" in result.reason

    def test_deterministic(self) -> None:
        a = assess_debt_capacity(_flat_history(), 30_000_000)
        b = assess_debt_capacity(_flat_history(), 30_000_000)
        assert a == b


class TestDebtCapacityReason:
    """The reason string is deterministic display prose, decides nothing."""

    def test_no_service_within_capacity_reason(self) -> None:
        result = assess_debt_capacity(_flat_history(), 0)
        assert "返済負担なし" in result.reason

    def test_zero_ceiling_over_capacity_reason(self) -> None:
        from app.backend.analysis.debt_capacity import DebtServiceProfile

        profile = DebtServiceProfile(
            annual_ordinary_profit=0,
            prudent_service_ceiling=0,
            principal_leg=4_000_000,
            interest_leg=1_000_000,
            annual_debt_service=5_000_000,
        )
        reason = debt_capacity_reason("over_capacity", profile, None, 0)
        assert "余力超過" in reason
        assert "ゼロ" in reason
