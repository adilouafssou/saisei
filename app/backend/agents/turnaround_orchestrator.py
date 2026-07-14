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

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph can resolve the ``config`` node parameter type at ``add_node`` time
without emitting a spurious UserWarning about its annotation.
"""

import datetime as dt
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.record import record_event
from app.backend.observability import capture_hitl_feedback
from app.backend.state import (
    NegotiationDecision,
    ReconciliationOutcome,
    SaiseiState,
    Strategy,
)
from app.backend.trajectory.record import TrajectoryDecision
from app.backend.trajectory.recorder import build_node_trajectory, record_trajectory
from app.shared.logging import get_logger
from app.shared.models.loan import (
    HITL_GATED_TRANSITIONS,
    LoanEvent,
    current_status,
    outstanding_principal_for_state,
    proposed_transition_for,
    provision_amount,
)
from app.shared.models.money import format_jpy

__all__ = ["hitl_negotiation_node"]

_log = get_logger(__name__)


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _persist_loan_events(state: SaiseiState, events: list[dict[str, Any]]) -> None:
    """Best-effort durable append of new loan events to the loan store.

    Mirrors the audit ledger's ``record_event`` posture (and the workout node's
    helper of the same name): a strict side-record that NEVER affects the node's
    return, a gate, a route, or a figure, and is NEVER fatal. The events are
    already in the LangGraph checkpointer state (via the node's ``loan_events``
    return); this additionally persists them to the dedicated, append-only,
    tenant-scoped loan ledger so a facility's lifecycle is durable in its own
    store.

    Offline (no ``SAISEI_LOAN_DSN``) the factory returns ``NullLoanStore`` and
    this is a no-op, keeping the system byte-stable. A no-op also occurs when no
    loan is attached or there are no new events.

    Args:
        state: Current graph state (source of ``loan_id`` + tenant scope).
        events: The newly recorded LoanEvent dicts the node is appending.
    """
    if not events or not state.loan_id:
        return
    try:
        from app.backend.portfolio.loan_store_postgres import get_loan_store
        from app.shared.settings import get_settings

        settings = get_settings()
        store = get_loan_store(settings.loan_dsn)
        tenant_id = settings.loan_tenant_default
        for raw in events:
            store.append(tenant_id, state.loan_id, LoanEvent.model_validate(raw))
        _log.info("hitl.loan_persisted", loan_id=state.loan_id, count=len(events))
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort, never fatal
        _log.warning("hitl.loan_persist_failed", error=str(exc), loan_id=state.loan_id)


def _capture_decision(
    state: SaiseiState,
    decision: NegotiationDecision,
    *,
    approved_strategy: Strategy | None,
    revision_note: str | None,
) -> bool:
    """Best-effort capture of the banker's decision to the LangSmith corpus.

    Calls :func:`~app.backend.observability.capture_hitl_feedback`, which is a
    strict offline no-op (returns ``False``, zero network) when LangSmith is not
    configured. Wrapped so a capture failure can NEVER break the graph node:
    observability is best-effort and must not affect the deterministic decision
    that has already been validated.

    Args:
        state: Current graph state at the time of the decision.
        decision: The validated banker decision.
        approved_strategy: The approved strategy (approve path) or None.
        revision_note: The banker's revision note (revise/reject path) or None.

    Returns:
        ``True`` when the decision was actually captured to LangSmith; ``False``
        when capture was an offline no-op (not configured) or failed. The return
        value lets the caller (and the logs) distinguish a real capture from a
        silent no-op instead of discarding that signal.
    """
    try:
        captured = capture_hitl_feedback(
            tdb_code=state.tdb_code,
            decision=decision.value,
            strategies=[s.model_dump() for s in state.proposed_strategies],
            approved_strategy=(approved_strategy.model_dump() if approved_strategy else None),
            revision_note=revision_note,
            reconciliation_required=state.reconciliation_required,
            reconciliation_details=state.reconciliation_details,
            feasibility_notes=state.feasibility_notes,
            fsa_classification=(
                state.fsa_classification.value if state.fsa_classification else None
            ),
            working_capital_gap=state.working_capital_gap,
        )
    except Exception as exc:  # noqa: BLE001 - capture is best-effort, never fatal
        _log.warning("hitl.capture_failed", error=str(exc), tdb_code=state.tdb_code)
        return False
    if captured:
        _log.info("hitl.captured", decision=decision.value, tdb_code=state.tdb_code)
    else:
        _log.debug(
            "hitl.capture_skipped",
            decision=decision.value,
            tdb_code=state.tdb_code,
            reason="langsmith_not_configured_or_noop",
        )
    return bool(captured)


def _loan_summary(state: SaiseiState) -> dict[str, Any] | None:
    """Build an advisory loan-lifecycle summary for the HITL payload.

    Read-only display data surfaced to the banker alongside the strategies: the
    attached facility's current status (romanized + kanji + English), its
    outstanding principal, the FSA-implied proposed transition (条件変更 / 管理回収
    or none), and the deterministic loan-loss provision (貸倒引当金) the current
    classification implies. Every figure is computed deterministically; nothing
    here feeds a gate, route, or decision — it only informs the banker.

    Returns ``None`` when no loan is attached (no log), so the payload simply
    omits the block on runs without a facility (offline / backward-compatible).

    Args:
        state: Current graph state.

    Returns:
        The advisory loan summary dict, or ``None`` when no loan is attached.
    """
    if not state.loan_events:
        return None
    try:
        events = [LoanEvent.model_validate(e) for e in state.loan_events]
        status = current_status(events)
    except Exception as exc:  # noqa: BLE001 - display must never break the pause
        _log.warning("hitl.loan_summary_failed", error=str(exc), loan_id=state.loan_id)
        return None

    principal = _outstanding_principal(state)
    fsa = state.fsa_classification
    proposed = proposed_transition_for(fsa, status) if fsa is not None else None
    provision = (
        provision_amount(
            principal,
            fsa,
            special_attention=bool(state.special_attention),
        )
        if fsa is not None and principal > 0
        else None
    )
    return {
        "loan_id": state.loan_id,
        "status": status.value,
        "status_kanji": status.kanji,
        "status_english": status.english,
        "outstanding_principal": principal,
        "outstanding_principal_formatted": (format_jpy(principal) if principal > 0 else None),
        "proposed_transition": proposed.value if proposed else None,
        "proposed_transition_kanji": proposed.kanji if proposed else None,
        "provision_amount": provision,
        "provision_amount_formatted": (format_jpy(provision) if provision is not None else None),
        "note": (
            "Advisory only: deterministic loan-lifecycle status and the "
            "FSA-implied transition / provision. The banker decides; this "
            "figure feeds no gate, route, or decision."
        ),
    }


def _outstanding_principal(state: SaiseiState) -> int:
    """Return the facility's TRUE outstanding principal (残高), or 0.

    Delegates to the shared :func:`outstanding_principal_for_state` seam: the
    lender-stakes sum (the original principal the intake bootstrap sets) minus
    the cumulative ``principal_repaid`` on the loan log. With no repayments this
    equals the old proxy exactly; once a facility amortizes it is the real
    declining balance, so the loan-loss provision (貸倒引当金) reserves against the
    truthful exposure. Returns 0 when no stake data is present.
    """
    return outstanding_principal_for_state(state)


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
        # MR #2: Surface reconciliation details so the banker sees the
        # LLM-vs-floor disagreement with full context. Advisory only — the
        # banker resolves the disagreement; no gate or figure is affected.
        "reconciliation_required": state.reconciliation_required,
        "reconciliation_details": state.reconciliation_details,
        "reconciliation_prompt": (
            "The deterministic feasibility floor and the LLM feasibility signal "
            "disagree significantly for one or more strategies (see "
            "reconciliation_details). Please review and confirm whether to "
            "proceed with the deterministic assessment or request a revision."
        )
        if state.reconciliation_required
        else None,
        # MR2 (outcome capture): ask the banker who was right so the resolution
        # is recorded in the permanent who-was-right corpus. Optional and
        # advisory only — a missing/blank verdict captures as '' (not
        # adjudicated) and never affects any gate, route, or figure.
        "reconciliation_verdict_prompt": (
            "After deciding, optionally record who was right for each routed "
            "disagreement so the reconciliation thresholds can be calibrated: "
            "'floor' (deterministic band was right), 'llm' (LLM band was right), "
            "'neither', or leave blank if not adjudicated. Pass as "
            "reconciliation_verdict in the resume payload."
        )
        if state.reconciliation_required
        else None,
        # Loan-lifecycle surfacing: advisory loan status + FSA-implied
        # transition + deterministic provision (貸倒引当金). Omitted (None) when no
        # loan is attached. Display only — never feeds a gate, route, or figure.
        "loan": _loan_summary(state),
    }


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    """Extract the run thread_id from a LangGraph RunnableConfig (or '').

    The thread_id lives in the run config (``configurable.thread_id``), not in
    ``SaiseiState``, so audit call sites read it here to key the hash chain
    (same helper as ``ews_scoring._thread_id_from_config``).
    """
    if not config:
        return ""
    configurable = config.get("configurable") or {}
    return str(configurable.get("thread_id", "") or "")


def _resolve_actor(response: dict[str, Any]) -> str:
    """Resolve the banker identity for a human_decision audit event.

    Prefers an explicit actor in the resume payload (``actor`` / ``banker_id``);
    falls back to the identity seam (``current_actor``), which today returns the
    configured ``audit_actor_default`` placeholder and, once Feature 6 (auth/
    OIDC) lands, the real authenticated banker id — with no further change here.
    Read defensively so a missing/blank value never raises on the decision path.
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


