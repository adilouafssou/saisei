"""LangGraph builder for the Saisei turnaround graph.

Wires the nodes into a ``StateGraph`` with:

* A linear assessment path: intake -> ews -> macro -> classifier -> keieisha_hosho.
* A conditional edge on ``fsa_classification``:
  - Joyo (Normal) → END (monitor-only).
  - Yoi Kanri / Yukyo Guchi → strategist (turnaround path).
* For distressed borrowers (Yoi Kanri / Yukyo Guchi):
  - strategist → feasibility_critic → [main_bank_critic, sub_bank_critic, guarantor_critic] (parallel fan-out).
  - [critics] → lead_arranger (fan-in).
  - lead_arranger → conditional:
    * approved → hitl_negotiation (existing HITL loop).
    * rejected (and revision_count < MAX) → strategist (cyclic revision).
    * rejected (and revision_count >= MAX) → END (escalate).
* HITL loop: hitl_negotiation → {approve: plan_writer, revise: strategist, reject: END}.
* Postgres checkpointer so state (and the ``interrupt()`` pause) persists.

PART 2: keieisha_hosho runs for ALL borrowers (after classifier, before branch).
PART 3: critics run in parallel for distressed borrowers only; lead_arranger
        consolidates before the human HITL step.

The mock data provider is bound into the data-loading nodes via ``partial`` so
the graph can later be pointed at live clients without structural changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import Literal

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.backend.agents.turnaround_orchestrator import hitl_negotiation_node
from app.backend.nodes.critics import (
    feasibility_critic_node,
    guarantor_critic_node,
    main_bank_critic_node,
    sub_bank_critic_node,
)
from app.backend.nodes.ews_scoring import classifier_node, ews_node
from app.backend.nodes.financial_extraction import intake_node, macro_node
from app.backend.nodes.kaizen_generation import plan_writer_node, strategist_node
from app.backend.nodes.keieisha_hosho import keieisha_hosho_node
from app.backend.nodes.lead_arranger import lead_arranger_node
from app.backend.state import NegotiationDecision, SaiseiState
from app.backend.tools.provider import MockDataProvider
from app.shared.constants import MAX_REVISION_CYCLES as _MAX_REVISION_CYCLES
from app.shared.settings import get_settings

__all__ = [
    "build_graph",
    "compile_graph",
    "postgres_checkpointer",
    "route_after_classification",
    "route_after_negotiation",
    "route_after_lead_arranger",
]


def route_after_classification(
    state: SaiseiState,
) -> Literal["strategist", "__end__"]:
    """Route on the FSA classification.

    Joyo (Normal) is monitor-only and ends; the other classes need a turnaround.
    """
    if state.fsa_classification is not None and state.fsa_classification.requires_turnaround:
        return "strategist"
    return END


def route_after_negotiation(
    state: SaiseiState,
) -> Literal["plan_writer", "strategist", "__end__"]:
    """Route on the banker's negotiation decision."""
    decision = state.negotiation_decision
    if decision is NegotiationDecision.APPROVE:
        return "plan_writer"
    if decision is NegotiationDecision.REVISE:
        return "strategist"
    return END


def route_after_lead_arranger(
    state: SaiseiState,
) -> Literal["hitl_negotiation", "strategist", "__end__"]:
    """Route on the lead arranger's consensus verdict.

    - approved → proceed to human HITL step.
    - needs_human → proceed to HITL so the banker can set commitment flags
      (yakuin_hoshu_cut / personal_asset_disposal) that the strategist cannot.
      This prevents a deadlock where banker-only blockers would otherwise loop
      the strategist to escalation before the banker is ever consulted.
    - rejected + revision_count < MAX → back to strategist (cyclic revision).
    - rejected + revision_count >= MAX → END (escalate; prevent infinite loop).
    """
    if state.negotiation_status in ("approved", "needs_human"):
        return "hitl_negotiation"
    # Rejected path.
    if state.revision_count < _MAX_REVISION_CYCLES:
        return "strategist"
    # Max revisions reached — escalate.
    return END


