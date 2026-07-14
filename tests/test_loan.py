"""Tests for the loan-lifecycle spine (Loan aggregate + LoanStatus + events)."""

from __future__ import annotations

import datetime as dt

import pytest
from app.shared.constants import (
    PROVISION_RATE_BANKRUPT,
    PROVISION_RATE_NEEDS_ATTENTION,
    PROVISION_RATE_SPECIAL_ATTENTION,
)
from app.shared.models.classification import FsaClass
from app.shared.models.loan import (
    HITL_GATED_TRANSITIONS,
    SERVICING_TRANSITIONS,
    Loan,
    LoanEvent,
    LoanStatus,
    current_status,
    is_servicing_transition,
    outstanding_principal,
    outstanding_principal_for_state,
    proposed_servicing_transition,
    proposed_transition_for,
    provision_amount,
    provision_rate_for,
)
from pydantic import ValidationError

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def _event(status: LoanStatus, *, days: int = 0) -> LoanEvent:
    return LoanEvent(status=status, at=_AT + dt.timedelta(days=days), actor="banker-1")


def _loan(*statuses: LoanStatus, principal: int = 150_000_000) -> Loan:
    events = tuple(_event(s, days=i) for i, s in enumerate(statuses))
    return Loan(
        loan_id="L-001",
        hojin_bango="1234567890123",
        principal=principal,
        originated_on=dt.date(2025, 4, 1),
        events=events,
    )


# --- transition legality -------------------------------------------------


def test_full_happy_path_is_legal() -> None:
    loan = _loan(
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
        LoanStatus.PERFORMING,
        LoanStatus.CLOSED,
    )
    assert loan.status is LoanStatus.CLOSED
    assert not loan.is_open


def test_turnaround_path_is_legal() -> None:
    loan = _loan(
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
        LoanStatus.PERFORMING,
        LoanStatus.RESTRUCTURED,
        LoanStatus.WORKOUT,
        LoanStatus.WRITTEN_OFF,
    )
    assert loan.status is LoanStatus.WRITTEN_OFF
    assert loan.status.is_terminal


def test_illegal_transition_rejected() -> None:
    with pytest.raises(ValidationError):
        _loan(LoanStatus.APPLIED, LoanStatus.DISBURSED)


def test_first_event_must_be_applied() -> None:
    with pytest.raises(ValidationError):
        _loan(LoanStatus.UNDER_REVIEW)


def test_empty_event_log_rejected() -> None:
    with pytest.raises(ValidationError):
        Loan(
            loan_id="L-001",
            hojin_bango="1234567890123",
            principal=1_000_000,
            originated_on=dt.date(2025, 4, 1),
            events=(),
        )


def test_no_transition_out_of_terminal() -> None:
    assert LoanStatus.CLOSED.allowed_transitions == frozenset()
    assert LoanStatus.DECLINED.allowed_transitions == frozenset()
    assert LoanStatus.WRITTEN_OFF.allowed_transitions == frozenset()
    with pytest.raises(ValidationError):
        _loan(LoanStatus.APPLIED, LoanStatus.DECLINED, LoanStatus.UNDER_REVIEW)


def test_can_transition_to() -> None:
    assert LoanStatus.PERFORMING.can_transition_to(LoanStatus.RESTRUCTURED)
    assert not LoanStatus.PERFORMING.can_transition_to(LoanStatus.APPROVED)


# --- integer-yen principal ----------------------------------------------


def test_fractional_yen_rejected() -> None:
    with pytest.raises(ValidationError):
        _loan(LoanStatus.APPLIED, principal=1_000.5)  # type: ignore[arg-type]


def test_whole_float_yen_rejected() -> None:
    with pytest.raises(ValidationError):
        _loan(LoanStatus.APPLIED, principal=1_000.0)  # type: ignore[arg-type]


def test_bool_yen_rejected() -> None:
    with pytest.raises(ValidationError):
        _loan(LoanStatus.APPLIED, principal=True)


# --- event-log derivation -----------------------------------------------


def test_current_status_from_log() -> None:
    events = [
        _event(LoanStatus.APPLIED, days=0),
        _event(LoanStatus.UNDER_REVIEW, days=1),
        _event(LoanStatus.APPROVED, days=2),
    ]
    assert current_status(events) is LoanStatus.APPROVED


def test_current_status_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty event log"):
        current_status([])


# --- display + immutability + gating metadata ---------------------------