def _record_human_decision(
    state: SaiseiState,
    *,
    decision: NegotiationDecision,
    response: dict[str, Any],
    config: RunnableConfig | None,
    approved_strategy: Strategy | None,
    revision_note: str | None,
    flags: dict[str, bool],
) -> None:
    """Best-effort immutable audit record of the banker's HITL decision (spec §7).

    Emits one ``human_decision`` event capturing WHO decided WHAT, WHEN, against
    which data/threshold version (the version hashes are computed inside
    ``record_event``). Captures the decision, the approved strategy title /
    index, the commitment flags, the revision note, and the negotiation status.

    Side-record ONLY: this is called AFTER the node's return dict is assembled
    and never mutates graph state, a gate, a route, or a figure. Best-effort and
    never fatal (``record_event`` swallows; the call site is additionally
    guarded). Offline (no ``audit_dsn``) -> NullAuditSink -> no-op.
    """
    try:
        record_event(
            AuditEventType.HUMAN_DECISION,
            state=state,
            thread_id=_thread_id_from_config(config),
            actor=_resolve_actor(response),
            payload={
                "decision": decision.value,
                "strategy_index": response.get("strategy_index"),
                "approved_strategy_title": (approved_strategy.title if approved_strategy else None),
                "revision_note": revision_note,
                "yakuin_hoshu_cut": flags.get("yakuin_hoshu_cut", state.yakuin_hoshu_cut),
                "personal_asset_disposal": flags.get(
                    "personal_asset_disposal", state.personal_asset_disposal
                ),
                "negotiation_status": state.negotiation_status,
                "reconciliation_verdict": response.get("reconciliation_verdict", ""),
            },
        )
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never fatal
        _log.warning("hitl.audit_record_failed", error=str(exc), tdb_code=state.tdb_code)