def build_graph(provider: MockDataProvider | None = None) -> StateGraph:
    """Build (but do not compile) the Saisei StateGraph.

    Args:
        provider: Data provider bound into data-loading nodes; defaults to mocks.

    Returns:
        The assembled, uncompiled ``StateGraph``.
    """
    provider = provider or MockDataProvider()
    graph: StateGraph = StateGraph(SaiseiState)

    # --- Assessment path (all borrowers) ---
    graph.add_node("intake", partial(intake_node, provider=provider))
    graph.add_node("ews", partial(ews_node, provider=provider))
    graph.add_node("macro", partial(macro_node, provider=provider))
    graph.add_node("classifier", classifier_node)
    # PART 2: Keieisha Hosho runs for ALL borrowers.
    graph.add_node("keieisha_hosho", keieisha_hosho_node)

    # --- Turnaround path (distressed borrowers) ---
    graph.add_node("strategist", strategist_node)

    # PART 4: Feasibility critic (advisory-only upstream operational pre-screen).
    graph.add_node("feasibility_critic", feasibility_critic_node)

    # PART 3: Parallel critics (fan-out).
    graph.add_node("main_bank_critic", main_bank_critic_node)
    graph.add_node("sub_bank_critic", sub_bank_critic_node)
    graph.add_node("guarantor_critic", guarantor_critic_node)

    # PART 3: Lead arranger (fan-in).
    graph.add_node("lead_arranger", lead_arranger_node)

    # HITL (existing, unchanged).
    graph.add_node("hitl_negotiation", hitl_negotiation_node)
    graph.add_node("plan_writer", plan_writer_node)

    # --- Edges: assessment path ---
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "ews")
    graph.add_edge("ews", "macro")
    graph.add_edge("macro", "classifier")
    # PART 2: keieisha_hosho after classifier, before branch.
    graph.add_edge("classifier", "keieisha_hosho")

    graph.add_conditional_edges(
        "keieisha_hosho",
        route_after_classification,
        {"strategist": "strategist", END: END},
    )

    # --- Edges: turnaround path ---
    # PART 4: strategist -> feasibility_critic (advisory pre-screen) -> fan-out.
    # The feasibility critic runs once before the parallel critic fan-out and
    # annotates strategies; it does not gate, so the fan-out originates from it.
    graph.add_edge("strategist", "feasibility_critic")

    # PART 3: Fan-out from feasibility_critic to all three critics in parallel.
    graph.add_edge("feasibility_critic", "main_bank_critic")
    graph.add_edge("feasibility_critic", "sub_bank_critic")
    graph.add_edge("feasibility_critic", "guarantor_critic")

    # PART 3: Fan-in from all critics to lead_arranger.
    graph.add_edge("main_bank_critic", "lead_arranger")
    graph.add_edge("sub_bank_critic", "lead_arranger")
    graph.add_edge("guarantor_critic", "lead_arranger")

    # PART 3: Conditional routing from lead_arranger.
    graph.add_conditional_edges(
        "lead_arranger",
        route_after_lead_arranger,
        {
            "hitl_negotiation": "hitl_negotiation",
            "strategist": "strategist",
            END: END,
        },
    )

    # --- Edges: HITL loop (unchanged) ---
    graph.add_conditional_edges(
        "hitl_negotiation",
        route_after_negotiation,
        {"plan_writer": "plan_writer", "strategist": "strategist", END: END},
    )
    graph.add_edge("plan_writer", END)

    return graph


@contextmanager
def postgres_checkpointer() -> Iterator[PostgresSaver]:
    """Yield a set-up Postgres checkpointer using the configured DSN.

    Usage::

        with postgres_checkpointer() as cp:
            app = compile_graph(checkpointer=cp)
    """
    dsn = get_settings().postgres_dsn
    with PostgresSaver.from_conn_string(dsn) as checkpointer:
        checkpointer.setup()
        yield checkpointer


def compile_graph(
    provider: MockDataProvider | None = None,
    checkpointer: PostgresSaver | None = None,
) -> CompiledStateGraph:
    """Compile the Saisei graph.

    Args:
        provider: Data provider bound into data-loading nodes.
        checkpointer: Optional checkpointer enabling interrupt/resume. When
            omitted the graph compiles without persistence (useful for tests
            that do not exercise the HITL pause).

    Returns:
        The compiled, runnable graph.
    """
    graph = build_graph(provider)
    return graph.compile(checkpointer=checkpointer)