def test_display_labels() -> None:
    assert LoanStatus.RESTRUCTURED.kanji == "条件変更"
    assert LoanStatus.RESTRUCTURED.english == "Restructured"
    assert LoanStatus.WORKOUT.is_distressed
    assert not LoanStatus.PERFORMING.is_distressed


def test_loan_is_frozen() -> None:
    loan = _loan(LoanStatus.APPLIED)
    with pytest.raises(ValidationError):
        loan.loan_id = "L-002"  # type: ignore[misc]


def test_hitl_gated_transitions_are_subset_of_legal() -> None:
    for src, dst in HITL_GATED_TRANSITIONS:
        assert src.can_transition_to(dst), f"{src} -> {dst} must be legal"


def test_summary_formats_yen() -> None:
    loan = _loan(LoanStatus.APPLIED, principal=150_000_000)
    assert "\u00a5150,000,000" in loan.summary()


# --- depth: FSA classification -> proposed loan transition ---------------


def test_normal_class_proposes_nothing() -> None:
    assert proposed_transition_for(FsaClass.SEIJOSAKI, LoanStatus.PERFORMING) is None


def test_turnaround_classes_propose_restructured() -> None:
    for fsa in (FsaClass.YOCHUISAKI, FsaClass.HATAN_KENENSAKI):
        assert fsa.requires_turnaround
        assert proposed_transition_for(fsa, LoanStatus.PERFORMING) is LoanStatus.RESTRUCTURED


def test_workout_classes_propose_workout() -> None:
    for fsa in (FsaClass.JISSHITSU_HATANSAKI, FsaClass.HATANSAKI):
        assert fsa.requires_workout
        assert proposed_transition_for(fsa, LoanStatus.PERFORMING) is LoanStatus.WORKOUT
        # also legal from an already-restructured facility
        assert proposed_transition_for(fsa, LoanStatus.RESTRUCTURED) is LoanStatus.WORKOUT


def test_proposed_transition_is_always_legal_and_hitl_gated() -> None:
    for fsa in FsaClass:
        for current in LoanStatus:
            target = proposed_transition_for(fsa, current)
            if target is None:
                continue
            assert current.can_transition_to(target)
            assert (current, target) in HITL_GATED_TRANSITIONS


def test_no_proposal_from_terminal_or_pre_disbursement() -> None:
    # A turnaround class cannot restructure a loan that is not yet performing.
    assert proposed_transition_for(FsaClass.YOCHUISAKI, LoanStatus.APPLIED) is None
    assert proposed_transition_for(FsaClass.HATANSAKI, LoanStatus.CLOSED) is None


# --- breadth: servicing transitions (実行 → 正常 → 完済) ----------------------


def test_disbursed_proposes_performing_for_any_outstanding() -> None:
    # Entering normal servicing is an operational step, independent of balance.
    assert proposed_servicing_transition(LoanStatus.DISBURSED, 150_000_000) is LoanStatus.PERFORMING
    assert proposed_servicing_transition(LoanStatus.DISBURSED, 0) is LoanStatus.PERFORMING


def test_performing_proposes_closed_only_when_fully_repaid() -> None:
    # 完済 is a fact: only when outstanding principal has reached zero.
    assert proposed_servicing_transition(LoanStatus.PERFORMING, 0) is LoanStatus.CLOSED
    assert proposed_servicing_transition(LoanStatus.PERFORMING, 1) is None
    assert proposed_servicing_transition(LoanStatus.PERFORMING, 50_000_000) is None


def test_no_servicing_transition_from_other_statuses() -> None:
    for current in (
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DECLINED,
        LoanStatus.WORKOUT,
        LoanStatus.CLOSED,
        LoanStatus.WRITTEN_OFF,
    ):
        assert proposed_servicing_transition(current, 0) is None


def test_servicing_negative_outstanding_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        proposed_servicing_transition(LoanStatus.PERFORMING, -1)


def test_every_servicing_proposal_is_legal_and_a_servicing_transition() -> None:
    for current in LoanStatus:
        for outstanding in (0, 100_000_000):
            target = proposed_servicing_transition(current, outstanding)
            if target is None:
                continue
            assert current.can_transition_to(target)
            assert (current, target) in SERVICING_TRANSITIONS
            assert is_servicing_transition(current, target)


def test_servicing_transitions_are_legal() -> None:
    for src, dst in SERVICING_TRANSITIONS:
        assert src.can_transition_to(dst), f"{src} -> {dst} must be legal"


def test_servicing_and_hitl_gated_sets_are_disjoint() -> None:
    # A servicing move is an operational fact, NEVER a banker-authority credit /
    # distress transition -- the structural guarantee the breadth bridge stays
    # out of the gated half.
    assert SERVICING_TRANSITIONS.isdisjoint(HITL_GATED_TRANSITIONS)


