"""Human-in-the-loop (HITL) turnaround orchestrator.

The only TRUE agent in the Saisei system: pauses the graph with ``interrupt()``
so a banker can review the proposed strategies and respond. Execution resumes
via ``Command(resume=...)`` carrying the banker's decision.

Resume payloads accepted:
* ``{"decision": "approve", "strategy_index": <int>}``
* ``{"decision": "revise",  "revision_note": "<text>"}``
* ``{"decision": "reject",  "revision_note": "<optional text>"}``

State is persisted across the interrupt by the Postgres checkpointer (wired in
the builder), so the pause can last arbitrarily long.

This module is the canonical location under ``app.backend.agents.turnaround_orchestrator``.
The legacy path ``shared.graph.nodes.hitl_negotiation`` re-exports from here.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.backend.state import NegotiationDecision, SaiseiState, Strategy
from app.shared.logging import get_logger

__all__ = ["hitl_negotiation_node"]

_log = get_logger(__name__)


def _interrupt_payload(state: SaiseiState) -> dict[str, Any]:
    """Build the payload surfaced to the human during the interrupt."""
    return {
        "prompt": "Review the proposed turnaround strategies and decide.",
        "company": state.company_profile.name if state.company_profile else state.tdb_code,
        "fsa_classification": (
            state.fsa_classification.value if state.fsa_classification else None
        ),
        "ews_score": state.ews_score,
        "working_capital_gap": state.working_capital_gap,
        "strategies": [s.model_dump() for s in state.proposed_strategies],
        "decisions": [d.value for d in NegotiationDecision],
        # Surface Hosho Kaijo assessment to the banker.
        "hosho_kaijo_score": state.hosho_kaijo_score,
        "hosho_kaijo_eligible": state.hosho_kaijo_eligible,
        "succession_ready": state.succession_ready,
        # Surface creditor-meeting result if available.
        "negotiation_status": state.negotiation_status,
        "revision_directive": state.revision_directive,
        # PART 4: Surface the advisory creditor-meeting rehearsal + feasibility
        # notes so the banker can prepare for how each creditor will argue.
        # Advisory only; never affects any gate or routing.
        "meeting_briefing": state.meeting_briefing,
        "feasibility_notes": state.feasibility_notes,
        # Surface accountability commitment flags (Fix 3).
        # The banker must set these to True to satisfy the main_bank critic gates.
        "yakuin_hoshu_cut": state.yakuin_hoshu_cut,
        "personal_asset_disposal": state.personal_asset_disposal,
        "commitment_flags_prompt": (
            "To satisfy the main bank critic, confirm: "
            "(1) yakuin_hoshu_cut — executive compensation has been / will be cut; "
            "(2) personal_asset_disposal — owner will dispose of personal assets "
            "(required only when working_capital_gap < 0)."
        ),
    }


def _commitment_flags(response: dict[str, Any]) -> dict[str, bool]:
    """Extract banker commitment flags from the resume payload.

    Only keys explicitly present in the payload are returned, so a partial
    write never clobbers existing state with defaults. These flags are the
    banker-only gates the main_bank critic checks (``yakuin_hoshu_cut`` and
    ``personal_asset_disposal``); persisting them is what lets a revise loop
    clear a ``needs_human`` blocker instead of deadlocking.
    """
    flags: dict[str, bool] = {}
    for key in ("yakuin_hoshu_cut", "personal_asset_disposal"):
        if key in response:
            flags[key] = bool(response[key])
    return flags


def hitl_negotiation_node(state: SaiseiState) -> dict[str, Any]:
    """Interrupt for banker negotiation and apply the resumed decision.

    Args:
        state: Current graph state (requires ``proposed_strategies``).

    Returns:
        Partial state update reflecting the banker's decision.
    """
    response: dict[str, Any] = interrupt(_interrupt_payload(state))

    raw_decision = str(response.get("decision", "")).lower()
    try:
        decision = NegotiationDecision(raw_decision)
    except ValueError:
        _log.warning("hitl.invalid_decision", decision=raw_decision)
        return {"errors": [*state.errors, f"Invalid negotiation decision: {raw_decision!r}"]}

    # Persist banker commitment flags (banker-only critic gates) so a revise
    # loop can actually clear needs_human blockers instead of deadlocking.
    flags = _commitment_flags(response)

    if decision is NegotiationDecision.APPROVE:
        index = int(response.get("strategy_index", 0))
        if not 0 <= index < len(state.proposed_strategies):
            _log.warning("hitl.bad_index", strategy_index=index)
            return {"errors": [*state.errors, f"strategy_index out of range: {index}"]}
        approved: Strategy = state.proposed_strategies[index]
        _log.info("hitl.approved", strategy=approved.title)
        return {
            "negotiation_decision": decision,
            "approved_strategy": approved,
            "revision_note": None,
            **flags,
        }

    note = response.get("revision_note")
    _log.info("hitl.decided", decision=decision.value)
    return {
        "negotiation_decision": decision,
        "revision_note": note,
        "approved_strategy": None,
        **flags,
    }
