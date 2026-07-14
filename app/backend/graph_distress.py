"""LangGraph builder for the loan-distress graph (条件変更 / 償却).

The depth half's graph edge, and the distress twin of
:mod:`app.backend.graph_servicing`. Where the servicing graph records a
**non-distress, non-gated** operational transition along the performing arc
(実行 → 正常 → 完済) and runs straight to completion, the distress graph records a
**HITL-gated credit / distress** transition and therefore PAUSES with
``interrupt()`` for the banker, exactly like the origination graph
(:mod:`app.backend.graph_origination`) does at the 稟議 credit gate.

It is the graph-side realisation of the two distress nodes that were built and
merged but, until now, had no graph driving them:

* ``restructure_node`` (条件変更 / リスケ) — records the HITL-gated
  ``PERFORMING (正常) → RESTRUCTURED (条件変更)`` transition and attaches the
  deterministic self-curing verdict for the proposed terms.
* ``writeoff_node`` (償却) — records the HITL-gated
  ``WORKOUT (管理回収) → WRITTEN_OFF (償却)`` terminal transition and surfaces the
  deterministic charged-off amount.

Flow::

    START
      → distress_intake   (load the facility's durable loan log by loan_id)
      → distress_hitl     (interrupt; the banker decides proceed / abort)
      → route_after_distress_decision:
            proceed + restructure → restructure_node → END
            proceed + writeoff    → writeoff_node    → END
            abort / unknown       → END

Authority boundary (identical to the rest of Saisei): the node only ASKS at the
interrupt; the banker decides. The gated transition is recorded ONLY on the
proceed branch, by the existing ``restructure_node`` / ``writeoff_node`` — which
already guard against :data:`HITL_GATED_TRANSITIONS` and author the event as the
resolved banker. The advisory verdict (``restructure_curing`` / ``loan_writeoff``)
feeds no gate, route, or figure; the route keys solely off the banker's
proceed/abort decision and the requested ``distress_action``.

This graph is ADDITIVE — it touches neither the origination, servicing, nor
turnaround graphs; the four share only the ``SaiseiState`` schema and the
loan-lifecycle spine, so a facility originated, serviced, then (if it
deteriorates) restructured or charged off forms one continuous, auditable
ledger.

The checkpointer is reused from ``app.backend.graph`` so the ``interrupt()``
pause persists exactly like the origination / turnaround HITL pause.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph resolves node ``config`` parameter types at ``add_node`` time without a
spurious UserWarning (mirrors app.backend.graph_origination / graph_servicing).
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.backend.graph_servicing import servicing_intake_node
from app.backend.nodes.restructure import restructure_node
from app.backend.nodes.writeoff import writeoff_node
from app.backend.state import SaiseiState
from app.shared.logging import get_logger

__all__ = [
    "DISTRESS_ACTIONS",
    "build_distress_graph",
    "compile_distress_graph",
    "distress_hitl_node",
    "route_after_distress_decision",
]

_log = get_logger(__name__)

#: The closed set of distress actions the graph accepts, each routing to the
#: deterministic, HITL-gated distress node that records the implied transition:
#:   'restructure' -> 条件変更 (PERFORMING -> RESTRUCTURED), via restructure_node
#:   'writeoff'    -> 償却     (WORKOUT   -> WRITTEN_OFF),  via writeoff_node
DISTRESS_ACTIONS: frozenset[str] = frozenset({"restructure", "writeoff"})


def _interrupt_payload(state: SaiseiState) -> dict[str, Any]:
    """Build the payload surfaced to the banker at the distress decision gate.

    Surfaces the requested distress action and, for a restructure, the proposed
    terms (so the banker sees the relief being granted). The advisory verdict
    itself (``restructure_curing`` / ``loan_writeoff``) is produced by the
    recording node on the proceed branch and read from the snapshot after the
    run completes — the interrupt asks only for the proceed/abort decision.
    """
    action = str(state.distress_action or "").strip().lower()
    payload: dict[str, Any] = {
        "prompt": (
            "Review the distress move and decide: proceed (実行) or abort (中止). "
            "The transition is HITL-gated and recorded only on proceed."
        ),
        "loan_id": state.loan_id,
        "distress_action": action,
        "decisions": ["proceed", "abort"],
    }
    if action == "restructure":
        payload["proposed_terms"] = {
            "grace_months": state.restructure_grace_months,
            "rate_reduction_bps": state.restructure_rate_reduction_bps,
        }
    return payload


def distress_hitl_node(state: SaiseiState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Interrupt for the banker's proceed / abort distress decision.

    Pauses with ``interrupt()`` so the banker decides whether to proceed with the
    requested distress move (条件変更 / 償却) or abort. Records the decision on
    ``distress_decision`` so :func:`route_after_distress_decision` can route to
    the recording node (proceed) or to END (abort). Records NO loan transition
    itself — the gated transition is owned by ``restructure_node`` /
    ``writeoff_node`` on the proceed branch, mirroring how
    ``origination_hitl_node`` defers disbursement to ``disbursement_node``.

    Resume payload: ``{"decision": "proceed" | "abort", "actor": "<id>"?}``.

    Args:
        state: Current graph state (requires an attached facility + a
            ``distress_action``).
        config: LangGraph run config (injected; unused beyond symmetry).

    Returns:
        Partial state update with ``distress_decision`` (and an ``errors`` entry
        for an unrecognised decision, which routes to END as an abort).
    """
    response: dict[str, Any] = interrupt(_interrupt_payload(state))

    decision = str(response.get("decision", "")).strip().lower()
    if decision not in {"proceed", "abort"}:
        _log.warning("distress.invalid_decision", decision=decision)
        return {
            "errors": [
                *state.errors,
                f"Invalid distress decision: {decision!r}",
            ],
            "distress_decision": "abort",
        }
    _log.info(
        "distress.decided",
        decision=decision,
        action=state.distress_action,
        loan_id=state.loan_id,
    )
    return {"distress_decision": decision}


