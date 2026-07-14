"""End-to-end tests for the loan-origination graph (融資組成 graph edge).

Drive a new facility application through the compiled origination graph with a
MemorySaver, exercising the full front of the lifecycle as one record:

    APPLIED → UNDER_REVIEW → {APPROVED → DISBURSED | DECLINED}

The graph pauses at the 稟議 credit decision (interrupt); a Command(resume=...)
carries the banker's 承認 / 謝絶. Fully offline (MemorySaver; no audit/loan DSN).
"""

from __future__ import annotations

from app.backend.graph_origination import compile_origination_graph
from app.backend.state import SaiseiState
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

# Aichi fixture: TDB score 41 (< the origination approve floor) by default, but
# the origination node reads state.tdb_score, which we set explicitly per test
# so the recommendation is deterministic regardless of the fixture.
_TDB = "1234567"


def _cfg(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _start(app: CompiledStateGraph[SaiseiState], thread_id: str, *, tdb_score: int) -> None:
    app.invoke(  # type: ignore[call-overload]
        {"tdb_code": _TDB, "tdb_score": tdb_score},
        config=_cfg(thread_id),
    )


def _events(app: CompiledStateGraph[SaiseiState], thread_id: str) -> list[LoanEvent]:
    values = app.get_state(_cfg(thread_id)).values
    return [LoanEvent.model_validate(e) for e in values.get("loan_events", [])]


def test_pauses_at_the_credit_decision() -> None:
    """The graph runs to the 稟議 interrupt and waits for the banker."""
    app = compile_origination_graph(checkpointer=MemorySaver())
    _start(app, "orig-pause", tdb_score=80)
    snapshot = app.get_state(_cfg("orig-pause"))
    # Paused at the HITL credit-decision node (interrupt -> non-empty next).
    assert snapshot.next == ("origination_hitl",)
    # The advisory recommendation is on state for the banker to review.
    rec = snapshot.values["origination_recommendation"]
    assert rec["recommendation"] == "approve"
    assert rec["grounded"] is True
    # Up to the pause the facility is UNDER_REVIEW (the credit decision is gated).
    assert current_status(_events(app, "orig-pause")) is LoanStatus.UNDER_REVIEW


def test_approve_path_disburses() -> None:
    """Banker 承認 records APPROVED then the deterministic APPROVED → DISBURSED."""
    app = compile_origination_graph(checkpointer=MemorySaver())
    _start(app, "orig-approve", tdb_score=80)
    app.invoke(Command(resume={"decision": "approve"}), config=_cfg("orig-approve"))

    events = _events(app, "orig-approve")
    statuses = [e.status for e in events]
    assert statuses == [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
    ]
    assert current_status(events) is LoanStatus.DISBURSED
    # The graph completed (no pending node).
    assert app.get_state(_cfg("orig-approve")).next == ()


def test_decline_path_ends_at_declined() -> None:
    """Banker 謝絖 records DECLINED (terminal) and never disburses."""
    app = compile_origination_graph(checkpointer=MemorySaver())
    _start(app, "orig-decline", tdb_score=80)
    app.invoke(Command(resume={"decision": "decline"}), config=_cfg("orig-decline"))

    events = _events(app, "orig-decline")
    statuses = [e.status for e in events]
    assert statuses == [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.DECLINED,
    ]
    assert current_status(events) is LoanStatus.DECLINED
    assert LoanStatus.DISBURSED not in statuses
    assert app.get_state(_cfg("orig-decline")).next == ()


def test_banker_can_approve_against_a_decline_recommendation() -> None:
    """Human authority: the banker may approve even when the advice is DECLINE.

    A weak applicant (score below the floor) is RECOMMENDED decline, but the
    banker holds the authority — a 承認 still drives APPROVED → DISBURSED. The
    recommendation informs; it never decides.
    """
    app = compile_origination_graph(checkpointer=MemorySaver())
    _start(app, "orig-override", tdb_score=10)
    # Advisory recommendation is decline...
    assert (
        app.get_state(_cfg("orig-override")).values["origination_recommendation"]["recommendation"]
        == "decline"
    )
    # ...but the banker approves.
    app.invoke(Command(resume={"decision": "approve"}), config=_cfg("orig-override"))
    assert current_status(_events(app, "orig-override")) is LoanStatus.DISBURSED


def test_invalid_decision_records_an_error_and_no_transition() -> None:
    """An unrecognised decision is rejected without recording a credit event."""
    app = compile_origination_graph(checkpointer=MemorySaver())
    _start(app, "orig-bad", tdb_score=80)
    app.invoke(Command(resume={"decision": "maybe"}), config=_cfg("orig-bad"))

    values = app.get_state(_cfg("orig-bad")).values
    assert any("Invalid origination decision" in e for e in values["errors"])
    # No credit transition was recorded; the facility stays UNDER_REVIEW.
    assert current_status(_events(app, "orig-bad")) is LoanStatus.UNDER_REVIEW


# ---------------------------------------------------------------------------
# Intake data-load: the graph resolves the applicant from the TDB code alone
# ---------------------------------------------------------------------------


def test_intake_resolves_applicant_when_only_the_tdb_code_is_given() -> None:
    """Started from just a TDB code, intake loads the applicant's real signals.

    The front-of-lifecycle data-load: with no caller-supplied score, intake
    resolves the applicant on the provider seam, so the 稟議 recommendation
    reflects the real TDB score (aichi_manufacturer: 41, below the approve
    floor -> decline) instead of collapsing to a no-score default. The profile,
    score, and financials all land on state, and the loan id is keyed on the
    resolved 13-digit Hojin Bango.
    """
    app = compile_origination_graph(checkpointer=MemorySaver())
    # Only the TDB code -- no tdb_score, no profile, no shisanhyo.
    app.invoke({"tdb_code": _TDB}, config=_cfg("orig-resolve"))  # type: ignore[call-overload]

    values = app.get_state(_cfg("orig-resolve")).values
    # Applicant resolved on the shared data seam.
    assert values["company_profile"] is not None
    assert values["tdb_score"] == 41
    assert values["shisanhyo"], "financials must be loaded for the ceiling"
    # The recommendation reflects the resolved sub-floor score.
    rec = values["origination_recommendation"]
    assert rec["recommendation"] == "decline"
    assert rec["grounded"] is True
    # Loan keyed on the resolved Hojin Bango (L-<hojin_bango>), not the TDB code.
    assert values["loan_id"] == f"L-{values['company_profile']['hojin_bango']}"
    assert current_status(_events(app, "orig-resolve")) is LoanStatus.UNDER_REVIEW


def test_caller_supplied_score_is_not_overwritten_by_intake() -> None:
    """A caller-supplied tdb_score wins over the resolved one (per-field).

    Intake still loads the profile / financials it is missing, but never clobbers
    a value the caller put on the initial invoke -- so a test (or an upstream
    pre-screen) that drives a specific score keeps it. Here an explicit
    above-floor score flips the aichi applicant's recommendation to approve.
    """
    app = compile_origination_graph(checkpointer=MemorySaver())
    app.invoke(  # type: ignore[call-overload]
        {"tdb_code": _TDB, "tdb_score": 80}, config=_cfg("orig-keep")
    )

    values = app.get_state(_cfg("orig-keep")).values
    assert values["tdb_score"] == 80  # caller value preserved
    assert values["company_profile"] is not None  # still enriched
    assert values["origination_recommendation"]["recommendation"] == "approve"
