"""Tests for the loan-lifecycle wiring on the WORKOUT (terminal) path.

These cover the additive ``_loan_workout_event`` side-record in ``workout``:
the deterministic workout routing records the FSA-implied 管理回収 (WORKOUT)
transition as a LoanEvent when (and only when) a loan is attached and the
implied transition is legal from the loan's current status.

Unlike the HITL approve path, this transition is authored by the classifier
(``"system"``): a workout handoff is the deterministic terminal decision
mandated by ``fsa_classification.requires_workout``, with no banker negotiation.

The end-to-end test drives a genuinely-insolvent borrower through the compiled
graph and asserts the persisted loan log ends at WORKOUT -- the regression this
bridge fixes (the facility previously stayed stuck at PERFORMING).
"""

from __future__ import annotations

import datetime as dt
from typing import cast

from app.backend.graph import build_graph
from app.backend.nodes.workout import _loan_workout_event, workout_node
from app.backend.state import SaiseiState
from app.shared.models.classification import FsaClass
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)

_PERFORMING_CHAIN = (
    LoanStatus.APPLIED,
    LoanStatus.UNDER_REVIEW,
    LoanStatus.APPROVED,
    LoanStatus.DISBURSED,
    LoanStatus.PERFORMING,
)


def _events(*statuses: LoanStatus) -> list[dict[str, object]]:
    return [
        LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system").model_dump(mode="json")
        for i, s in enumerate(statuses)
    ]


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


# ---------------------------------------------------------------------------
# Unit: _loan_workout_event
# ---------------------------------------------------------------------------


def test_no_loan_attached_is_noop() -> None:
    state = _state(fsa_classification=FsaClass.JISSHITSU_HATANSAKI)
    assert _loan_workout_event(state) == []


def test_no_fsa_classification_is_noop() -> None:
    state = _state(loan_id="L-1", loan_events=_events(*_PERFORMING_CHAIN))
    assert _loan_workout_event(state) == []


def test_empty_log_is_noop() -> None:
    state = _state(loan_id="L-1", fsa_classification=FsaClass.HATANSAKI)
    assert _loan_workout_event(state) == []


def test_de_facto_bankrupt_records_workout() -> None:
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
        loan_events=_events(*_PERFORMING_CHAIN),
    )
    out = _loan_workout_event(state)
    assert len(out) == 1
    assert out[0]["status"] == LoanStatus.WORKOUT.value
    # Deterministic, classifier-authored (not a banker).
    assert out[0]["actor"] == "system"


def test_bankrupt_records_workout() -> None:
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.HATANSAKI,
        loan_events=_events(*_PERFORMING_CHAIN),
    )
    out = _loan_workout_event(state)
    assert len(out) == 1
    assert out[0]["status"] == LoanStatus.WORKOUT.value


def test_records_workout_from_restructured() -> None:
    # A facility already 条件変更 (RESTRUCTURED) may still legally move to WORKOUT.
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
        loan_events=_events(*_PERFORMING_CHAIN, LoanStatus.RESTRUCTURED),
    )
    out = _loan_workout_event(state)
    assert len(out) == 1
    assert out[0]["status"] == LoanStatus.WORKOUT.value


def test_illegal_from_terminal_is_noop() -> None:
    # Already WRITTEN_OFF (terminal): no transition is legal.
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.HATANSAKI,
        loan_events=_events(*_PERFORMING_CHAIN, LoanStatus.WORKOUT, LoanStatus.WRITTEN_OFF),
    )
    assert _loan_workout_event(state) == []


def test_already_in_workout_is_noop() -> None:
    # Current status is WORKOUT; WORKOUT -> WORKOUT is not a legal self-loop.
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.HATANSAKI,
        loan_events=_events(*_PERFORMING_CHAIN, LoanStatus.WORKOUT),
    )
    assert _loan_workout_event(state) == []


def test_workout_node_appends_loan_event_to_return() -> None:
    state = _state(
        loan_id="L-1",
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
        loan_events=_events(*_PERFORMING_CHAIN),
        net_worth=-5_000_000,
    )
    out = workout_node(state)
    # Handoff text is unchanged / still present.
    assert "WORKOUT HANDOFF" in out["workout_handoff"]
    # And the loan transition is recorded on the append-only channel.
    assert len(out["loan_events"]) == 1
    assert out["loan_events"][0]["status"] == LoanStatus.WORKOUT.value


def test_workout_node_noop_loan_event_without_loan() -> None:
    state = _state(fsa_classification=FsaClass.HATANSAKI, net_worth=-1)
    out = workout_node(state)
    assert out["loan_events"] == []


# ---------------------------------------------------------------------------
# End-to-end: insolvent borrower's persisted loan log ends at WORKOUT
# ---------------------------------------------------------------------------


def _cfg(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def test_insolvent_run_persists_workout_transition() -> None:
    """A net_worth<0 borrower routes to workout; its loan log must end at WORKOUT.

    This is the regression the bridge fixes: previously the loan facility for a
    bankrupt borrower stayed stuck at PERFORMING because the workout path never
    recorded the FSA-implied 管理回収 transition.
    """
    app = build_graph().compile(checkpointer=MemorySaver())
    cfg = _cfg("workout-bridge-e2e")
    app.invoke(
        cast("SaiseiState", {"tdb_code": "1234567", "net_worth": -5_000_000}),
        config=cfg,
    )
    values = app.get_state(cfg).values

    # Routed to workout (terminal) and recorded the handoff.
    assert values["fsa_classification"] is FsaClass.JISSHITSU_HATANSAKI
    assert values["workout_handoff"] is not None

    # The loan facility was attached at intake (lender stakes present in the
    # fixture) and its derived current status is now WORKOUT, not PERFORMING.
    loan_events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    assert loan_events, "expected an attached loan log for this borrower"
    assert current_status(loan_events) is LoanStatus.WORKOUT
