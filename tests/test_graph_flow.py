"""End-to-end tests for the Saisei graph flow.

Uses an in-memory checkpointer so the HITL interrupt/resume cycle can be
exercised without a live Postgres instance.
"""

from __future__ import annotations

from typing import cast

from app.backend.graph import (
    build_graph,
    route_after_classification,
    route_after_negotiation,
)
from app.backend.nodes.workout import workout_node
from app.backend.state import FsaClass, NegotiationDecision, SaiseiState, Strategy
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "test-thread"}}


def _compiled() -> CompiledStateGraph[SaiseiState]:
    return build_graph().compile(checkpointer=MemorySaver())


def test_route_after_classification() -> None:
    normal = SaiseiState(tdb_code="1234567", fsa_classification=FsaClass.SEIJOSAKI)
    needs_attention = SaiseiState(tdb_code="1234567", fsa_classification=FsaClass.YOCHUISAKI)
    in_danger = SaiseiState(tdb_code="1234567", fsa_classification=FsaClass.HATAN_KENENSAKI)
    de_facto = SaiseiState(tdb_code="1234567", fsa_classification=FsaClass.JISSHITSU_HATANSAKI)
    bankrupt = SaiseiState(tdb_code="1234567", fsa_classification=FsaClass.HATANSAKI)
    assert route_after_classification(normal) == "__end__"
    assert route_after_classification(needs_attention) == "strategist"
    assert route_after_classification(in_danger) == "strategist"
    assert route_after_classification(de_facto) == "workout"
    assert route_after_classification(bankrupt) == "workout"


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
        cast(
            "SaiseiState",
            {
                "tdb_code": "1234567",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            },
        ),
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
        cast(
            "SaiseiState",
            {
                "tdb_code": "1234567",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            },
        ),
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
    config: RunnableConfig = {"configurable": {"thread_id": "test-needs-human"}}
    # Do NOT set the commitment flags (both default False).
    app.invoke(cast("SaiseiState", {"tdb_code": "1234567"}), config=config)
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
    config: RunnableConfig = {"configurable": {"thread_id": "test-revise-clears-deadlock"}}

    # 1) Start with NO commitment flags -> pauses at HITL with needs_human.
    app.invoke(cast("SaiseiState", {"tdb_code": "1234567"}), config=config)
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
    app.invoke(Command(resume={"decision": "approve", "strategy_index": 0}), config=config)
    snapshot = app.get_state(config)
    assert not snapshot.next  # graph completed
    draft = snapshot.values["keikakusho_draft"]
    assert draft and "\u7d4c\u55b6\u6539\u5584\u8a08\u753b\u66f8" in draft


# ---------------------------------------------------------------------------
# Workout node (実質破綻先 / 破綻先 routing)
# ---------------------------------------------------------------------------


def test_workout_node_records_handoff_for_de_facto_bankrupt() -> None:
    """workout_node must set workout_handoff for 実質破綻先."""
    state = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
        ews_score=90.0,
        net_worth=-5_000_000,
        is_insolvent=None,
    )
    result = workout_node(state)
    assert "workout_handoff" in result
    handoff = result["workout_handoff"]
    assert handoff is not None
    assert "実質破綻先" in handoff
    assert "WORKOUT HANDOFF" in handoff


def test_workout_handoff_formats_yen_consistently() -> None:
    """Money in the handoff must use format_jpy (¥ + separators), never raw ints.

    The handoff is the auditable record handed to the special-assets team, so it
    must match the product's yen formatting everywhere else. A negative net worth
    renders as -¥5,000,000; the old raw form ('-5000000 円') must be gone.
    """
    state = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
        ews_score=90.0,
        net_worth=-5_000_000,
        working_capital_gap=-3_200_000,
        is_insolvent=True,
    )
    handoff = workout_node(state)["workout_handoff"]
    assert "-\u00a55,000,000" in handoff  # net worth, formatted
    assert "-\u00a53,200,000" in handoff  # working-capital gap, formatted
    assert "5000000 \u5186" not in handoff  # old raw form is gone
    assert "3200000 \u5186" not in handoff


def test_workout_handoff_handles_unassessed_figures() -> None:
    """With no net worth / gap, the handoff shows the 未評価 placeholders."""
    state = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.HATANSAKI,
        ews_score=88.0,
        net_worth=None,
        working_capital_gap=None,
        is_insolvent=True,
    )
    handoff = workout_node(state)["workout_handoff"]
    assert "\u7d14\u8cc7\u7523: \u672a\u8a55\u4fa1" in handoff
    assert "\u8cc7\u91d1\u7e70\u308a\u30ae\u30e3\u30c3\u30d7: \u672a\u8a55\u4fa1" in handoff


def test_workout_node_records_handoff_for_bankrupt() -> None:
    """workout_node must set workout_handoff for 破綻先."""
    state = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.HATANSAKI,
        ews_score=80.0,
        net_worth=-10_000_000,
        is_insolvent=True,
    )
    result = workout_node(state)
    handoff = result["workout_handoff"]
    assert handoff is not None
    assert "破綻先" in handoff


def test_workout_node_is_deterministic() -> None:
    """workout_node must produce identical output for identical input."""
    state = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
        ews_score=88.0,
        net_worth=-1_000_000,
        is_insolvent=True,
    )
    result_a = workout_node(state)
    result_b = workout_node(state)
    assert result_a == result_b, "workout_node must be deterministic"


def test_route_after_classification_workout_path() -> None:
    """実質破綻先 and 破綻先 must route to 'workout', not 'strategist'."""
    de_facto = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
    )
    bankrupt = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.HATANSAKI,
    )
    assert route_after_classification(de_facto) == "workout"
    assert route_after_classification(bankrupt) == "workout"
    # Confirm turnaround bands still route correctly.
    needs_attention = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.YOCHUISAKI,
    )
    in_danger = SaiseiState(
        tdb_code="1234567",
        fsa_classification=FsaClass.HATAN_KENENSAKI,
    )
    assert route_after_classification(needs_attention) == "strategist"
    assert route_after_classification(in_danger) == "strategist"
