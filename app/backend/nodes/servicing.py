"""Loan-servicing node — deterministic non-distress lifecycle transitions.

The graph-side realisation of the servicing spine
(:func:`app.shared.models.loan.proposed_servicing_transition`). It advances a
facility along the **performing arc** of its life — the non-distress middle of
the lifecycle that neither origination (``loan_origination`` / ``disbursement``)
nor the distress half (``workout`` / the turnaround HITL approve path) covers:

    DISBURSED (実行) → PERFORMING (正常)   — a drawn-down facility enters servicing
    PERFORMING (正常) → CLOSED (完済)      — full repayment

Why this is safe (the same invariant the rest of Saisei enforces)
----------------------------------------------------------------
1. **Deterministic.** The transition is proposed by the pure domain helper from
   the facility's current status and outstanding principal. No LLM is involved
   and no figure is produced here.
2. **Non-distress, non-gated by construction.** Both transitions are members of
   :data:`~app.shared.models.loan.SERVICING_TRANSITIONS`, which is *disjoint*
   from :data:`~app.shared.models.loan.HITL_GATED_TRANSITIONS`. A servicing move
   is an operational fact (a facility entered normal servicing; a facility was
   fully repaid), never a banker-authority credit / distress judgement. The
   node defensively asserts this before recording — it can never record a
   条件変更 / 管理回収 / 償却 transition (those stay owned by the depth half and
   remain HITL-gated).
3. **Append-only + durable.** The transition is a single immutable
   :class:`~app.shared.models.loan.LoanEvent` appended to the in-state log AND
   best-effort persisted to the dedicated, append-only, tenant-scoped loan
   ledger (offline no-op without ``SAISEI_LOAN_DSN``), exactly like
   ``disbursement_node`` / ``workout_node``.
4. **Best-effort watchlist.** The now-正常 / 完済 facility is upserted into the
   SAME watchlist book, carrying its new lifecycle status. Write-only, never
   fatal, offline no-op.

Authority note: ``confirm`` (実行 → 正常) and ``repay`` (正常 → 完済) are
operational steps an authorised operator records — not a fresh credit decision —
so the event is authored by ``"system"``, mirroring the deterministic
``disbursement_node`` bootstrap. There is no negotiation on this path by design.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph can resolve the ``config`` node parameter type at ``add_node`` time
without emitting a spurious UserWarning (mirrors ews_scoring / workout /
disbursement).
"""

import datetime as dt
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.backend.state import SaiseiState
from app.shared.logging import get_logger
from app.shared.models.loan import (
    LoanEvent,
    LoanStatus,
    current_status,
    is_servicing_transition,
    outstanding_principal_for_state,
    proposed_servicing_transition,
)

__all__ = ["servicing_node", "SERVICING_ACTIONS"]

_log = get_logger(__name__)

#: The closed set of servicing actions the node accepts, each mapping to a
#: deterministic servicing move along the performing arc:
#:   'confirm'      -> 実行 → 正常 (DISBURSED → PERFORMING)
#:   'repay_amount' -> 一部入金: record principal_repaid on a PERFORMING /
#:                     RESTRUCTURED self-event; auto-closes to 完済 at zero balance.
#:   'repay'        -> 完済: full repayment (records the remaining balance as a
#:                     repayment, then → CLOSED) -- a 'repay_amount' of the whole
#:                     outstanding balance.
SERVICING_ACTIONS: frozenset[str] = frozenset({"confirm", "repay", "repay_amount"})


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _repayment_amount(state: SaiseiState, action: str, outstanding: int) -> int:
    """Resolve the principal to repay in this servicing action (integer yen).

    - ``repay``: the FULL remaining outstanding balance (完済) -- the binary
      payoff, now recorded truthfully as a repayment of the real balance rather
      than a bare status flip.
    - ``repay_amount``: the banker-supplied ``servicing_amount`` (一部入金),
      capped at the outstanding balance so a facility can never repay more than
      it owes (a defensive clamp; the domain model also rejects over-repayment).
    - ``confirm``: 0 (no money moves on entering normal servicing).
    """
    if action == "repay":
        return outstanding
    if action == "repay_amount":
        return min(max(0, int(state.servicing_amount)), outstanding)
    return 0


def _servicing_events(state: SaiseiState, action: str) -> list[dict[str, Any]]:
    """Record the deterministic servicing event(s) implied by ``action``.

    Three operational shapes, all non-distress and non-gated:

    * ``confirm``: a single DISBURSED → PERFORMING status event (実行→正常).
    * ``repay_amount`` / ``repay``: a partial-repayment SELF-event carrying
      ``principal_repaid`` (一部入金); when that paydown brings the outstanding
      balance to zero, a follow-on → CLOSED (完済) status event is appended in the
      SAME return so a fully-repaid facility lands closed atomically.

    Every recorded transition is asserted to be a servicing (non-HITL-gated)
    move before it is recorded, so this node can never record a credit /
    distress transition. Returns ``[]`` (append-only no-op) when no loan is
    attached, the log is empty/malformed, or no legal servicing move applies.
    """
    if not state.loan_id or not state.loan_events:
        return []
    try:
        current = current_status([LoanEvent.model_validate(e) for e in state.loan_events])
    except Exception as exc:  # noqa: BLE001 - never break the servicing path
        _log.warning("servicing.loan_status_failed", error=str(exc), loan_id=state.loan_id)
        return []

    if action == "confirm":
        return _confirm_events(state, current)
    return _repay_events(state, current, action)


