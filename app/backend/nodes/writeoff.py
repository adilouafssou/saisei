"""Loan write-off closure node (償却 / WORKOUT → WRITTEN_OFF) — HITL-gated, terminal.

The missing terminal of the distress arc. The state machine permits
``WORKOUT (管理回収) → WRITTEN_OFF (償却)`` and marks it HITL-gated (writing off a
loan is the most consequential credit decision a bank makes — never automatic),
but no node recorded it: a facility could enter workout and never legally close
out. This node closes that hole.

It mirrors the restructure node's authority posture exactly:

* it records the gated ``WORKOUT → WRITTEN_OFF`` transition ONLY when it is legal
  and HITL-gated, authored by the resolved banker (a write-off is a
  banker-authority credit judgement, never a ``"system"`` operational fact); and
* it surfaces the deterministic **written-off amount** — the facility's true
  outstanding principal (残高) at the bankrupt-class 100% loss
  (``provision_amount`` at ``PROVISION_RATE_BANKRUPT``) — so the banker and the
  ledger record exactly how much was charged off.

The amount is a deterministic figure from outstanding principal and the FSA
class; no LLM is involved. The transition is HITL-gated by construction
(guarded against ``HITL_GATED_TRANSITIONS``) and each recorded transition is an
immutable event on the append-only loan ledger (persisted best-effort / offline
no-op), exactly like ``workout_node`` / ``restructure_node``.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph can resolve the ``config`` node parameter type at ``add_node`` time
without a spurious UserWarning (mirrors workout / servicing / restructure).
"""

import datetime as dt
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.backend.state import SaiseiState
from app.shared.logging import get_logger
from app.shared.models.classification import FsaClass
from app.shared.models.loan import (
    HITL_GATED_TRANSITIONS,
    LoanEvent,
    LoanStatus,
    current_status,
    outstanding_principal_for_state,
    provision_amount,
)
from app.shared.models.money import format_jpy

__all__ = ["writeoff_node"]

_log = get_logger(__name__)


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _resolve_actor(state: SaiseiState) -> str:
    """Resolve the banker identity authoring the HITL-gated 償却 transition.

    A write-off is a banker-authority credit judgement (unlike a servicing
    operational fact authored by ``"system"``), so the recorded event must be
    attributed to the banker. Read defensively from the identity seam so a
    missing value never raises on the path (mirrors the restructure node's
    resolver); falls back to ``"banker"``.
    """
    try:
        from app.backend.identity import current_actor

        return current_actor()
    except Exception:  # noqa: BLE001 - identity read must not break the node
        return "banker"


def _written_off_amount(state: SaiseiState) -> int:
    """Return the deterministic charged-off amount (償却額) in integer yen.

    The write-off charges off the facility's TRUE outstanding principal (残高) at
    the bankrupt-class reserve ratio. A facility reaching 償却 is a bankrupt /
    de-facto-bankrupt borrower (``PROVISION_RATE_BANKRUPT`` = 1.0), so the
    charged-off amount equals the full outstanding balance — computed via the
    SAME deterministic ``provision_amount`` the workout handoff reserves, so the
    write-off figure and the loan-loss provision agree by construction.

    Falls back to the bankrupt class when no FSA classification is on state (a
    facility in WORKOUT is, by routing, a bankrupt-band borrower). Returns 0 when
    no outstanding balance is known, so the amount is omitted rather than guessed.
    """
    outstanding = outstanding_principal_for_state(state)
    if outstanding <= 0:
        return 0
    fsa = state.fsa_classification or FsaClass.HATANSAKI
    return provision_amount(outstanding, fsa, special_attention=bool(state.special_attention))