def test_is_servicing_transition_rejects_distress_and_credit_moves() -> None:
    assert not is_servicing_transition(LoanStatus.PERFORMING, LoanStatus.RESTRUCTURED)
    assert not is_servicing_transition(LoanStatus.PERFORMING, LoanStatus.WORKOUT)
    assert not is_servicing_transition(LoanStatus.UNDER_REVIEW, LoanStatus.APPROVED)


# --- amortization: partial repayment (一部入金) + outstanding principal ------


def _repay(amount: int, *, days: int = 0, status: LoanStatus = LoanStatus.PERFORMING) -> LoanEvent:
    """A partial-repayment self-event carrying ``amount`` repaid."""
    return LoanEvent(
        status=status,
        at=_AT + dt.timedelta(days=days),
        actor="system",
        principal_repaid=amount,
    )


def _performing_loan(*extra: LoanEvent, principal: int = 100_000_000) -> Loan:
    """A facility driven to PERFORMING, plus any ``extra`` trailing events."""
    chain = (
        _event(LoanStatus.APPLIED, days=0),
        _event(LoanStatus.UNDER_REVIEW, days=1),
        _event(LoanStatus.APPROVED, days=2),
        _event(LoanStatus.DISBURSED, days=3),
        _event(LoanStatus.PERFORMING, days=4),
    )
    return Loan(
        loan_id="L-001",
        hojin_bango="1234567890123",
        principal=principal,
        originated_on=dt.date(2025, 4, 1),
        events=(*chain, *extra),
    )


def test_default_event_has_zero_repayment() -> None:
    # Backward compatibility: an event built without principal_repaid is 0.
    assert _event(LoanStatus.APPLIED).principal_repaid == 0


def test_outstanding_principal_declines_with_repayments() -> None:
    events = [
        _event(LoanStatus.APPLIED, days=0),
        _event(LoanStatus.UNDER_REVIEW, days=1),
        _event(LoanStatus.APPROVED, days=2),
        _event(LoanStatus.DISBURSED, days=3),
        _event(LoanStatus.PERFORMING, days=4),
        _repay(30_000_000, days=5),
        _repay(20_000_000, days=6),
    ]
    assert outstanding_principal(100_000_000, events) == 50_000_000


def test_outstanding_principal_clamps_at_zero() -> None:
    # Defensive: outstanding never goes negative.
    events = [_event(LoanStatus.APPLIED), _repay(0)]  # APPLIED carries 0
    assert outstanding_principal(10, events) == 10
    assert outstanding_principal(0, [_event(LoanStatus.APPLIED)]) == 0


def test_outstanding_principal_negative_original_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        outstanding_principal(-1, [_event(LoanStatus.APPLIED)])


def test_partial_repayment_self_loop_is_legal() -> None:
    loan = _performing_loan(_repay(40_000_000, days=5), _repay(10_000_000, days=6))
    # Status is unchanged (still performing); balance has declined.
    assert loan.status is LoanStatus.PERFORMING
    assert loan.outstanding == 50_000_000


def test_repayments_then_close() -> None:
    loan = _performing_loan(
        _repay(100_000_000, days=5),
        _event(LoanStatus.CLOSED, days=6),
    )
    assert loan.status is LoanStatus.CLOSED
    assert loan.outstanding == 0


def test_restructured_repayment_self_loop_is_legal() -> None:
    loan = _performing_loan(
        _event(LoanStatus.RESTRUCTURED, days=5),
        _repay(25_000_000, days=6, status=LoanStatus.RESTRUCTURED),
    )
    assert loan.status is LoanStatus.RESTRUCTURED
    assert loan.outstanding == 75_000_000


def test_repayment_exceeding_principal_rejected() -> None:
    with pytest.raises(ValidationError):
        _performing_loan(_repay(150_000_000, days=5))  # > 100M principal


def test_repayment_on_status_change_event_rejected() -> None:
    # A repayment may not ride a status-changing event (e.g. PERFORMING -> CLOSED).
    with pytest.raises(ValidationError):
        _performing_loan(
            LoanEvent(
                status=LoanStatus.CLOSED,
                at=_AT + dt.timedelta(days=5),
                actor="system",
                principal_repaid=50_000_000,
            )
        )


def test_negative_repayment_rejected() -> None:
    with pytest.raises(ValidationError):
        LoanEvent(
            status=LoanStatus.PERFORMING,
            at=_AT,
            actor="system",
            principal_repaid=-1,
        )


