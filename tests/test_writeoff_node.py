"""Tests for the HITL-gated loan write-off closure node (償却 terminal).

The terminal of the distress arc. Verifies, fully offline:

- the deterministic charged-off amount (償却額) is the full outstanding principal
  at the bankrupt-class 100% loss, surfaced on ``loan_writeoff``;
- the HITL-gated WORKOUT -> WRITTEN_OFF transition is recorded when legal and
  gated, authored by the banker (NOT 'system');
- a non-WORKOUT status (e.g. PERFORMING) records no transition;
- a no-loan run records no transition but still surfaces the (zero) record;
- the node is read-only on the snapshot and deterministic.

The charged-off amount is pinned to the yen: a bankrupt-class facility reserves
at PROVISION_RATE_BANKRUPT (1.0), so the write-off equals the full outstanding.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.writeoff import writeoff_node
from app.backend.state import SaiseiState
from app.shared.models.classification import FsaClass
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def _workout_log(disbursed: int = 500_000_000) -> list[dict[str, object]]:
    """A facility log whose current status is WORKOUT (管理回収).

    APPLIED -> UNDER_REVIEW -> APPROVED -> DISBURSED (stamps the principal
    baseline) -> PERFORMING -> WORKOUT. With no repayments the outstanding
    principal is the full disbursed amount.
    """
    chain = [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
        LoanStatus.PERFORMING,
        LoanStatus.WORKOUT,
    ]
    events: list[dict[str, object]] = []
    for status in chain:
        kw: dict[str, object] = {"status": status, "at": _AT, "actor": "system"}
        if status is LoanStatus.DISBURSED:
            kw["principal_disbursed"] = disbursed
        events.append(LoanEvent(**kw).model_dump(mode="json"))
    return events


def _performing_log() -> list[dict[str, object]]:
    """A facility log whose current status is PERFORMING (cannot write off)."""
    chain = [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
        LoanStatus.PERFORMING,
    ]
    events: list[dict[str, object]] = []
    for status in chain:
        kw: dict[str, object] = {"status": status, "at": _AT, "actor": "system"}
        if status is LoanStatus.DISBURSED:
            kw["principal_disbursed"] = 500_000_000
        events.append(LoanEvent(**kw).model_dump(mode="json"))
    return events


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


# --- charged-off amount ----------------------------------------------------


def test_written_off_amount_is_full_outstanding_at_bankrupt_loss() -> None:
    # Bankrupt class reserves at 1.0, so the 償却額 == full outstanding (500M).
    state = _state(
        loan_id="L-1",
        loan_events=_workout_log(500_000_000),
        fsa_classification=FsaClass.HATANSAKI,
    )
    rec = writeoff_node(state)["loan_writeoff"]
    assert rec["written_off_amount"] == 500_000_000
    assert rec["recorded"] is True


def test_amount_defaults_to_bankrupt_class_without_classification() -> None:
    # A facility in WORKOUT with no FSA class on state still charges off the full
    # outstanding (the node falls back to the bankrupt class, ratio 1.0).
    state = _state(loan_id="L-1", loan_events=_workout_log(300_000_000))
    rec = writeoff_node(state)["loan_writeoff"]
    assert rec["written_off_amount"] == 300_000_000


def test_no_outstanding_yields_zero_amount() -> None:
    # A WORKOUT facility with no principal baseline -> amount omitted, not guessed.
    log = [
        LoanEvent(status=s, at=_AT, actor="system").model_dump(mode="json")
        for s in (
            LoanStatus.APPLIED,
            LoanStatus.UNDER_REVIEW,
            LoanStatus.APPROVED,
            LoanStatus.DISBURSED,
            LoanStatus.PERFORMING,
            LoanStatus.WORKOUT,
        )
    ]
    state = _state(loan_id="L-1", loan_events=log)
    rec = writeoff_node(state)["loan_writeoff"]
    assert rec["written_off_amount"] == 0
    assert rec["written_off_amount_formatted"] is None


# --- HITL-gated transition -------------------------------------------------


def test_records_gated_transition_authored_by_the_banker() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_workout_log(),
        fsa_classification=FsaClass.HATANSAKI,
    )
    events = writeoff_node(state)["loan_events"]
    assert len(events) == 1
    assert events[0]["status"] == LoanStatus.WRITTEN_OFF.value
    # A write-off is a banker-authority credit judgement, NOT a 'system' fact.
    assert events[0]["actor"] != "system"


def test_non_workout_status_records_no_transition() -> None:
    # PERFORMING cannot legally transition to WRITTEN_OFF.
    state = _state(
        loan_id="L-1",
        loan_events=_performing_log(),
        fsa_classification=FsaClass.HATANSAKI,
    )
    assert writeoff_node(state)["loan_events"] == []


def test_no_loan_attached_records_no_transition() -> None:
    state = _state(fsa_classification=FsaClass.HATANSAKI)
    out = writeoff_node(state)
    assert out["loan_events"] == []
    assert out["loan_writeoff"]["recorded"] is False


# --- read-only / determinism ----------------------------------------------


def test_node_does_not_mutate_state() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_workout_log(),
        fsa_classification=FsaClass.HATANSAKI,
    )
    before = state.model_dump(mode="json")
    writeoff_node(state)
    assert state.model_dump(mode="json") == before


def test_node_is_deterministic() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_workout_log(),
        fsa_classification=FsaClass.HATANSAKI,
    )
    a = writeoff_node(state)["loan_writeoff"]
    b = writeoff_node(state)["loan_writeoff"]
    assert a == b