def route_after_distress_decision(state: SaiseiState) -> str:
    """Route on the banker's distress decision + the requested action.

    - ``proceed`` + ``restructure`` -> restructure_node (records 条件変更).
    - ``proceed`` + ``writeoff``    -> writeoff_node    (records 償却).
    - anything else (``abort`` / unset / unknown action) -> END.

    Keyed off ``distress_decision`` (set by distress_hitl_node) and
    ``distress_action``, never a string literal scattered elsewhere.
    """
    if state.distress_decision != "proceed":
        return END
    action = str(state.distress_action or "").strip().lower()
    if action == "restructure":
        return "restructure"
    if action == "writeoff":
        return "writeoff"
    return END


def build_distress_graph() -> StateGraph[SaiseiState]:
    """Build (but do not compile) the loan-distress StateGraph.

    A four-node graph: ``distress_intake`` resolves the facility's durable loan
    log by ``loan_id`` (a no-op when the caller already supplied it or none is
    durable, reusing the servicing intake seam verbatim), ``distress_hitl``
    interrupts for the banker's proceed/abort decision, then a conditional edge
    routes to ``restructure_node`` or ``writeoff_node`` (which record the
    HITL-gated transition + attach the advisory verdict) on proceed, or to END on
    abort. No data provider is bound (the distress nodes reason only over the
    facility's existing loan log + financials on state, not a fresh TDB lookup).

    Returns:
        The assembled, uncompiled distress ``StateGraph``.
    """
    graph: StateGraph[SaiseiState] = StateGraph(SaiseiState)
    graph.add_node("distress_intake", servicing_intake_node)
    graph.add_node("distress_hitl", distress_hitl_node)
    graph.add_node("restructure", restructure_node)
    graph.add_node("writeoff", writeoff_node)

    graph.add_edge(START, "distress_intake")
    graph.add_edge("distress_intake", "distress_hitl")
    graph.add_conditional_edges(
        "distress_hitl",
        route_after_distress_decision,
        {"restructure": "restructure", "writeoff": "writeoff", END: END},
    )
    graph.add_edge("restructure", END)
    graph.add_edge("writeoff", END)
    return graph


def compile_distress_graph(
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[SaiseiState]:
    """Compile the loan-distress graph.

    Args:
        checkpointer: Optional checkpointer enabling the HITL interrupt/resume
            and making the run durable/idempotent on its ``thread_id`` (exactly
            like the origination graph). When omitted the graph compiles without
            persistence (useful for tests that drive the decision directly).

    Returns:
        The compiled, runnable distress graph.
    """
    return build_distress_graph().compile(checkpointer=checkpointer)