def test_close_proposed_after_repaying_to_zero() -> None:
    # The 完済 proposal keys off the DERIVED outstanding balance.
    loan = _performing_loan(_repay(100_000_000, days=5))
    assert loan.outstanding == 0
    assert proposed_servicing_transition(loan.status, loan.outstanding) is LoanStatus.CLOSED
    # Still has balance -> no close proposed.
    partial = _performing_loan(_repay(40_000_000, days=5))
    assert proposed_servicing_transition(partial.status, partial.outstanding) is None


def test_repayment_self_loops_are_servicing_not_gated() -> None:
    for pair in (
        (LoanStatus.PERFORMING, LoanStatus.PERFORMING),
        (LoanStatus.RESTRUCTURED, LoanStatus.RESTRUCTURED),
    ):
        assert pair in SERVICING_TRANSITIONS
        assert pair not in HITL_GATED_TRANSITIONS
        assert is_servicing_transition(*pair)


# --- amortization: outstanding_principal_for_state (the shared seam) --------


class _StateLike:
    """A minimal state-like object for the shared outstanding seam."""

    def __init__(
        self,
        lender_stakes: dict[str, int],
        loan_events: list[dict[str, object]],
    ) -> None:
        self.lender_stakes = lender_stakes
        self.loan_events = loan_events


def test_state_seam_equals_stakes_without_repayments() -> None:
    # Backward compatible: no repayments -> the old sum(lender_stakes) proxy.
    state = _StateLike(
        {"main_bank": 60_000_000, "sub_bank": 40_000_000},
        [
            e.model_dump(mode="json")
            for e in (
                _event(LoanStatus.APPLIED, days=0),
                _event(LoanStatus.UNDER_REVIEW, days=1),
                _event(LoanStatus.APPROVED, days=2),
                _event(LoanStatus.DISBURSED, days=3),
                _event(LoanStatus.PERFORMING, days=4),
            )
        ],
    )
    assert outstanding_principal_for_state(state) == 100_000_000


def test_state_seam_declines_with_repayments() -> None:
    events = [
        _event(LoanStatus.APPLIED, days=0),
        _event(LoanStatus.UNDER_REVIEW, days=1),
        _event(LoanStatus.APPROVED, days=2),
        _event(LoanStatus.DISBURSED, days=3),
        _event(LoanStatus.PERFORMING, days=4),
        _repay(30_000_000, days=5),
    ]
    state = _StateLike(
        {"main_bank": 100_000_000},
        [e.model_dump(mode="json") for e in events],
    )
    assert outstanding_principal_for_state(state) == 70_000_000


def test_state_seam_zero_without_stakes() -> None:
    assert outstanding_principal_for_state(_StateLike({}, [])) == 0


def test_state_seam_degrades_on_malformed_log() -> None:
    # A malformed log degrades to the repayment-free baseline (never raises).
    state = _StateLike({"main_bank": 50_000_000}, [{"bad": 1}])
    assert outstanding_principal_for_state(state) == 50_000_000


def test_state_seam_drives_a_declining_provision() -> None:
    # The payoff: the provision reserves against the real declining balance.
    full = _StateLike({"main_bank": 100_000_000}, [])
    base = outstanding_principal_for_state(full)
    after = outstanding_principal_for_state(
        _StateLike(
            {"main_bank": 100_000_000},
            [
                e.model_dump(mode="json")
                for e in (
                    _event(LoanStatus.APPLIED, days=0),
                    _event(LoanStatus.UNDER_REVIEW, days=1),
                    _event(LoanStatus.APPROVED, days=2),
                    _event(LoanStatus.DISBURSED, days=3),
                    _event(LoanStatus.PERFORMING, days=4),
                    _repay(40_000_000, days=5),
                )
            ],
        )
    )
    assert provision_amount(after, FsaClass.HATAN_KENENSAKI) < provision_amount(
        base, FsaClass.HATAN_KENENSAKI
    )


# --- amortization: ledger-first baseline via principal_disbursed ------------


def _disbursed(amount: int, *, days: int = 3) -> LoanEvent:
    """A DISBURSED event stamping the drawn principal onto the ledger."""
    return LoanEvent(
        status=LoanStatus.DISBURSED,
        at=_AT + dt.timedelta(days=days),
        actor="system",
        principal_disbursed=amount,
    )


