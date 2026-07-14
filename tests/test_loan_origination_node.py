"""Tests for the grounded, audited origination node (loan-lifecycle breadth).

Verifies the graph-side origination contract, fully offline (no audit backend ->
record_event is a no-op; grounding is deterministic):

- the deterministic recommendation + provisional ceiling reach the banker;
- the advisory reason is grounded (no 【未検証】 marker on the deterministic text);
- ONLY the administrative APPLIED -> UNDER_REVIEW transition is recorded; the
  credit decision (UNDER_REVIEW -> APPROVED / DECLINED) is NEVER auto-recorded
  (it is HITL-gated);
- DECLINE paths surface zero ceiling and decline;
- annualised sales are derived from the latest Shisanhyo;
- the node is read-only on the snapshot and deterministic.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.loan_origination import (
    annual_sales_from_state,
    loan_origination_node,
)
from app.backend.state import SaiseiState
from app.shared.constants import (
    MONTHS_PER_YEAR,
    ORIGINATION_TDB_APPROVE_FLOOR,
)
from app.shared.models.accounting import TrialBalance
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)
_FLOOR = ORIGINATION_TDB_APPROVE_FLOOR


def _tb(sales: int) -> TrialBalance:
    return TrialBalance(
        period=dt.date(2026, 3, 31),
        uriage=sales,
        uriage_genka=int(sales * 0.7),
        hanbaihi=int(sales * 0.1),
    )


def _applied_log() -> list[dict[str, object]]:
    return [LoanEvent(status=LoanStatus.APPLIED, at=_AT, actor="system").model_dump(mode="json")]


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


# --- annualised sales -----------------------------------------------------


def test_annual_sales_from_latest_shisanhyo() -> None:
    state = _state(shisanhyo=[_tb(10_000_000), _tb(12_000_000)])
    assert annual_sales_from_state(state) == 12_000_000 * MONTHS_PER_YEAR


def test_annual_sales_zero_without_shisanhyo() -> None:
    assert annual_sales_from_state(_state()) == 0


# --- approve path ---------------------------------------------------------


def test_approve_recommendation_is_grounded_with_ceiling() -> None:
    state = _state(tdb_score=_FLOOR, shisanhyo=[_tb(100_000_000)])
    out = loan_origination_node(state)
    rec = out["origination_recommendation"]
    assert rec["recommendation"] == "approve"
    assert rec["proposed_status"] == LoanStatus.APPROVED.value
    assert rec["max_facility_amount"] > 0
    assert rec["grounded"] is True
    assert "\u672a検証" not in rec["reason"]


def test_approve_records_only_the_administrative_intake_transition() -> None:
    state = _state(
        tdb_score=80,
        shisanhyo=[_tb(100_000_000)],
        loan_id="L-1",
        loan_events=_applied_log(),
    )
    out = loan_origination_node(state)
    events = out["loan_events"]
    # Exactly one event, and it is the administrative APPLIED -> UNDER_REVIEW.
    # The credit decision (-> APPROVED / DECLINED) is HITL-gated, never here.
    assert len(events) == 1
    assert events[0]["status"] == LoanStatus.UNDER_REVIEW.value
    assert events[0]["actor"] == "system"
    statuses = {e["status"] for e in events}
    assert LoanStatus.APPROVED.value not in statuses
    assert LoanStatus.DECLINED.value not in statuses


# --- decline paths --------------------------------------------------------


def test_decline_below_floor_has_zero_ceiling() -> None:
    state = _state(tdb_score=_FLOOR - 1, shisanhyo=[_tb(100_000_000)])
    rec = loan_origination_node(state)["origination_recommendation"]
    assert rec["recommendation"] == "decline"
    assert rec["proposed_status"] == LoanStatus.DECLINED.value
    assert rec["max_facility_amount"] == 0
    assert rec["grounded"] is True


def test_decline_when_anti_social_flagged() -> None:
    # A FLAGGED anti-social check is carried as an intake error string; the node
    # must conservatively recommend DECLINE even with a strong score.
    state = _state(
        tdb_score=95,
        shisanhyo=[_tb(100_000_000)],
        errors=["Anti-social-forces check FLAGGED — escalate; no turnaround support."],
    )
    rec = loan_origination_node(state)["origination_recommendation"]
    assert rec["recommendation"] == "decline"
    assert rec["max_facility_amount"] == 0


# --- no-loan / read-only / determinism -----------------------------------


def test_no_loan_attached_records_no_events() -> None:
    state = _state(tdb_score=80, shisanhyo=[_tb(100_000_000)])
    assert loan_origination_node(state)["loan_events"] == []


def test_node_does_not_mutate_state() -> None:
    state = _state(tdb_score=80, shisanhyo=[_tb(100_000_000)], loan_events=_applied_log())
    before = state.model_dump(mode="json")
    loan_origination_node(state)
    assert state.model_dump(mode="json") == before


def test_node_is_deterministic() -> None:
    state = _state(tdb_score=72, shisanhyo=[_tb(54_000_000)])
    a = loan_origination_node(state)["origination_recommendation"]
    b = loan_origination_node(state)["origination_recommendation"]
    assert a == b


# --- collateral / guarantee coverage annotation (breadth #6) --------------


def test_coverage_block_is_attached_to_the_recommendation() -> None:
    # The advisory coverage block rides on origination_recommendation, beside
    # debt_capacity, for the banker at the 稟議 gate.
    state = _state(tdb_score=80, shisanhyo=[_tb(100_000_000)])
    rec = loan_origination_node(state)["origination_recommendation"]
    assert "coverage" in rec
    assert rec["coverage"]["band"] in {"well_covered", "partial", "uncovered"}


def test_coverage_with_no_data_is_uncovered_for_a_positive_facility() -> None:
    # The prudent-banker default: an APPROVE with a positive ceiling but no
    # supplied collateral / guarantee bands as 'uncovered' (never assumed secured).
    state = _state(tdb_score=80, shisanhyo=[_tb(100_000_000)])
    cov = loan_origination_node(state)["origination_recommendation"]["coverage"]
    assert cov["covered_amount"] == 0
    assert cov["band"] == "uncovered"
    assert cov["uncovered_amount"] > 0


def test_coverage_reflects_supplied_collateral_and_guarantee() -> None:
    # Collateral + guarantee that meet/exceed the facility band as well_covered.
    state = _state(tdb_score=80, shisanhyo=[_tb(100_000_000)])
    facility = loan_origination_node(state)["origination_recommendation"]["max_facility_amount"]
    covered = _state(
        tdb_score=80,
        shisanhyo=[_tb(100_000_000)],
        collateral_value=facility,
        guarantee_coverage=0,
    )
    cov = loan_origination_node(covered)["origination_recommendation"]["coverage"]
    assert cov["covered_amount"] == facility
    assert cov["band"] == "well_covered"
    assert cov["uncovered_amount"] == 0


def test_coverage_is_advisory_only_does_not_alter_the_ceiling() -> None:
    # Supplying coverage never changes the recommended facility ceiling.
    base = loan_origination_node(_state(tdb_score=80, shisanhyo=[_tb(100_000_000)]))[
        "origination_recommendation"
    ]["max_facility_amount"]
    with_cov = loan_origination_node(
        _state(
            tdb_score=80,
            shisanhyo=[_tb(100_000_000)],
            collateral_value=999_000_000,
            guarantee_coverage=999_000_000,
        )
    )["origination_recommendation"]["max_facility_amount"]
    assert base == with_cov
