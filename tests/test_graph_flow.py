"""End-to-end tests for the Saisei graph flow.

Uses an in-memory checkpointer so the HITL interrupt/resume cycle can be
exercised without a live Postgres instance.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.backend.graph import (
    build_graph,
    route_after_classification,
    route_after_negotiation,
)
from app.backend.state import FsaClass, NegotiationDecision, SaiseiState, Strategy

_CONFIG = {"configurable": {"thread_id": "test-thread"}}


def _compiled():  # type: ignore[no-untyped-def]
    return build_graph().compile(checkpointer=MemorySaver())


def test_route_after_classification() -> None:
    joyo = SaiseiState(tdb_code="1234567", fsa_classification=FsaClass.JOYO)
    bad = SaiseiState(tdb_code="1234567", fsa_classification=FsaClass.YUKYO_GUCHI)
    assert route_after_classification(joyo) == "__end__"
    assert route_after_classification(bad) == "strategist"


def test_route_after_negotiation() -> None:
    approve = SaiseiState(tdb_code="1", negotiation_decision=NegotiationDecision.APPROVE)
    revise = SaiseiState(tdb_code="1", negotiation_decision=NegotiationDecision.REVISE)
    reject = SaiseiState(tdb_code="1", negotiation_decision=NegotiationDecision.REJECT)
    assert route_after_negotiation(approve) == "plan_writer"
    assert route_after_negotiation(revise) == "strategist"
    assert route_after_negotiation(reject) == "__end__"


def test_graph_pauses_at_interrupt() -> None:
    app = _compiled()
    # Set commitment flags so the main_bank critic PASSes and the graph reaches HITL.
    # Without these flags (both default False), main_bank always FAILs and the graph
    # exhausts all revision cycles before reaching the HITL interrupt.
    app.invoke(
        {
            "tdb_code": "1234567",
            "yakuin_hoshu_cut": True,
            "personal_asset_disposal": True,
        },
        config=_CONFIG,
    )
    snapshot = app.get_state(_CONFIG)
    # The deteriorating Aichi SME must reach the HITL pause.
    assert snapshot.next
    assert snapshot.values["fsa_classification"].requires_turnaround
    assert snapshot.values["proposed_strategies"]
    # PART 2: Hosho Kaijo assessment must have run.
    assert snapshot.values["hosho_kaijo_score"] is not None
    assert snapshot.values["hosho_kaijo_conditions"] is not None
    assert snapshot.values["succession_ready"] is not None
    # PART 3: Creditor meeting must have run.
    assert snapshot.values["negotiation_status"] in ("approved", "rejected", "pending")


def test_graph_resume_approves_and_writes_keikakusho() -> None:
    app = _compiled()
    # Set commitment flags so the main_bank critic PASSes and the graph reaches HITL.
    app.invoke(
        {
            "tdb_code": "1234567",
            "yakuin_hoshu_cut": True,
            "personal_asset_disposal": True,
        },
        config=_CONFIG,
    )
    app.invoke(Command(resume={"decision": "approve", "strategy_index": 0}), config=_CONFIG)
    snapshot = app.get_state(_CONFIG)
    assert not snapshot.next  # graph completed
    approved = snapshot.values["approved_strategy"]
    assert isinstance(approved, Strategy)
    draft = snapshot.values["keikakusho_draft"]
    assert draft and "経営改善計画書" in draft


def test_graph_reaches_hitl_with_default_commitment_flags() -> None:
    """Regression: default flags must NOT deadlock into pre-HITL escalation.

    Previously, with yakuin_hoshu_cut/personal_asset_disposal defaulting to
    False, the main_bank critic always FAILed and the graph looped the
    strategist MAX_REVISION_CYCLES times, escalating to END before the banker
    was ever consulted.  The lead_arranger now emits 'needs_human' for
    banker-only blockers and the graph routes to HITL so the banker can set the
    commitment flags.
    """
    app = _compiled()
    config = {"configurable": {"thread_id": "test-needs-human"}}
    # Do NOT set the commitment flags (both default False).
    app.invoke({"tdb_code": "1234567"}, config=config)
    snapshot = app.get_state(config)
    # The graph must PAUSE at HITL, not have terminated at escalation.
    assert snapshot.next, "graph must reach the HITL interrupt, not escalate to END"
    assert snapshot.values["negotiation_status"] in ("needs_human", "approved")


def test_hitl_revise_persists_commitment_flags_and_clears_deadlock() -> None:
    """Regression: a banker who confirms commitments via revise must clear the
    needs_human deadlock.

    Reproduces the original bug: starting with default (False) commitment flags,
    the meeting consolidates to 'needs_human' (banker-only blockers) and the
    graph pauses at HITL.  The banker then resumes with decision='revise' AND
    the commitment flags set True.  hitl_negotiation_node must persist those
    flags into state so the next critic round PASSes the main_bank gate instead
    of looping back to 'needs_human' forever.  After a final approve the
    Keikakusho is written.
    """
    app = _compiled()
    config = {"configurable": {"thread_id": "test-revise-clears-deadlock"}}

    # 1) Start with NO commitment flags -> pauses at HITL with needs_human.
    app.invoke({"tdb_code": "1234567"}, config=config)
    snapshot = app.get_state(config)
    assert snapshot.next, "graph must pause at the HITL interrupt"
    assert snapshot.values["negotiation_status"] == "needs_human"
    assert snapshot.values["yakuin_hoshu_cut"] is False
    assert snapshot.values["personal_asset_disposal"] is False

    # 2) Banker revises AND confirms the banker-only commitments.
    app.invoke(
        Command(
            resume={
                "decision": "revise",
                "revision_note": "役員報酬削減・個人資産処分を確認しました。",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            }
        ),
        config=config,
    )
    snapshot = app.get_state(config)
    # Flags must now be persisted in state (this is the core fix).
    assert snapshot.values["yakuin_hoshu_cut"] is True
    assert snapshot.values["personal_asset_disposal"] is True
    # With the banker-only blockers cleared, the meeting must approve and the
    # graph must pause again at HITL (not loop back to needs_human / escalate).
    assert snapshot.next, "graph must pause at HITL again, not escalate"
    assert snapshot.values["negotiation_status"] == "approved"

    # 3) Final approve writes the Keikakusho.
    app.invoke(
        Command(resume={"decision": "approve", "strategy_index": 0}), config=config
    )
    snapshot = app.get_state(config)
    assert not snapshot.next  # graph completed
    draft = snapshot.values["keikakusho_draft"]
    assert draft and "\u7d4c\u55b6\u6539\u5584\u8a08\u753b\u66f8" in draft
