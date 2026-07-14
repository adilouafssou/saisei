"""Tests for the loan-lifecycle wiring at the HITL approve path.

These cover the additive ``_loan_events`` side-record in
``turnaround_orchestrator``: an approved banker decision records the
FSA-implied 条件変更 / 管理回収 transition as a LoanEvent when (and only when) a
loan is attached and the implied transition is legal and HITL-gated.
"""

from __future__ import annotations

import datetime as dt

from app.backend.agents.turnaround_orchestrator import _loan_events
from app.backend.state import SaiseiState
from app.shared.models.classification import FsaClass
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def _events(*statuses: LoanStatus) -> list[dict[str, object]]:
    return [
        LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system").model_dump(mode="json")
        for i, s in enumerate(statuses)
    ]


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


def test_no_loan_attached_is_noop() -> None:
    state = _state(fsa_classification=FsaClass.YOCHUISAKI)
    assert _loan_events(state, response={}) == []


def test_no_fsa_classification_is_noop() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(
            LoanStatus.APPLIED,
            LoanStatus.UNDER_REVIEW,
            LoanStatus.APPROVED,
            LoanStatus.DISBURSED,
            LoanStatus.PERFORMING,
        ),
    )
    assert _loan_events(state, response={}) == []


def test_normal_class_records_no_transition() -> None:
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.SEIJOSAKI,
        loan_events=_events(
            LoanStatus.APPLIED,
            LoanStatus.UNDER_REVIEW,
            LoanStatus.APPROVED,
            LoanStatus.DISBURSED,
            LoanStatus.PERFORMING,
        ),
    )
    assert _loan_events(state, response={}) == []


def test_turnaround_class_records_restructured() -> None:
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.YOCHUISAKI,
        loan_events=_events(
            LoanStatus.APPLIED,
            LoanStatus.UNDER_REVIEW,
            LoanStatus.APPROVED,
            LoanStatus.DISBURSED,
            LoanStatus.PERFORMING,
        ),
    )
    out = _loan_events(state, response={"actor": "banker-7"})
    assert len(out) == 1
    assert out[0]["status"] == LoanStatus.RESTRUCTURED.value
    assert out[0]["actor"] == "banker-7"


def test_workout_class_records_workout() -> None:
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.HATANSAKI,
        loan_events=_events(
            LoanStatus.APPLIED,
            LoanStatus.UNDER_REVIEW,
            LoanStatus.APPROVED,
            LoanStatus.DISBURSED,
            LoanStatus.PERFORMING,
        ),
    )
    out = _loan_events(state, response={})
    assert len(out) == 1
    assert out[0]["status"] == LoanStatus.WORKOUT.value


def test_illegal_from_current_is_noop() -> None:
    # Already CLOSED (terminal): no transition is legal, so no event recorded.
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.HATANSAKI,
        loan_events=_events(
            LoanStatus.APPLIED,
            LoanStatus.UNDER_REVIEW,
            LoanStatus.APPROVED,
            LoanStatus.DISBURSED,
            LoanStatus.PERFORMING,
            LoanStatus.CLOSED,
        ),
    )
    assert _loan_events(state, response={}) == []
