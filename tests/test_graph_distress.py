"""End-to-end tests for the loan-distress graph (条件変更 / 償却 graph edge).

The distress mirror of tests/test_loan_origination_graph.py. Drive an attached
facility through the compiled distress graph with a MemorySaver, exercising the
HITL-gated distress moves as one record:

    PERFORMING (正常) -> RESTRUCTURED (条件変更)   [action='restructure']
    WORKOUT    (管理回収) -> WRITTEN_OFF (償却)     [action='writeoff']

Like origination (and UNLIKE servicing) the graph PAUSES at the distress
decision (interrupt); a Command(resume=...) carries the banker's proceed / abort.
The gated transition is recorded ONLY on proceed, by the existing restructure /
writeoff nodes; on abort the run ends with no transition. Fully offline
(MemorySaver; no audit/loan DSN).
"""

from __future__ import annotations

import datetime as dt

from app.backend.graph_distress import compile_distress_graph
from app.backend.state import SaiseiState
from app.shared.models.accounting import TrialBalance
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def _cfg(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _declining_history() -> list[dict[str, object]]:
    """A deteriorating 12-month history (mirrors the restructure node fixture).

    Serialised to JSON-safe dicts so it rides the MemorySaver checkpoint exactly
    as the HTTP surface would deliver it.
    """
    rows: list[dict[str, object]] = []
    for i in range(12):
        sales = 150_000_000 - i * 2_500_000
        cogs = int(sales * (0.80 + i * 0.005))
        rows.append(
            TrialBalance(
                period=dt.date(2025, 1, 1) + dt.timedelta(days=30 * i),
                uriage=sales,
                uriage_genka=cogs,
                hanbaihi=20_000_000,
            ).model_dump(mode="json")
        )
    return rows


def _performing_log() -> list[dict[str, object]]:
    """A facility log whose current status is PERFORMING (正常)."""
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


def _workout_log(disbursed: int = 500_000_000) -> list[dict[str, object]]:
    """A facility log whose current status is WORKOUT (管理回収)."""
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


def _events(app: CompiledStateGraph[SaiseiState], thread_id: str) -> list[LoanEvent]:
    values = app.get_state(_cfg(thread_id)).values
    return [LoanEvent.model_validate(e) for e in values.get("loan_events", [])]


def _start_restructure(app: CompiledStateGraph[SaiseiState], thread_id: str) -> None:
    app.invoke(  # type: ignore[call-overload]
        {
            "tdb_code": "1234567",
            "loan_id": "L-1",
            "loan_events": _performing_log(),
            "shisanhyo": _declining_history(),
            "distress_action": "restructure",
            "restructure_grace_months": 12,
            "restructure_rate_reduction_bps": 200,
        },
        config=_cfg(thread_id),
    )


def _start_writeoff(app: CompiledStateGraph[SaiseiState], thread_id: str) -> None:
    app.invoke(  # type: ignore[call-overload]
        {
            "tdb_code": "1234567",
            "loan_id": "L-1",
            "loan_events": _workout_log(),
            "distress_action": "writeoff",
        },
        config=_cfg(thread_id),
    )


# --- interrupt -------------------------------------------------------------


def test_pauses_at_the_distress_decision() -> None:
    """The graph runs to the distress interrupt and waits for the banker."""
    app = compile_distress_graph(checkpointer=MemorySaver())
    _start_restructure(app, "d-pause")
    snapshot = app.get_state(_cfg("d-pause"))
    # Paused at the HITL distress-decision node (interrupt -> non-empty next).
    assert snapshot.next == ("distress_hitl",)
    # No transition recorded yet -- the facility is still PERFORMING.
    assert current_status(_events(app, "d-pause")) is LoanStatus.PERFORMING


# --- proceed: restructure --------------------------------------------------


def test_proceed_restructure_records_the_gated_transition() -> None:
    """Banker proceed records PERFORMING -> RESTRUCTURED, authored by the banker."""
    app = compile_distress_graph(checkpointer=MemorySaver())
    _start_restructure(app, "d-restructure")
    app.invoke(
        Command(resume={"decision": "proceed", "actor": "banker-7"}),
        config=_cfg("d-restructure"),
    )

    events = _events(app, "d-restructure")
    assert current_status(events) is LoanStatus.RESTRUCTURED
    last = events[-1]
    assert last.status is LoanStatus.RESTRUCTURED
    # A restructure is a banker-authority credit judgement, NOT a 'system' fact.
    assert last.actor != "system"
    # The advisory verdict is surfaced for the record.
    values = app.get_state(_cfg("d-restructure")).values
    assert values["restructure_curing"]["band"] in {
        "self_curing",
        "marginal",
        "non_curing",
    }
    # The graph completed.
    assert app.get_state(_cfg("d-restructure")).next == ()


# --- proceed: writeoff -----------------------------------------------------


def test_proceed_writeoff_records_the_terminal_transition() -> None:
    """Banker proceed records WORKOUT -> WRITTEN_OFF (償却), authored by the banker."""
    app = compile_distress_graph(checkpointer=MemorySaver())
    _start_writeoff(app, "d-writeoff")
    app.invoke(
        Command(resume={"decision": "proceed", "actor": "banker-7"}),
        config=_cfg("d-writeoff"),
    )

    events = _events(app, "d-writeoff")
    assert current_status(events) is LoanStatus.WRITTEN_OFF
    assert events[-1].actor != "system"
    # The deterministic charged-off amount is surfaced (full outstanding, 500M).
    values = app.get_state(_cfg("d-writeoff")).values
    assert values["loan_writeoff"]["written_off_amount"] == 500_000_000
    assert values["loan_writeoff"]["recorded"] is True
    assert app.get_state(_cfg("d-writeoff")).next == ()


# --- abort -----------------------------------------------------------------


def test_abort_records_no_transition() -> None:
    """Banker abort ends the run with NO loan transition recorded."""
    app = compile_distress_graph(checkpointer=MemorySaver())
    _start_restructure(app, "d-abort")
    app.invoke(Command(resume={"decision": "abort"}), config=_cfg("d-abort"))

    # The facility stays PERFORMING -- abort routes straight to END.
    assert current_status(_events(app, "d-abort")) is LoanStatus.PERFORMING
    assert app.get_state(_cfg("d-abort")).next == ()


def test_invalid_decision_aborts_with_an_error_and_no_transition() -> None:
    """An unrecognised decision records an error and routes to END (no move)."""
    app = compile_distress_graph(checkpointer=MemorySaver())
    _start_writeoff(app, "d-bad")
    app.invoke(Command(resume={"decision": "maybe"}), config=_cfg("d-bad"))

    values = app.get_state(_cfg("d-bad")).values
    assert any("Invalid distress decision" in e for e in values["errors"])
    # No transition recorded; the facility stays WORKOUT.
    assert current_status(_events(app, "d-bad")) is LoanStatus.WORKOUT
    assert app.get_state(_cfg("d-bad")).next == ()


# --- routing ---------------------------------------------------------------


def test_writeoff_action_does_not_restructure_a_workout_facility() -> None:
    """A 'writeoff' action routes to the write-off node, never the restructure one.

    A WORKOUT facility cannot legally restructure; proceeding with 'writeoff'
    must charge it off, not attempt a 条件変更. Pins the action-based routing.
    """
    app = compile_distress_graph(checkpointer=MemorySaver())
    _start_writeoff(app, "d-route")
    app.invoke(
        Command(resume={"decision": "proceed", "actor": "banker-7"}),
        config=_cfg("d-route"),
    )
    statuses = [e.status for e in _events(app, "d-route")]
    assert LoanStatus.WRITTEN_OFF in statuses
    assert LoanStatus.RESTRUCTURED not in statuses


# --- determinism -----------------------------------------------------------


def test_advisory_verdict_is_deterministic_across_runs() -> None:
    """Two identical proceed runs produce the same advisory self-curing verdict."""
    app = compile_distress_graph(checkpointer=MemorySaver())
    _start_restructure(app, "d-det-a")
    app.invoke(
        Command(resume={"decision": "proceed", "actor": "banker-7"}),
        config=_cfg("d-det-a"),
    )
    _start_restructure(app, "d-det-b")
    app.invoke(
        Command(resume={"decision": "proceed", "actor": "banker-7"}),
        config=_cfg("d-det-b"),
    )
    a = app.get_state(_cfg("d-det-a")).values["restructure_curing"]
    b = app.get_state(_cfg("d-det-b")).values["restructure_curing"]
    assert a == b