def _writeoff_event(state: SaiseiState, *, actor: str, amount: int) -> list[dict[str, Any]]:
    """Record the HITL-gated WORKOUT → WRITTEN_OFF transition, if legal and gated.

    A defensive guard asserts the pair is in :data:`HITL_GATED_TRANSITIONS`
    before recording, so this node can never record a non-gated or illegal
    transition. Returns ``[]`` (append-only no-op) when no loan is attached, the
    log is empty/malformed, the current status is not WORKOUT, or the move is not
    legal/gated (e.g. an already-terminal facility).

    Args:
        state: Current graph state at write-off time.
        actor: The resolved banker identity authoring the event.
        amount: The deterministic charged-off amount (for the audit note).

    Returns:
        A list with zero or one ``LoanEvent`` dict to append.
    """
    if not state.loan_id or not state.loan_events:
        return []
    try:
        current = current_status([LoanEvent.model_validate(e) for e in state.loan_events])
    except Exception as exc:  # noqa: BLE001 - never break the terminal path
        _log.warning("writeoff.loan_status_failed", error=str(exc), loan_id=state.loan_id)
        return []

    target = LoanStatus.WRITTEN_OFF
    if current is not LoanStatus.WORKOUT or not current.can_transition_to(target):
        return []
    if (current, target) not in HITL_GATED_TRANSITIONS:
        _log.warning(
            "writeoff.transition_not_gated",
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
            f"償却 (write-off): {current.value} -> {target.value} "
            f"(charged off {format_jpy(amount)})"
        ),
    )
    _log.info(
        "writeoff.loan_transition",
        loan_id=state.loan_id,
        current=current.value,
        target=target.value,
        amount=amount,
    )
    return [event.model_dump(mode="json")]


def _record_watchlist(state: SaiseiState, new_events: list[dict[str, Any]]) -> None:
    """Best-effort upsert of the now-償却 facility into the watchlist book.

    Records the facility into the SAME Portfolio watchlist the other lifecycle
    paths use, carrying its NEW terminal status (償却). The node's just-recorded
    events are not yet in ``state.loan_events`` (LangGraph applies the return
    after the node), so a merged state view is passed. Write-only, never fatal,
    offline no-op — mirrors the restructure / servicing helpers of the same name.
    """
    if not new_events:
        return
    try:
        from app.backend.portfolio.recorder import record_origination_snapshot

        merged = state.model_copy(update={"loan_events": [*state.loan_events, *new_events]})
        record_origination_snapshot(state=merged)
    except Exception as exc:  # noqa: BLE001 - watchlist is best-effort, never fatal
        _log.warning("writeoff.watchlist_failed", error=str(exc))


def writeoff_node(state: SaiseiState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Record the HITL-gated WORKOUT → WRITTEN_OFF (償却) terminal transition.

    Closes the distress arc: a facility in workout (管理回収) that the bank has
    decided to charge off is moved to the terminal WRITTEN_OFF (償却) status. The
    deterministic charged-off amount (償却額) — the outstanding principal at the
    bankrupt-class 100% loss — is surfaced on ``loan_writeoff`` so the banker and
    the ledger record exactly how much was charged off.

    Authority: a write-off is a banker-authority credit judgement, so the
    transition is HITL-gated (guarded against ``HITL_GATED_TRANSITIONS``) and the
    event is authored by the resolved banker — never ``"system"``. A no-loan run,
    a non-WORKOUT status, or a non-gated/illegal pair yields an append-only no-op
    (the advisory ``loan_writeoff`` amount is still surfaced).

    Args:
        state: Current graph state (requires an attached WORKOUT facility).
        config: LangGraph run config (injected; unused beyond symmetry).

    Returns:
        Partial state update with ``loan_writeoff`` (the charged-off amount +
        reason) and any HITL-gated ``loan_events`` to append.
    """
    amount = _written_off_amount(state)
    actor = _resolve_actor(state)
    loan_events = _writeoff_event(state, actor=actor, amount=amount)

    # Durable side-record: persist the gated 償却 transition to the append-only
    # loan ledger (offline no-op without SAISEI_LOAN_DSN). Never affects the
    # return, a gate, a route, or a figure, and is never fatal.
    from app.backend.portfolio.loan_store_postgres import persist_loan_events

    persist_loan_events(state, loan_events, log_event="writeoff.loan_persisted")
    _record_watchlist(state, loan_events)

    _log.info(
        "writeoff.recorded",
        loan_id=state.loan_id,
        amount=amount,
        recorded=bool(loan_events),
    )

    return {
        "loan_writeoff": {
            "written_off_amount": amount,
            "written_off_amount_formatted": (format_jpy(amount) if amount > 0 else None),
            "recorded": bool(loan_events),
            "reason": (
                f"償却額 {format_jpy(amount)} （引当対象残高の全額を償却） "
                f"(charged off {format_jpy(amount)}: full outstanding at "
                "bankrupt-class loss)"
                if amount > 0
                else "償却額: 未評価（残高未確認） (write-off amount: outstanding unknown)"
            ),
            "note": (
                "The 償却 (write-off) transition is HITL-gated; the banker decides. "
                "The charged-off amount is the deterministic outstanding principal "
                "at the bankrupt-class loss and feeds no gate, route, or decision."
            ),
        },
        "loan_events": loan_events,
    }
