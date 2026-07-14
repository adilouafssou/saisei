"""Loan-restructure node (条件変更 / リスケ) — grounded, audited, HITL-gated.

The graph-side realisation of the restructure move on the distress arc, and the
depth mirror of ``loan_origination`` on the breadth arc:

* ``loan_origination`` attaches the deterministic debt-capacity verdict to its
  recommendation, then the credit decision is HITL-gated.
* ``restructure_node`` attaches the deterministic SELF-CURING verdict
  (:func:`app.backend.analysis.restructure_grounding.assess_restructure`) to a
  proposed 条件変更, then the PERFORMING -> RESTRUCTURED transition is HITL-gated.

The distress move it records is ``PERFORMING (正常) -> RESTRUCTURED (条件変更)`` or
the ``RESTRUCTURED -> RESTRUCTURED`` re-restructure self-loop — both members of
:data:`~app.shared.models.loan.HITL_GATED_TRANSITIONS`. Unlike ``servicing_node``
(non-distress, non-gated operational facts) this is a banker-authority credit
judgement, so the node:

1. **always** computes the advisory self-curing verdict from the proposed terms
   and the facility's own figures (so the banker sees whether the restructure
   actually cures the borrower BEFORE deciding); and
2. records the gated transition ONLY when it is legal and HITL-gated, authored
   by the resolved banker — the same authority guard ``origination_hitl_node``
   uses for the credit decision.

Every boundary the rest of Saisei enforces holds: the verdict is deterministic
(no LLM, no figure produced here beyond the auditable relief arithmetic), the
verdict feeds no gate / route / figure (it annotates), the transition is
HITL-gated, and each recorded transition is an immutable event on the
append-only loan ledger (persisted best-effort / offline no-op).

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph can resolve the ``config`` node parameter type at ``add_node`` time
without a spurious UserWarning (mirrors ews_scoring / workout / servicing).
"""

import datetime as dt
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.backend.analysis.restructure_grounding import assess_restructure
from app.backend.state import SaiseiState
from app.shared.logging import get_logger
from app.shared.models.loan import (
    HITL_GATED_TRANSITIONS,
    LoanEvent,
    LoanStatus,
    current_status,
    outstanding_principal_for_state,
)

__all__ = ["restructure_node"]

_log = get_logger(__name__)


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _resolve_actor(state: SaiseiState) -> str:
    """Resolve the banker identity authoring the HITL-gated 条件変更 transition.

    A restructure is a banker-authority credit judgement (unlike a servicing
    operational fact authored by ``"system"``), so the recorded event must be
    attributed to the banker. Read defensively from the identity seam so a
    missing value never raises on the path (mirrors the origination orchestrator's
    resolver); falls back to ``"banker"``.
    """
    try:
        from app.backend.identity import current_actor

        return current_actor()
    except Exception:  # noqa: BLE001 - identity read must not break the node
        return "banker"


def _restructure_event(state: SaiseiState, *, actor: str) -> list[dict[str, Any]]:
    """Record the HITL-gated 条件変更 transition, if legal and gated.

    Records ``PERFORMING -> RESTRUCTURED`` (正常 -> 条件変更) or the
    ``RESTRUCTURED -> RESTRUCTURED`` re-restructure self-loop. A defensive guard
    asserts the pair is in :data:`HITL_GATED_TRANSITIONS` before recording, so
    this node can never record a non-gated or illegal transition. Returns ``[]``
    (append-only no-op) when no loan is attached, the log is empty/malformed, the
    current status cannot legally restructure, or the move is not gated.

    Args:
        state: Current graph state at restructure time.
        actor: The resolved banker identity authoring the event.

    Returns:
        A list with zero or one ``LoanEvent`` dict to append.
    """
    if not state.loan_id or not state.loan_events:
        return []
    try:
        current = current_status([LoanEvent.model_validate(e) for e in state.loan_events])
    except Exception as exc:  # noqa: BLE001 - never break the restructure path
        _log.warning("restructure.loan_status_failed", error=str(exc), loan_id=state.loan_id)
        return []

    target = LoanStatus.RESTRUCTURED
    if not current.can_transition_to(target):
        return []
    if (current, target) not in HITL_GATED_TRANSITIONS:
        # The RESTRUCTURED -> RESTRUCTURED re-restructure is a servicing
        # self-loop, not a gated move; this node records only the gated
        # distress transition (PERFORMING -> RESTRUCTURED). A non-gated pair is
        # a no-op here by design.
        _log.info(
            "restructure.transition_not_gated",
            loan_id=state.loan_id,
            current=current.value,
            target=target.value,
        )
        return []

    event = LoanEvent(
        status=target,
        at=_now_utc(),
        actor=actor,
        note=(
            f"条件変更 (restructure): {current.value} -> {target.value} "
            f"(grace {state.restructure_grace_months}m, "
            f"rate -{state.restructure_rate_reduction_bps}bps)"
        ),
    )
    _log.info(
        "restructure.loan_transition",
        loan_id=state.loan_id,
        current=current.value,
        target=target.value,
    )
    return [event.model_dump(mode="json")]


