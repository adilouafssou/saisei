"""Human-in-the-loop origination orchestrator (the 稟議 credit decision).

The origination-side analogue of
:mod:`app.backend.agents.turnaround_orchestrator`. Where the turnaround HITL
node gates the distress decision (approve / revise / reject a turnaround plan),
this node gates the **credit decision** at the 稟議 gate: it pauses the
origination graph with ``interrupt()`` so a banker reviews the deterministic,
grounded recommendation produced by ``loan_origination_node`` and decides
APPROVE (承認) or DECLINE (謝絖).

Authority boundary (identical to the rest of Saisei):

* The node only ASKS; the banker decides. The deterministic recommendation is
  advisory — it never records the decision.
* On the banker's decision it records the HITL-gated
  ``UNDER_REVIEW → APPROVED`` / ``UNDER_REVIEW → DECLINED`` transition (both in
  ``HITL_GATED_TRANSITIONS``) as an immutable :class:`LoanEvent`, and emits a
  best-effort ``HUMAN_DECISION`` audit event (who decided what, when, against
  which data/threshold version).
* No LLM is involved and no figure is produced here.

Disbursement (``APPROVED → DISBURSED``) is a separate, deterministic node
(:func:`disbursement_node`) reached only on the approve path — the operational
realisation of an approved facility.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph can resolve the ``config`` node parameter type at ``add_node`` time
without emitting a spurious UserWarning (mirrors turnaround_orchestrator).
"""

import datetime as dt
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.record import record_event
from app.backend.state import SaiseiState
from app.shared.logging import get_logger
from app.shared.models.loan import (
    HITL_GATED_TRANSITIONS,
    LoanEvent,
    LoanStatus,
    OriginationDecision,
    current_status,
)

__all__ = ["origination_hitl_node", "disbursement_node"]

_log = get_logger(__name__)


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    """Extract the run thread_id from a LangGraph RunnableConfig (or '')."""
    if not config:
        return ""
    configurable = config.get("configurable") or {}
    return str(configurable.get("thread_id", "") or "")


def _resolve_actor(response: dict[str, Any]) -> str:
    """Resolve the banker identity for the credit-decision audit event.

    Prefers an explicit actor in the resume payload (``actor`` / ``banker_id``);
    falls back to the identity seam (``current_actor``), read defensively so a
    missing value never raises on the decision path (mirrors the turnaround
    orchestrator's resolver).
    """
    for key in ("actor", "banker_id"):
        value = str(response.get(key, "") or "").strip()
        if value:
            return value
    try:
        from app.backend.identity import current_actor

        return current_actor()
    except Exception:  # noqa: BLE001 - settings read must not break the decision
        return "banker"


def _interrupt_payload(state: SaiseiState) -> dict[str, Any]:
    """Build the payload surfaced to the banker at the 稟議 credit decision."""
    return {
        "prompt": (
            "Review the origination recommendation and decide: approve (承認) or decline (謝絖)."
        ),
        "company": (state.company_profile.name if state.company_profile else state.tdb_code),
        "tdb_score": state.tdb_score,
        # The deterministic, grounded advisory recommendation (advisory only).
        "recommendation": state.origination_recommendation,
        "decisions": [d.value for d in OriginationDecision],
    }