def _confirm_events(state: SaiseiState, current: LoanStatus) -> list[dict[str, Any]]:
    """Record the DISBURSED → PERFORMING (実行→正常) status event, if legal."""
    target = proposed_servicing_transition(current, outstanding=1)
    # confirm only applies from DISBURSED; proposed_servicing_transition returns
    # PERFORMING there (the outstanding arg is irrelevant to that branch).
    if target is not LoanStatus.PERFORMING or not is_servicing_transition(current, target):
        return []
    event = LoanEvent(
        status=target,
        at=_now_utc(),
        actor="system",
        note=f"Servicing (confirm): {current.value} -> {target.value} (正常)",
    )
    _log.info(
        "servicing.loan_transition",
        loan_id=state.loan_id,
        action="confirm",
        current=current.value,
        target=target.value,
    )
    return [event.model_dump(mode="json")]


def _repay_events(state: SaiseiState, current: LoanStatus, action: str) -> list[dict[str, Any]]:
    """Record a partial-repayment self-event (+ a 完済 close when fully repaid).

    A repayment is only legal from a servicing self-loop status (PERFORMING /
    RESTRUCTURED). The repaid amount is recorded as ``principal_repaid`` on a
    self-event; when the resulting balance is zero a follow-on → CLOSED event is
    appended so the facility lands 完済 atomically.
    """
    if not is_servicing_transition(current, current):
        # current is not a repayment self-loop status (e.g. DISBURSED) -> no-op.
        return []
    outstanding = outstanding_principal_for_state(state)
    amount = _repayment_amount(state, action, outstanding)
    if amount <= 0:
        # No balance to repay (no principal baseline known) -> append-only
        # no-op. A 完済 must record the truthful payoff of a real balance; a
        # facility with an unknown baseline is not closed by a bare 'repay'.
        return []
    at = _now_utc()
    events: list[dict[str, Any]] = [
        LoanEvent(
            status=current,
            at=at,
            actor="system",
            note=f"Servicing ({action}): 一部入金 {amount} (principal_repaid)",
            principal_repaid=amount,
        ).model_dump(mode="json")
    ]
    remaining = outstanding - amount
    if remaining <= 0:
        close_target = proposed_servicing_transition(current, outstanding=0)
        if close_target is LoanStatus.CLOSED and is_servicing_transition(current, close_target):
            events.append(
                LoanEvent(
                    status=close_target,
                    at=at,
                    actor="system",
                    note=(
                        f"Servicing ({action}): {current.value} -> "
                        f"{close_target.value} (完済, fully repaid)"
                    ),
                ).model_dump(mode="json")
            )
    _log.info(
        "servicing.repayment",
        loan_id=state.loan_id,
        action=action,
        amount=amount,
        remaining=max(0, remaining),
        closed=remaining <= 0,
    )
    return events


def _record_watchlist(state: SaiseiState, new_events: list[dict[str, Any]]) -> None:
    """Best-effort upsert of the serviced facility into the watchlist book.

    Records the facility into the SAME Portfolio watchlist the assessment and
    origination paths use, carrying its NEW lifecycle status. The node's
    just-recorded events are not yet in ``state.loan_events`` (LangGraph applies
    the return after the node), so a state view with the new events merged in is
    passed so the recorded status reflects the transition the node just made
    (正常 / 完済). Write-only, never fatal, offline no-op — mirrors the
    origination orchestrator's helper of the same name.
    """
    if not new_events:
        return
    try:
        from app.backend.portfolio.recorder import record_origination_snapshot

        merged = state.model_copy(update={"loan_events": [*state.loan_events, *new_events]})
        record_origination_snapshot(state=merged)
    except Exception as exc:  # noqa: BLE001 - watchlist is best-effort, never fatal
        _log.warning("servicing.watchlist_failed", error=str(exc))


def servicing_node(state: SaiseiState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Record a deterministic, non-distress loan-servicing transition.

    Reads ``state.servicing_action`` ('confirm' → 実行→正常; 'repay_amount' → 一部入金;
    'repay' → 完済), records the implied servicing event(s) as immutable
    LoanEvents, persists them to the append-only loan ledger, and upserts the
    facility into the watchlist book — all best-effort and never fatal. An
    unknown / missing action, an unattached loan, or a move that is not legal
    from the current status yields an append-only no-op (and, for an unknown
    action, an error string), so the node never breaks a run.

    Args:
        state: Current graph state (requires an attached facility + a
            ``servicing_action``).
        config: LangGraph run config (injected; unused beyond symmetry).

    Returns:
        Partial state update appending any servicing ``loan_events`` (or a
        no-op / an ``errors`` entry for an unknown action).
    """
    action = str(state.servicing_action or "").strip().lower()
    if action not in SERVICING_ACTIONS:
        _log.warning("servicing.invalid_action", action=action)
        return {
            "errors": [
                *state.errors,
                f"Invalid servicing action: {action!r}",
            ]
        }

    loan_events = _servicing_events(state, action)

    # Durable side-record: persist the servicing transition to the dedicated
    # append-only loan ledger (offline no-op without SAISEI_LOAN_DSN). Never
    # affects the return, a gate, a route, or a figure, and is never fatal.
    from app.backend.portfolio.loan_store_postgres import persist_loan_events

    persist_loan_events(state, loan_events, log_event="servicing.loan_persisted")
    # Upsert the now-正常 / 完済 facility into the watchlist book.
    _record_watchlist(state, loan_events)

    _log.info("servicing.recorded", action=action, recorded=bool(loan_events))
    return {"loan_events": loan_events}