def _ledger_chain(disbursed: int, *trailing: LoanEvent) -> list[dict[str, object]]:
    """APPLIED..DISBURSED(stamped) + trailing events, as JSON dicts."""
    head = (
        _event(LoanStatus.APPLIED, days=0),
        _event(LoanStatus.UNDER_REVIEW, days=1),
        _event(LoanStatus.APPROVED, days=2),
        _disbursed(disbursed, days=3),
    )
    return [e.model_dump(mode="json") for e in (*head, *trailing)]


def test_principal_disbursed_only_on_disbursed_event() -> None:
    # Stamping it on a non-DISBURSED event is rejected.
    with pytest.raises(ValidationError):
        Loan(
            loan_id="L-1",
            hojin_bango="1234567890123",
            principal=100_000_000,
            originated_on=dt.date(2025, 4, 1),
            events=(
                _event(LoanStatus.APPLIED, days=0),
                LoanEvent(
                    status=LoanStatus.UNDER_REVIEW,
                    at=_AT + dt.timedelta(days=1),
                    actor="system",
                    principal_disbursed=5,
                ),
            ),
        )


def test_seam_prefers_ledger_stamp_over_stakes() -> None:
    # No lender_stakes at all -> the balance still resolves from the stamp.
    state = _StateLike(
        {},
        _ledger_chain(
            80_000_000,
            _event(LoanStatus.PERFORMING, days=4),
            _repay(30_000_000, days=5, status=LoanStatus.PERFORMING),
        ),
    )
    assert outstanding_principal_for_state(state) == 50_000_000


def test_seam_stamp_wins_when_both_present() -> None:
    # The ledger stamp is authoritative over a (stale) stakes snapshot.
    state = _StateLike(
        {"main_bank": 999_000_000},
        _ledger_chain(80_000_000, _event(LoanStatus.PERFORMING, days=4)),
    )
    assert outstanding_principal_for_state(state) == 80_000_000


def test_seam_falls_back_to_stakes_without_a_stamp() -> None:
    # A pre-stamp facility (disbursed=0) uses the stakes baseline unchanged.
    state = _StateLike(
        {"main_bank": 60_000_000},
        [
            e.model_dump(mode="json")
            for e in (
                _event(LoanStatus.APPLIED, days=0),
                _event(LoanStatus.UNDER_REVIEW, days=1),
                _event(LoanStatus.APPROVED, days=2),
                _event(LoanStatus.DISBURSED, days=3),
                _event(LoanStatus.PERFORMING, days=4),
            )
        ],
    )
    assert outstanding_principal_for_state(state) == 60_000_000


# --- depth: loan-loss provisioning (貸倒引当金) --------------------------


def test_provision_rate_increases_with_distress() -> None:
    rates = [
        provision_rate_for(FsaClass.SEIJOSAKI),
        provision_rate_for(FsaClass.YOCHUISAKI),
        provision_rate_for(FsaClass.HATAN_KENENSAKI),
        provision_rate_for(FsaClass.JISSHITSU_HATANSAKI),
    ]
    assert rates == sorted(rates)
    assert provision_rate_for(FsaClass.HATANSAKI) == PROVISION_RATE_BANKRUPT


def test_special_attention_is_heavier_than_base_needs_attention() -> None:
    base = provision_rate_for(FsaClass.YOCHUISAKI)
    special = provision_rate_for(FsaClass.YOCHUISAKI, special_attention=True)
    assert base == PROVISION_RATE_NEEDS_ATTENTION
    assert special == PROVISION_RATE_SPECIAL_ATTENTION
    assert special > base


def test_special_attention_ignored_for_non_needs_attention() -> None:
    # The flag only means anything for 要注意先.
    assert provision_rate_for(FsaClass.SEIJOSAKI, special_attention=True) == provision_rate_for(
        FsaClass.SEIJOSAKI
    )
    assert provision_rate_for(FsaClass.HATANSAKI, special_attention=True) == provision_rate_for(
        FsaClass.HATANSAKI
    )


def test_provision_amount_is_integer_yen() -> None:
    amount = provision_amount(100_000_000, FsaClass.HATAN_KENENSAKI)
    assert isinstance(amount, int)
    assert amount == 70_000_000


def test_provision_amount_bankrupt_is_full_principal() -> None:
    assert provision_amount(50_000_000, FsaClass.HATANSAKI) == 50_000_000


def test_provision_amount_rounds_to_whole_yen() -> None:
    # 33,333,333 * 0.05 = 1,666,666.65 -> rounds to 1,666,667.
    assert provision_amount(33_333_333, FsaClass.YOCHUISAKI) == 1_666_667


def test_provision_amount_negative_outstanding_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        provision_amount(-1, FsaClass.SEIJOSAKI)