def _credit_event(
    state: SaiseiState, *, target: LoanStatus, response: dict[str, Any]
) -> list[dict[str, Any]]:
    """Record the HITL-gated UNDER_REVIEW → APPROVED / DECLINED transition.

    Runs only when a loan is attached, its current status is UNDER_REVIEW, and
    the transition is legal and HITL-gated (a defensive guard asserts this). The
    event is authored by the resolved banker, mirroring the turnaround approve
    path. Returns ``[]`` (append-only no-op) otherwise.
    """
    if not state.loan_id or not state.loan_events:
        return []
    try:
        current = current_status([LoanEvent.model_validate(e) for e in state.loan_events])
    except Exception as exc:  # noqa: BLE001 - never break the decision path
        _log.warning("origination.hitl_status_failed", error=str(exc), loan_id=state.loan_id)
        return []
    if current is not LoanStatus.UNDER_REVIEW:
        return []
    if not current.can_transition_to(target):
        return []
    if (current, target) not in HITL_GATED_TRANSITIONS:
        _log.warning(
            "origination.transition_not_gated",
            loan_id=state.loan_id,
            current=current.value,
            target=target.value,
        )
        return []
    event = LoanEvent(
        status=target,
        at=_now_utc(),
        actor=_resolve_actor(response),
        note=f"稟議 credit decision: {current.value} -> {target.value}",
    )
    _log.info(
        "origination.credit_decision",
        loan_id=state.loan_id,
        current=current.value,
        target=target.value,
    )
    return [event.model_dump(mode="json")]


def _record_human_decision(
    state: SaiseiState,
    *,
    decision: OriginationDecision,
    response: dict[str, Any],
    config: RunnableConfig | None,
) -> None:
    """Best-effort immutable audit record of the banker's credit decision.

    Emits one ``HUMAN_DECISION`` event capturing WHO decided WHAT, WHEN, against
    which data/threshold version (computed inside ``record_event``). Side-record
    only: never mutates graph state and never fatal (mirrors the turnaround
    orchestrator's recorder). Offline -> NullAuditSink -> no-op.
    """
    try:
        record_event(
            AuditEventType.HUMAN_DECISION,
            state=state,
            thread_id=_thread_id_from_config(config),
            actor=_resolve_actor(response),
            payload={
                "decision": decision.value,
                "gate": "origination",
                "proposed_status": decision.proposed_status.value,
            },
        )
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never fatal
        _log.warning("origination.audit_record_failed", error=str(exc), tdb_code=state.tdb_code)


def _record_watchlist(state: SaiseiState, new_events: list[dict[str, Any]]) -> None:
    """Best-effort upsert of the originated facility into the watchlist book.

    Records the facility into the SAME Portfolio watchlist the assessment side
    uses, carrying its NEW loan-lifecycle status. The node's just-recorded events
    are not yet in ``state.loan_events`` (LangGraph applies the return after the
    node), so a state view with the new events merged in is passed so the
    recorded status reflects the transition the node just made (承認 / 謝絶 /
    実行), not the prior one. Write-only, never fatal, offline no-op — see
    ``record_origination_snapshot``.
    """
    try:
        from app.backend.portfolio.recorder import record_origination_snapshot

        merged = state.model_copy(update={"loan_events": [*state.loan_events, *new_events]})
        record_origination_snapshot(state=merged)
    except Exception as exc:  # noqa: BLE001 - watchlist is best-effort, never fatal
        _log.warning("origination.watchlist_failed", error=str(exc))