def _record_watchlist(state: SaiseiState, new_events: list[dict[str, Any]]) -> None:
    """Best-effort upsert of the now-条件変更 facility into the watchlist book.

    Records the facility into the SAME Portfolio watchlist the assessment /
    origination / servicing paths use, carrying its NEW lifecycle status
    (条件変更). The node's just-recorded events are not yet in
    ``state.loan_events`` (LangGraph applies the return after the node), so a
    merged state view is passed. Write-only, never fatal, offline no-op — mirrors
    the servicing / origination helpers of the same name.
    """
    if not new_events:
        return
    try:
        from app.backend.portfolio.recorder import record_origination_snapshot

        merged = state.model_copy(update={"loan_events": [*state.loan_events, *new_events]})
        record_origination_snapshot(state=merged)
    except Exception as exc:  # noqa: BLE001 - watchlist is best-effort, never fatal
        _log.warning("restructure.watchlist_failed", error=str(exc))


def restructure_node(state: SaiseiState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Attach the advisory self-curing verdict and record the gated 条件変更.

    Two responsibilities, mirroring ``loan_origination_node`` on the breadth arc:

    1. **Advisory verdict (always).** Computes the deterministic self-curing
       assessment for the proposed restructure terms
       (``restructure_grace_months`` + ``restructure_rate_reduction_bps``)
       against the facility's outstanding principal and the borrower's EWS
       trajectory, and surfaces it on ``restructure_curing`` so the banker sees
       whether the 条件変更 actually cures the borrower (self_curing) or is
       forbearance that never cures (non_curing) BEFORE deciding.
    2. **HITL-gated transition (when authorised).** Records the
       PERFORMING -> RESTRUCTURED transition only when it is legal and gated,
       authored by the resolved banker, and persists it to the append-only loan
       ledger (best-effort / offline no-op). A no-loan run, a non-restructurable
       status, or a non-gated pair yields an append-only no-op.

    The verdict feeds no gate, route, or figure; the transition is the only
    state-advancing effect and it is HITL-gated by construction.

    Args:
        state: Current graph state (the proposed terms + the attached facility).
        config: LangGraph run config (injected; unused beyond symmetry).

    Returns:
        Partial state update with ``restructure_curing`` (a JSON-safe dict) and
        any HITL-gated ``loan_events`` to append.
    """
    outstanding = outstanding_principal_for_state(state)
    curing = assess_restructure(
        state.shisanhyo,
        outstanding,
        state.restructure_grace_months,
        state.restructure_rate_reduction_bps,
    )

    actor = _resolve_actor(state)
    loan_events = _restructure_event(state, actor=actor)

    # Durable side-record: persist the gated 条件変更 transition to the append-only
    # loan ledger (offline no-op without SAISEI_LOAN_DSN). Never affects the
    # return, a gate, a route, or a figure, and is never fatal.
    from app.backend.portfolio.loan_store_postgres import persist_loan_events

    persist_loan_events(state, loan_events, log_event="restructure.loan_persisted")
    _record_watchlist(state, loan_events)

    _log.info(
        "restructure.assessed",
        loan_id=state.loan_id,
        band=curing.band,
        recovery_month_index=curing.recovery_month_index,
        annual_relief=curing.relief.annual_relief,
        recorded=bool(loan_events),
    )

    return {
        "restructure_curing": {
            "band": curing.band,
            "annual_relief": curing.relief.annual_relief,
            "grace_relief": curing.relief.grace_relief,
            "rate_relief": curing.relief.rate_relief,
            "recovery_month_index": curing.recovery_month_index,
            "horizon_months": curing.horizon_months,
            "reason": curing.reason,
            "note": (
                "Advisory only: deterministic self-curing check for the proposed "
                "条件変更. The banker decides; the 条件変更 transition is HITL-gated and "
                "this verdict feeds no gate, route, or decision."
            ),
        },
        "loan_events": loan_events,
    }
