"""Tests for the loan-origination bridge (deterministic underwriting, breadth).

The origination mirror of test_loan.py's distress-side coverage: the credit
recommendation cascade, the provisional facility ceiling, the bundled
recommendation + its auditable reason, and the load-bearing invariant that every
transition the recommendation can imply is a legal, HITL-gated transition out of
UNDER_REVIEW. Pure, offline, deterministic — no LLM, no graph.
"""

from __future__ import annotations

import pytest
from app.shared.constants import (
    ORIGINATION_MAX_FACILITY_SALES_MULTIPLE,
    ORIGINATION_TDB_APPROVE_FLOOR,
)
from app.shared.models.loan import (
    HITL_GATED_TRANSITIONS,
    LoanStatus,
    OriginationDecision,
    OriginationRecommendation,
    max_facility_amount,
    proposed_origination_decision,
    recommend_origination,
)

_FLOOR = ORIGINATION_TDB_APPROVE_FLOOR


# --- credit-recommendation cascade --------------------------------------


def test_approve_when_score_clears_floor_and_clean() -> None:
    assert (
        proposed_origination_decision(_FLOOR, anti_social_clear=True) is OriginationDecision.APPROVE
    )
    assert proposed_origination_decision(95, anti_social_clear=True) is OriginationDecision.APPROVE


def test_decline_below_floor() -> None:
    assert (
        proposed_origination_decision(_FLOOR - 1, anti_social_clear=True)
        is OriginationDecision.DECLINE
    )


def test_decline_when_no_score() -> None:
    assert (
        proposed_origination_decision(None, anti_social_clear=True) is OriginationDecision.DECLINE
    )


def test_anti_social_flag_overrides_a_strong_score() -> None:
    # A perfect credit score cannot override the compliance bar.
    assert (
        proposed_origination_decision(100, anti_social_clear=False) is OriginationDecision.DECLINE
    )


# --- decision -> proposed status ----------------------------------------


def test_decision_proposes_the_hitl_gated_status() -> None:
    assert OriginationDecision.APPROVE.proposed_status is LoanStatus.APPROVED
    assert OriginationDecision.DECLINE.proposed_status is LoanStatus.DECLINED


def test_proposed_transition_is_legal_and_hitl_gated_from_under_review() -> None:
    # The load-bearing invariant: both recommendations map to a legal,
    # HITL-gated transition out of UNDER_REVIEW (the banker is the decider).
    for decision in OriginationDecision:
        target = decision.proposed_status
        assert LoanStatus.UNDER_REVIEW.can_transition_to(target)
        assert (LoanStatus.UNDER_REVIEW, target) in HITL_GATED_TRANSITIONS


# --- provisional facility ceiling ---------------------------------------


def test_max_facility_is_sales_multiple() -> None:
    assert max_facility_amount(1_000_000_000) == round(
        1_000_000_000 * ORIGINATION_MAX_FACILITY_SALES_MULTIPLE
    )


def test_max_facility_is_integer_yen_rounded() -> None:
    # 333,333,333 * 0.5 = 166,666,666.5 -> banker's rounding to 166,666,666.
    amount = max_facility_amount(333_333_333)
    assert isinstance(amount, int)
    assert amount == round(333_333_333 * ORIGINATION_MAX_FACILITY_SALES_MULTIPLE)


def test_max_facility_zero_sales_is_zero() -> None:
    assert max_facility_amount(0) == 0


def test_max_facility_negative_sales_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        max_facility_amount(-1)


# --- bundled recommendation + auditable reason --------------------------


def test_recommend_approve_carries_ceiling_and_reason() -> None:
    rec = recommend_origination(_FLOOR, 800_000_000, anti_social_clear=True)
    assert isinstance(rec, OriginationRecommendation)
    assert rec.decision is OriginationDecision.APPROVE
    assert rec.proposed_status is LoanStatus.APPROVED
    assert rec.max_facility_amount == max_facility_amount(800_000_000)
    assert str(_FLOOR) in rec.reason


def test_recommend_decline_below_floor_has_zero_ceiling() -> None:
    rec = recommend_origination(_FLOOR - 1, 800_000_000, anti_social_clear=True)
    assert rec.decision is OriginationDecision.DECLINE
    assert rec.proposed_status is LoanStatus.DECLINED
    # No facility is recommended on a decline, even with healthy sales.
    assert rec.max_facility_amount == 0
    assert "below the origination approval floor" in rec.reason


def test_recommend_decline_anti_social_reason() -> None:
    rec = recommend_origination(100, 800_000_000, anti_social_clear=False)
    assert rec.decision is OriginationDecision.DECLINE
    assert rec.max_facility_amount == 0
    assert "anti-social" in rec.reason


def test_recommend_decline_no_score_reason() -> None:
    rec = recommend_origination(None, 800_000_000, anti_social_clear=True)
    assert rec.decision is OriginationDecision.DECLINE
    assert rec.max_facility_amount == 0
    assert "no TDB credit score" in rec.reason


def test_recommend_approve_with_zero_sales_has_zero_ceiling() -> None:
    # Creditworthy applicant but no sales figure -> approve with no provisional
    # ceiling (omitted, not guessed), mirroring the provision-amount stance.
    rec = recommend_origination(_FLOOR, 0, anti_social_clear=True)
    assert rec.decision is OriginationDecision.APPROVE
    assert rec.max_facility_amount == 0


# --- immutability + determinism -----------------------------------------


def test_recommendation_is_frozen() -> None:
    from pydantic import ValidationError

    rec = recommend_origination(_FLOOR, 800_000_000)
    with pytest.raises(ValidationError):
        rec.decision = OriginationDecision.DECLINE  # type: ignore[misc]


def test_recommendation_is_deterministic() -> None:
    a = recommend_origination(72, 540_000_000, anti_social_clear=True)
    b = recommend_origination(72, 540_000_000, anti_social_clear=True)
    assert a == b