def origination_hitl_node(
    state: SaiseiState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Interrupt for the banker's 稟議 credit decision and apply it.

    Pauses with ``interrupt()`` so the banker decides APPROVE / DECLINE over the
    advisory recommendation, then records the HITL-gated UNDER_REVIEW →
    APPROVED / DECLINED transition and a best-effort HUMAN_DECISION audit event.

    Resume payload: ``{"decision": "approve" | "decline", "actor": "<id>"?}``.

    Args:
        state: Current graph state (requires an attached UNDER_REVIEW loan).
        config: LangGraph run config (injected); used to read the thread_id.

    Returns:
        Partial state update with ``origination_decision`` and any HITL-gated
        ``loan_events`` to append.
    """
    response: dict[str, Any] = interrupt(_interrupt_payload(state))

    raw_decision = str(response.get("decision", "")).strip().lower()
    try:
        decision = OriginationDecision(raw_decision)
    except ValueError:
        _log.warning("origination.invalid_decision", decision=raw_decision)
        return {
            "errors": [
                *state.errors,
                f"Invalid origination decision: {raw_decision!r}",
            ]
        }

    loan_events = _credit_event(state, target=decision.proposed_status, response=response)
    _record_human_decision(state, decision=decision, response=response, config=config)
    # Durably persist the HITL-gated credit transition (APPROVED / DECLINED) to
    # the append-only loan ledger so the originated facility's lifecycle is
    # recoverable across runs. Best-effort / offline no-op (see the helper).
    from app.backend.portfolio.loan_store_postgres import persist_loan_events

    persist_loan_events(state, loan_events, log_event="origination.credit_persisted")
    # Surface the facility in the watchlist with its new status. On a DECLINE
    # this is the terminal record (謝絶); on an APPROVE the disbursement node
    # upserts again with 実行.
    _record_watchlist(state, loan_events)
    _log.info("origination.decided", decision=decision.value)
    return {
        "origination_decision": decision.value,
        "loan_events": loan_events,
    }


def _disbursed_amount(state: SaiseiState) -> int:
    """Resolve the principal drawn at disbursement (integer yen, >= 0).

    The drawn principal is the approved provisional facility ceiling (融資上限)
    surfaced by the origination recommendation -- the figure the banker approved.
    Read defensively from ``origination_recommendation`` (a JSON-safe dict on
    state); falls back to the lender-stakes sum, then 0. Never negative.
    """
    rec = getattr(state, "origination_recommendation", None) or {}
    amount = 0
    if isinstance(rec, dict):
        try:
            amount = int(rec.get("max_facility_amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0
    if amount <= 0 and state.lender_stakes:
        amount = sum(int(v) for v in state.lender_stakes.values())
    return max(0, amount)


def disbursement_node(state: SaiseiState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Record the deterministic APPROVED → DISBURSED transition (drawdown).

    Reached only on the approve path. Disbursing an already-approved facility is
    an operational step, not a fresh credit decision, so the deterministic node
    records it (authored by ``"system"``), mirroring the intake bootstrap. Runs
    only when a loan is attached, its current status is APPROVED, and the
    transition is legal. Returns ``[]`` (append-only no-op) otherwise.

    Args:
        state: Current graph state.
        config: LangGraph run config (injected; unused beyond symmetry).

    Returns:
        Partial state update appending the DISBURSED event (or no-op).
    """
    if not state.loan_id or not state.loan_events:
        return {"loan_events": []}
    try:
        current = current_status([LoanEvent.model_validate(e) for e in state.loan_events])
    except Exception as exc:  # noqa: BLE001 - never break the terminal path
        _log.warning("disbursement.status_failed", error=str(exc), loan_id=state.loan_id)
        return {"loan_events": []}
    target = LoanStatus.DISBURSED
    if current is not LoanStatus.APPROVED or not current.can_transition_to(target):
        return {"loan_events": []}
    # Stamp the drawn principal ONTO the DISBURSED event so the facility's
    # outstanding balance is recoverable from the loan ledger alone (no external
    # lender-stakes snapshot). The drawn amount is the approved provisional
    # ceiling (融資上限) the banker just approved; 0 when unavailable, which keeps
    # the event valid and falls back to the stakes baseline downstream.
    disbursed_amount = _disbursed_amount(state)
    event = LoanEvent(
        status=target,
        at=_now_utc(),
        actor="system",
        note=f"Disbursement: {current.value} -> {target.value} (deterministic)",
        principal_disbursed=disbursed_amount,
    )
    _log.info(
        "disbursement.recorded",
        loan_id=state.loan_id,
        current=current.value,
        target=target.value,
        principal_disbursed=disbursed_amount,
    )
    loan_events = [event.model_dump(mode="json")]
    # Persist the deterministic APPROVED -> DISBURSED drawdown to the ledger.
    from app.backend.portfolio.loan_store_postgres import persist_loan_events

    persist_loan_events(state, loan_events, log_event="disbursement.loan_persisted")
    # Upsert the now-DISBURSED (実行) facility into the watchlist book.
    _record_watchlist(state, loan_events)
    return {"loan_events": loan_events}