def _record_trajectory_flywheel(
    state: SaiseiState,
    *,
    decision: NegotiationDecision,
    response: dict[str, Any],
    config: RunnableConfig | None,
    approved_strategy: Strategy | None,
    revision_note: str | None,
) -> None:
    """Best-effort capture of the negotiation as a training trajectory (Feature 3).

    Persists the captured negotiation (inputs digest, proposed strategies,
    banker decision + note, approved strategy, final plan) to the agent-
    trajectory store, feeding the offline data flywheel. The approved strategy
    is passed explicitly because, on the approve path, it is in the node's
    return dict and not yet on ``state``.

    Side-record ONLY: called after the node's return dict is assembled; never
    mutates graph state, a gate, a route, or a figure. Best-effort and never
    fatal (``record_trajectory`` swallows; this call site is additionally
    guarded). Offline (no ``trajectory_dsn``) -> NullTrajectoryStore -> no-op.
    """
    try:
        record_trajectory(
            state=state,
            decision=TrajectoryDecision(decision.value),
            thread_id=_thread_id_from_config(config),
            actor=_resolve_actor(response),
            revision_note=revision_note or "",
            approved_strategy=approved_strategy,
            # Feature 3.1: capture the FULL agentic path, not just the
            # negotiation summary — the ordered per-node output digests plus the
            # exact interrupt payload the banker saw. Both are write-only
            # side-record data; they change no state, gate, route, or figure.
            node_trajectory=build_node_trajectory(state),
            interrupt_payload=_interrupt_payload(state),
        )
    except Exception as exc:  # noqa: BLE001 - flywheel is best-effort, never fatal
        _log.warning("hitl.trajectory_record_failed", error=str(exc), tdb_code=state.tdb_code)


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


