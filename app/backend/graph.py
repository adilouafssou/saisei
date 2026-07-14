"""LangGraph builder for the Saisei turnaround graph.

Wires the nodes into a ``StateGraph`` with:

* A linear assessment path: intake -> ews -> macro -> classifier -> keieisha_hosho.
* A conditional edge on ``fsa_classification`` (five FSA categories):
  - 正常先 (Normal) → END (monitor-only).
  - 要注意先 / 破綻懸念先 → strategist (turnaround path).
  - 実質破綻先 / 破綻先 → workout (legal/liquidation handoff, terminal).
* For distressed borrowers (要注意先 / 破綻懸念先):
  - strategist → feasibility_critic →
    [main_bank_critic, sub_bank_critic, guarantor_critic] (parallel fan-out).
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
FSA-5: workout node added for 実質破綻先 / 破綻先 (requires_workout=True).

The mock data provider is bound into the data-loading nodes via ``partial`` so
the graph can later be pointed at live clients without structural changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from threading import Lock

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.backend.agents.turnaround_orchestrator import hitl_negotiation_node
from app.backend.analysis.node_budgets import instrument_node
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
from app.backend.nodes.workout import workout_node
from app.backend.secrets import resolve_secret
from app.backend.state import NegotiationDecision, SaiseiState
from app.backend.tools.provider import MockDataProvider
from app.shared.constants import MAX_REVISION_CYCLES as _MAX_REVISION_CYCLES
from app.shared.settings import get_settings

__all__ = [
    "build_graph",
    "compile_graph",
    "postgres_checkpointer",
    "make_checkpointer",
    "reset_memory_saver",
    "route_after_classification",
    "route_after_negotiation",
    "route_after_lead_arranger",
    "route_after_feasibility",
]


def route_after_classification(
    state: SaiseiState,
) -> str:
    """Route on the FSA classification (five-category 金融検査マニュアル).

    - 正常先 (Normal) → END (monitor-only).
    - 要注意先 / 破綻懸念先 → strategist (turnaround workflow).
    - 実質破綻先 / 破綻先 → workout (legal/liquidation handoff, terminal).

    Routing is keyed off the ``requires_turnaround`` and ``requires_workout``
    properties on :class:`~app.shared.models.classification.FsaClass`, never
    off string literals, so the routing stays correct if kanji labels change.
    """
    cls = state.fsa_classification
    if cls is not None and cls.requires_workout:
        return "workout"
    if cls is not None and cls.requires_turnaround:
        return "strategist"
    return END


def route_after_negotiation(
    state: SaiseiState,
) -> str:
    """Route on the banker's negotiation decision."""
    decision = state.negotiation_decision
    if decision is NegotiationDecision.APPROVE:
        return "plan_writer"
    if decision is NegotiationDecision.REVISE:
        return "strategist"
    return END


def route_after_feasibility(
    state: SaiseiState,
) -> list[str] | str:
    """Route after feasibility_critic based on the reconciliation predicate.

    MR #2: A PURE DETERMINISTIC PREDICATE decides the route:
    - If ``reconciliation_required`` is True (LLM-vs-floor band distance >=
      RECONCILIATION_BAND_DISTANCE for at least one strategy), route to
      ``hitl_negotiation`` BEFORE the critic fan-out so a human can resolve
      the disagreement. The LLM can ONLY raise the question; it NEVER decides
      direction, verdict, or figure.
    - Otherwise (default, including all offline runs where no LLM is configured),
      fan out to all three critics in parallel (preserving the existing behaviour
      exactly).

    This conditional replaces the three unconditional edges from feasibility_critic
    to the fan-out. All existing routes when reconciliation_required is False are
    preserved exactly.

    Args:
        state: Current graph state (reads reconciliation_required).

    Returns:
        'hitl_negotiation' when reconciliation is required; a list of all three
        critic node names otherwise (LangGraph fan-out).
    """
    if state.reconciliation_required:
        return "hitl_negotiation"
    return ["main_bank_critic", "sub_bank_critic", "guarantor_critic"]


def route_after_lead_arranger(
    state: SaiseiState,
) -> str:
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


