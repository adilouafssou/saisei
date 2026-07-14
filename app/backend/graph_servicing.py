"""LangGraph builder for the loan-servicing graph (貸出管理).

The servicing half's graph edge: a small, self-contained StateGraph that records
a deterministic, **non-distress** lifecycle transition along the performing arc
of a facility's life — the middle of the lifecycle that neither the origination
graph (``app.backend.graph_origination``) nor the turnaround graph
(``app.backend.graph``) drives:

    START
      → servicing_intake (load the facility's durable loan log by loan_id)
      → servicing        (record the implied servicing transition:
                          'confirm' -> 実行 → 正常 ; 'repay' -> 正常 → 完済)
      → END

Unlike origination and turnaround there is NO ``interrupt()`` here: a servicing
transition is an operational fact (a drawn-down facility entered normal
servicing; a facility was fully repaid), never a banker-authority credit /
distress judgement — the servicing transitions are in
:data:`~app.shared.models.loan.SERVICING_TRANSITIONS`, which is disjoint from
:data:`~app.shared.models.loan.HITL_GATED_TRANSITIONS`. The graph therefore runs
straight to completion. Every credit / distress move (条件変更 / 管理回収 / 償却)
stays owned by the HITL-gated halves.

This graph is ADDITIVE — it touches neither the origination nor the turnaround
graph; the three share only the ``SaiseiState`` schema and the loan-lifecycle
spine, so a facility originated, then serviced, then (if it deteriorates)
assessed, forms one continuous, auditable ledger.

The checkpointer is reused from ``app.backend.graph`` so a servicing run is
durable and idempotent on its ``thread_id`` exactly like the other two graphs,
even though it never pauses.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph resolves node ``config`` parameter types at ``add_node`` time without a
spurious UserWarning (mirrors app.backend.graph / graph_origination).
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.backend.nodes.servicing import servicing_node
from app.backend.state import SaiseiState
from app.shared.logging import get_logger

__all__ = [
    "build_servicing_graph",
    "compile_servicing_graph",
    "servicing_intake_node",
]

_log = get_logger(__name__)


def servicing_intake_node(
    state: SaiseiState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Load the facility's durable loan log so the servicing node can act on it.

    Over the HTTP surface a servicing run arrives as just a ``loan_id`` + the
    requested ``servicing_action``; the facility's event log lives in the durable
    loan ledger (written by origination / assessment in earlier sessions). This
    front node resolves that log via the shared
    :func:`~app.backend.portfolio.loan_store_postgres.read_loan_events` seam so
    the deterministic ``servicing`` node reasons over the facility's TRUE current
    status rather than an empty in-run log.

    Caller-supplied events win: when ``state.loan_events`` is already populated
    (a unit test, or a caller that pre-attached the log), this is a no-op, so the
    graph stays drivable directly without a ledger. Offline (no
    ``SAISEI_LOAN_DSN``) the read returns ``[]`` and this is likewise a no-op,
    keeping the default byte-stable.

    Args:
        state: Current graph state (requires ``loan_id`` to load from the ledger).
        config: LangGraph run config (injected; unused).

    Returns:
        Partial state update attaching the durable ``loan_events``, or an empty
        update when the caller already supplied them / none are durable.
    """
    if state.loan_events or not state.loan_id:
        return {}
    from app.backend.portfolio.loan_store_postgres import read_loan_events

    events = read_loan_events(state.loan_id, log_event="servicing.loan_read_failed")
    if not events:
        return {}
    _log.info("servicing_intake.resumed", loan_id=state.loan_id, count=len(events))
    return {"loan_events": events}


def build_servicing_graph() -> StateGraph[SaiseiState]:
    """Build (but do not compile) the loan-servicing StateGraph.

    A two-node graph: ``servicing_intake`` resolves the facility's durable loan
    log by ``loan_id`` (a no-op when the caller already supplied it or none is
    durable), then the deterministic ``servicing`` node records the transition
    implied by ``state.servicing_action`` and the run completes. No data provider
    is bound (servicing reasons only over the facility's existing loan log, not
    over a fresh TDB lookup), and no interrupt is wired (servicing transitions
    are non-gated operational facts).

    Returns:
        The assembled, uncompiled servicing ``StateGraph``.
    """
    graph: StateGraph[SaiseiState] = StateGraph(SaiseiState)
    graph.add_node("servicing_intake", servicing_intake_node)
    graph.add_node("servicing", servicing_node)
    graph.add_edge(START, "servicing_intake")
    graph.add_edge("servicing_intake", "servicing")
    graph.add_edge("servicing", END)
    return graph


def compile_servicing_graph(
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[SaiseiState]:
    """Compile the loan-servicing graph.

    Args:
        checkpointer: Optional checkpointer making a servicing run durable and
            idempotent on its ``thread_id`` (the run never pauses, but the
            checkpoint lets a repeat call read the recorded outcome). When
            omitted the graph compiles without persistence (useful for tests
            that only assert the single transition).

    Returns:
        The compiled, runnable servicing graph.
    """
    return build_servicing_graph().compile(checkpointer=checkpointer)