def _reconciliation_outcomes(
    state: SaiseiState,
    *,
    banker_decision: NegotiationDecision,
    response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the who-was-right corpus entries for this HITL resolution.

    Produces one :class:`~app.backend.state.ReconciliationOutcome` per ROUTED
    disagreement — the entries in ``state.reconciliation_details`` with
    ``routed`` truthy (the ones that actually drove the trigger). Audit-only
    details (``routed`` falsey) are skipped, because only the routed
    disagreements reached the banker's attention and can be adjudicated.

    The optional ``reconciliation_verdict`` in the resume payload is the
    banker's who-was-right label; it is validated/normalised by the model's
    field validator ({'floor','llm','neither',''}). A missing verdict captures
    as '' (not adjudicated).

    LEARNING DATA ONLY: this is called AFTER the decision is made. It never
    influences the decision, a gate, a route, or a figure. When there was no
    reconciliation (offline / no routed details) it returns ``[]`` and the
    append-only reducer is a no-op.

    Args:
        state: Current graph state at resolution time.
        banker_decision: The validated banker decision (approve/revise/reject).
        response: The raw resume payload (read for ``reconciliation_verdict``).

    Returns:
        A list of ``ReconciliationOutcome`` dicts (possibly empty) to append.
    """
    if not state.reconciliation_required or not state.reconciliation_details:
        return []

    verdict = response.get("reconciliation_verdict", "")
    outcomes: list[dict[str, Any]] = []
    for detail in state.reconciliation_details:
        if not detail.get("routed", False):
            continue  # audit-only disagreement — never adjudicated
        outcome = ReconciliationOutcome(
            strategy_title=str(detail.get("strategy_title", "")),
            deterministic_band=str(detail.get("deterministic_band", "")),
            llm_band=str(detail.get("llm_band", "")),
            band_distance=int(detail.get("band_distance", 0)),
            banker_decision=banker_decision.value,
            banker_verdict=verdict,
        )
        outcomes.append(outcome.model_dump())
    return outcomes


def _loan_events(state: SaiseiState, *, response: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the loan-lifecycle event(s) recorded by an approved decision.

    When a loan is attached to the run (``state.loan_id``) and its current FSA
    classification implies a distress transition (条件変更 / 管理回収 via
    :func:`proposed_transition_for`) that is legal from the loan's current
    status, the banker's APPROVE records that transition as a single immutable
    :class:`~app.shared.models.loan.LoanEvent` appended to the append-only log.

    This is the realisation point of an HITL-gated transition: it runs ONLY on
    the approve path, AFTER the banker has decided. The proposed transition is
    always one of :data:`HITL_GATED_TRANSITIONS` by construction (a defensive
    guard asserts this), so the banker is, by definition, the decider. No gate,
    route, or figure reads the result.

    Returns ``[]`` (append-only reducer no-op) when no loan is attached, no FSA
    classification is set, the classification implies no transition, or the
    implied transition is not legal from the current status.

    Args:
        state: Current graph state at resolution time.
        response: The raw resume payload (read for an optional actor id).

    Returns:
        A list with zero or one ``LoanEvent`` dict to append.
    """
    if not state.loan_id or state.fsa_classification is None:
        return []
    if not state.loan_events:
        return []  # no prior log to transition from (loan not yet disbursed here)

    try:
        current = current_status([LoanEvent.model_validate(e) for e in state.loan_events])
    except Exception as exc:  # noqa: BLE001 - never break the decision path
        _log.warning("hitl.loan_status_failed", error=str(exc), loan_id=state.loan_id)
        return []

    target = proposed_transition_for(state.fsa_classification, current)
    if target is None:
        return []
    # Defensive: every transition this helper can record must be HITL-gated.
    if (current, target) not in HITL_GATED_TRANSITIONS:
        _log.warning(
            "hitl.loan_transition_not_gated",
            loan_id=state.loan_id,
            current=current.value,
            target=target.value,
        )
        return []

    event = LoanEvent(
        status=target,
        at=_now_utc(),
        actor=_resolve_actor(response),
        note=(
            f"FSA {state.fsa_classification.value} approved at HITL: "
            f"{current.value} -> {target.value}"
        ),
    )
    _log.info(
        "hitl.loan_transition",
        loan_id=state.loan_id,
        current=current.value,
        target=target.value,
    )
    return [event.model_dump(mode="json")]


def hitl_negotiation_node(
    state: SaiseiState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Interrupt for banker negotiation and apply the resumed decision.

    Args:
        state: Current graph state (requires ``proposed_strategies``).
        config: LangGraph run config (injected); used only to read the thread_id
               for the best-effort ``human_decision`` audit event.

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
        _capture_decision(state, decision, approved_strategy=approved, revision_note=None)
        loan_events = _loan_events(state, response=response)
        # Durable side-record: persist the banker-approved 条件変更 / 管理回収
        # transition to the dedicated append-only loan ledger (offline no-op
        # without SAISEI_LOAN_DSN). Never affects the return, a gate, a route,
        # or a figure, and never fatal.
        _persist_loan_events(state, loan_events)
        result: dict[str, Any] = {
            "negotiation_decision": decision,
            "approved_strategy": approved,
            "revision_note": None,
            "reconciliation_outcomes": _reconciliation_outcomes(
                state, banker_decision=decision, response=response
            ),
            "loan_events": loan_events,
            **flags,
        }
        _record_human_decision(
            state,
            decision=decision,
            response=response,
            config=config,
            approved_strategy=approved,
            revision_note=None,
            flags=flags,
        )
        _record_trajectory_flywheel(
            state,
            decision=decision,
            response=response,
            config=config,
            approved_strategy=approved,
            revision_note=None,
        )
        return result

    note = response.get("revision_note")
    _log.info("hitl.decided", decision=decision.value)
    _capture_decision(state, decision, approved_strategy=None, revision_note=note)
    result = {
        "negotiation_decision": decision,
        "revision_note": note,
        "approved_strategy": None,
        "reconciliation_outcomes": _reconciliation_outcomes(
            state, banker_decision=decision, response=response
        ),
        **flags,
    }
    _record_human_decision(
        state,
        decision=decision,
        response=response,
        config=config,
        approved_strategy=None,
        revision_note=note,
        flags=flags,
    )
    _record_trajectory_flywheel(
        state,
        decision=decision,
        response=response,
        config=config,
        approved_strategy=None,
        revision_note=note,
    )
    return result