def build_graph(provider: MockDataProvider | None = None) -> StateGraph[SaiseiState]:
    """Build (but do not compile) the Saisei StateGraph.

    Args:
        provider: Data provider bound into data-loading nodes; defaults to mocks.

    Returns:
        The assembled, uncompiled ``StateGraph``.
    """
    provider = provider or MockDataProvider()
    graph: StateGraph[SaiseiState] = StateGraph(SaiseiState)

    # --- Assessment path (all borrowers) ---
    graph.add_node("intake", instrument_node("intake", partial(intake_node, provider=provider)))
    graph.add_node("ews", instrument_node("ews", partial(ews_node, provider=provider)))
    graph.add_node("macro", instrument_node("macro", partial(macro_node, provider=provider)))
    graph.add_node("classifier", instrument_node("classifier", classifier_node))
    # PART 2: Keieisha Hosho runs for ALL borrowers.
    graph.add_node("keieisha_hosho", instrument_node("keieisha_hosho", keieisha_hosho_node))

    # --- Workout path (bankrupt borrowers: 実質破綻先 / 破綻先) ---
    # Terminal node: records legal/liquidation handoff and ends.
    graph.add_node("workout", instrument_node("workout", workout_node))

    # --- Turnaround path (distressed borrowers: 要注意先 / 破綻懸念先) ---
    graph.add_node("strategist", instrument_node("strategist", strategist_node))

    # PART 4: Feasibility critic (advisory-only upstream operational pre-screen).
    graph.add_node(
        "feasibility_critic", instrument_node("feasibility_critic", feasibility_critic_node)
    )

    # PART 3: Parallel critics (fan-out).
    graph.add_node("main_bank_critic", instrument_node("main_bank_critic", main_bank_critic_node))
    graph.add_node("sub_bank_critic", instrument_node("sub_bank_critic", sub_bank_critic_node))
    graph.add_node("guarantor_critic", instrument_node("guarantor_critic", guarantor_critic_node))

    # PART 3: Lead arranger (fan-in).
    graph.add_node("lead_arranger", instrument_node("lead_arranger", lead_arranger_node))

    # HITL (existing, unchanged).
    graph.add_node("hitl_negotiation", instrument_node("hitl_negotiation", hitl_negotiation_node))
    graph.add_node("plan_writer", instrument_node("plan_writer", plan_writer_node))

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
        {"strategist": "strategist", "workout": "workout", END: END},
    )

    # Workout is terminal: ends the graph after recording the handoff.
    graph.add_edge("workout", END)

    # --- Edges: turnaround path ---
    # PART 4: strategist -> feasibility_critic (advisory pre-screen).
    # The feasibility critic runs once before the parallel critic fan-out and
    # annotates strategies with the deterministic floor + optional advisory.
    graph.add_edge("strategist", "feasibility_critic")

    # MR #2: Conditional edge after feasibility_critic.
    # - reconciliation_required=True  -> hitl_negotiation (BEFORE fan-out).
    # - reconciliation_required=False -> fan-out to all three critics in parallel.
    # The path map covers all possible return values of route_after_feasibility.
    graph.add_conditional_edges(
        "feasibility_critic",
        route_after_feasibility,
        {
            "hitl_negotiation": "hitl_negotiation",
            "main_bank_critic": "main_bank_critic",
            "sub_bank_critic": "sub_bank_critic",
            "guarantor_critic": "guarantor_critic",
        },
    )

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
    dsn = resolve_secret(get_settings().postgres_dsn)
    with PostgresSaver.from_conn_string(dsn) as checkpointer:
        checkpointer.setup()
        yield checkpointer


#: Process-wide in-memory checkpointer singleton (created lazily). It MUST be a
#: singleton: the UI opens a checkpointer in the streaming worker, again to read
#: the final snapshot, and again on resume. With Postgres those all reconnect to
#: the same durable store; with MemorySaver the interrupt/resume state lives in
#: process memory, so every caller must share the SAME instance or the HITL
#: pause would be lost between calls.
_MEMORY_SAVER: MemorySaver | None = None
_MEMORY_SAVER_LOCK = Lock()


def _memory_saver() -> MemorySaver:
    """Return the shared process-wide MemorySaver, creating it once."""
    global _MEMORY_SAVER
    if _MEMORY_SAVER is None:
        with _MEMORY_SAVER_LOCK:
            if _MEMORY_SAVER is None:
                _MEMORY_SAVER = MemorySaver()
    return _MEMORY_SAVER


def reset_memory_saver() -> None:
    """Drop the process-wide in-memory checkpointer singleton.

    The next :func:`make_checkpointer` call (in the no-persistence mode) lazily
    recreates a fresh :class:`MemorySaver`, so all previously stored runs /
    interrupt state are discarded. This is the PUBLIC seam for that reset; it
    exists so callers never have to reach into the module-private
    ``_MEMORY_SAVER`` global (which is free to change).

    Two intended uses:

    * **Tests** that exercise the offline (MemorySaver) path need each case to
      start from a clean store so thread_ids cannot leak between tests, while
      still letting start -> get -> resume share state WITHIN a test.
    * A **long-lived process** that wants to deliberately clear all in-memory
      runs (e.g. a demo host "reset" action) without restarting.

    Thread-safe (takes the same lock as creation). A no-op when no singleton has
    been created yet. Has NO effect on the durable Postgres path, whose state
    lives outside the process.
    """
    global _MEMORY_SAVER
    with _MEMORY_SAVER_LOCK:
        _MEMORY_SAVER = None


@contextmanager
def make_checkpointer() -> Iterator[BaseCheckpointSaver[str]]:
    """Yield the configured checkpointer (Postgres or in-memory).

    Selection is driven by ``Settings.persist_checkpoints``:

    * ``True`` (default): the durable :func:`postgres_checkpointer`, preserving
      the production interrupt/resume-across-processes behaviour.
    * ``False``: a process-wide :class:`MemorySaver` singleton, so the app runs
      with NO Postgres/Redis (free demo hosting). State resets on restart.

    Callers should use this instead of :func:`postgres_checkpointer` directly so
    the same code path works in both modes::

        with make_checkpointer() as cp:
            app = compile_graph(checkpointer=cp)
    """
    if get_settings().persist_checkpoints:
        with postgres_checkpointer() as cp:
            yield cp
    else:
        # MemorySaver is not a context manager resource; yield the singleton.
        yield _memory_saver()


def compile_graph(
    provider: MockDataProvider | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[SaiseiState]:
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
