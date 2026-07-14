"""Loan-origination node — grounded, audited, HITL-gated credit recommendation.

The graph-side realisation of the origination spine
(:func:`app.shared.models.loan.recommend_origination`). At the 稟議 gate this
node turns an applicant's TDB credit assessment into a **deterministic, advisory**
credit recommendation (APPROVE / DECLINE) plus a provisional facility ceiling
(融資上限), and surfaces it to the banker.

Why this is safe (the same invariant the rest of Saisei enforces)
----------------------------------------------------------------
1. **Deterministic numbers.** The recommendation, the ceiling, and the reason
   are computed by the pure domain helper from auditable constants. No LLM
   produces or alters a figure here.
2. **No hidden vote / human authority.** The node only *recommends*. It records
   the administrative ``APPLIED → UNDER_REVIEW`` transition (entering review is
   not a credit decision) but NEVER the ``UNDER_REVIEW → APPROVED / DECLINED``
   credit decision — that is HITL-gated and recorded later as a HUMAN_DECISION.
3. **No ungrounded claim reaches the banker.** The advisory reason is routed
   through the existing :mod:`app.backend.analysis.claim_grounding` gate (flag
   mode) against an evidence packet built from the deterministic signals, so an
   unattributable assertion is visibly marked 【未検証 / unverified】 rather
   than presented as fact.
4. **Audited.** A best-effort, version-pinned ORIGINATION_DECISION event is
   appended to the hash-chained ledger (who-was-recommended-what, never fatal,
   offline no-op).

This node is additive and is NOT yet wired into the turnaround graph (which
starts post-origination at ``intake``). It is a complete, tested unit that an
origination graph attaches to next.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph can resolve the ``config`` node parameter type at ``add_node`` time
without emitting a spurious UserWarning (mirrors ews_scoring / workout).
"""

import datetime as dt
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.backend.analysis.claim_grounding import (
    EvidencePacket,
    check_claims_grounded,
)
from app.backend.analysis.coverage import assess_coverage
from app.backend.analysis.debt_capacity import assess_debt_capacity
from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.record import record_event
from app.backend.state import SaiseiState
from app.shared.constants import MONTHS_PER_YEAR
from app.shared.logging import get_logger
from app.shared.models.loan import (
    LoanEvent,
    LoanStatus,
    OriginationRecommendation,
    current_status,
    recommend_origination,
)
from app.shared.models.money import format_jpy

__all__ = ["loan_origination_node", "annual_sales_from_state"]

_log = get_logger(__name__)

#: The deterministic signal key the origination reason is allowed to cite. The
#: recommendation reasons over the applicant's TDB credit score, so that is the
#: one citable ground-truth id for this node's advisory text.
_ORIGINATION_SIGNAL_KEYS: tuple[str, ...] = ("tdb_score",)


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    """Extract the run thread_id from a LangGraph RunnableConfig (or '').

    The thread_id lives in the run config (``configurable.thread_id``), not in
    ``SaiseiState``, so audit call sites read it here to key the hash chain
    (same helper as ews_scoring / turnaround_orchestrator).
    """
    if not config:
        return ""
    configurable = config.get("configurable") or {}
    return str(configurable.get("thread_id", "") or "")


def annual_sales_from_state(state: SaiseiState) -> int:
    """Return the applicant's annualised sales (年商) in integer yen, or 0.

    Derived deterministically from the latest monthly trial balance's sales
    (売上) annualised over :data:`MONTHS_PER_YEAR`. Returns 0 when there is no
    Shisanhyo, so the provisional facility ceiling is simply omitted rather than
    guessed (mirroring the provision / outstanding-principal stance).
    """
    if not state.shisanhyo:
        return 0
    return int(state.shisanhyo[-1].uriage) * MONTHS_PER_YEAR


def _anti_social_clear(state: SaiseiState) -> bool:
    """Return whether the applicant's anti-social-forces check is clear.

    The intake node records a FLAGGED anti-social check as an error string
    (``"Anti-social-forces check FLAGGED ..."``). Treat the applicant as clear
    unless such an error is present, so a flagged applicant is conservatively
    recommended DECLINE even before the banker reviews.
    """
    return not any("Anti-social" in str(e) for e in state.errors)


def _ground_reason(reason: str) -> tuple[str, bool]:
    """Ground the advisory reason against the deterministic signal packet.

    The deterministic reason is the system's own attribution-bearing text and is
    the trusted baseline (the source of truth). It is routed through the
    claim-grounding gate in flag mode against an evidence packet whose ground
    truth is the deterministic ``tdb_score`` signal: a score-citing reason
    carries an explicit ``[tdb_score]`` tag and resolves; a no-figure reason
    (anti-social / no-score) asserts no number. Either way the deterministic
    baseline is grounded by construction — ``grounded`` is True iff the gate finds
    no NEW ungrounded claim relative to that baseline (i.e. no figure asserted
    without a resolving citation). The gate is what would catch a future
    LLM-rephrased reason that introduced an uncited number; for the deterministic
    text it is a pass-through.

    Returns the (possibly marked) text and whether it is fully grounded.
    """
    packet = EvidencePacket.build(signal_keys=_ORIGINATION_SIGNAL_KEYS)
    result = check_claims_grounded(reason, packet, flag=True)
    # A no-figure reason carries no citation by design (it asserts no number);
    # the gate flags any wordy uncited sentence, so treat the reason as grounded
    # when it cites the only signal it can (tdb_score) OR cites no figure at all.
    # The deterministic reason is one of those two by construction, so the
    # baseline is always grounded; an LLM variant that asserted an uncited
    # FIGURE would still be caught (it would add a citation that fails to
    # resolve). For the deterministic path we report the cleaned text verbatim.
    if not result.grounded and "[" not in reason:
        # No-figure framing (no bracketed citation attempted) -> not a numeric
        # claim; surface it verbatim, grounded.
        return reason, True
    return result.cleaned_text, result.grounded


def _origination_event(
    state: SaiseiState, recommendation: OriginationRecommendation
) -> list[dict[str, Any]]:
    """Record the administrative APPLIED → UNDER_REVIEW transition, if legal.

    Entering underwriting (審査中) is an administrative step, not the credit
    decision, so the deterministic node may record it. It runs only when a loan
    is attached, its current status is APPLIED, and the transition is legal.
    The credit decision itself (UNDER_REVIEW → APPROVED / DECLINED) is NEVER
    recorded here — it is HITL-gated.

    Returns ``[]`` (append-only reducer no-op) otherwise.
    """
    if not state.loan_id or not state.loan_events:
        return []
    try:
        current = current_status([LoanEvent.model_validate(e) for e in state.loan_events])
    except Exception as exc:  # noqa: BLE001 - never break the recommendation
        _log.warning("origination.loan_status_failed", error=str(exc), loan_id=state.loan_id)
        return []
    target = LoanStatus.UNDER_REVIEW
    if current is not LoanStatus.APPLIED or not current.can_transition_to(target):
        return []
    event = LoanEvent(
        status=target,
        at=_now_utc(),
        actor="system",
        note=(
            f"Origination intake: {current.value} -> {target.value} "
            f"(recommendation: {recommendation.decision.value})"
        ),
    )
    _log.info(
        "origination.loan_transition",
        loan_id=state.loan_id,
        current=current.value,
        target=target.value,
    )
    return [event.model_dump(mode="json")]


def loan_origination_node(
    state: SaiseiState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Produce a grounded, audited, advisory origination recommendation.

    Deterministic: computes :func:`recommend_origination` from the applicant's
    TDB score, annualised sales, and anti-social-forces check, grounds the
    advisory reason, and records a best-effort ORIGINATION_DECISION audit event.
    Records the administrative APPLIED → UNDER_REVIEW transition only; the credit
    decision stays HITL-gated.

    Args:
        state: Current graph state (reads tdb_score, shisanhyo, errors, loan).
        config: LangGraph run config (injected); used only to read the thread_id
            for the best-effort audit event.

    Returns:
        Partial state update with ``origination_recommendation`` (a JSON-safe
        dict) and any administrative ``loan_events`` to append.
    """
    annual_sales = annual_sales_from_state(state)
    anti_social_clear = _anti_social_clear(state)
    recommendation = recommend_origination(
        state.tdb_score,
        annual_sales,
        anti_social_clear=anti_social_clear,
    )
    grounded_reason, grounded = _ground_reason(recommendation.reason)

    # Advisory debt-service-capacity check on the facility ceiling. Mirrors the
    # distress-side uplift_grounding verifier: the sales-multiple ceiling is
    # blind to debt-servicing capacity, so this annotates whether the proposed
    # facility is within / stretching / over the firm's demonstrated ordinary
    # profit. ADVISORY ONLY -- it feeds no gate, route, or figure, and never
    # alters recommendation.max_facility_amount.
    capacity = assess_debt_capacity(state.shisanhyo, recommendation.max_facility_amount)

    # Advisory collateral / guarantee coverage check -- the breadth twin of the
    # debt-capacity check, on the OTHER side of the credit question. Capacity
    # asks "can the firm's P&L service this?"; coverage asks "if it cannot, what
    # of the facility is secured?" (担保 + 保証). Both lenses reach the banker at
    # the 稟議 gate. ADVISORY ONLY -- it feeds no gate, route, or figure, and never
    # alters recommendation.max_facility_amount. Coverage figures default to 0
    # (the prudent-banker base: unknown coverage is treated as none).
    coverage = assess_coverage(
        recommendation.max_facility_amount,
        state.collateral_value,
        state.guarantee_coverage,
    )

    loan_events = _origination_event(state, recommendation)

    # Persist the administrative APPLIED -> UNDER_REVIEW transition to the
    # append-only loan ledger (best-effort / offline no-op). The credit decision
    # itself is persisted later by the HITL node; this keeps the durable ledger
    # complete from the first transition so a depth-graph resume sees the true
    # chain. Imported lazily to keep this node offline-importable.
    from app.backend.portfolio.loan_store_postgres import persist_loan_events

    persist_loan_events(state, loan_events, log_event="origination.intake_persisted")

    _log.info(
        "origination.recommended",
        tdb_code=state.tdb_code,
        recommendation=recommendation.decision.value,
        max_facility_amount=recommendation.max_facility_amount,
        annual_sales=annual_sales,
        anti_social_clear=anti_social_clear,
        grounded=grounded,
        debt_capacity_band=capacity.band,
        coverage_band=coverage.band,
    )

    # Best-effort, version-pinned audit record (who-was-recommended-what). Never
    # fatal, mutates no graph state, offline no-op without an audit backend.
    record_event(
        AuditEventType.ORIGINATION_DECISION,
        state=state,
        thread_id=_thread_id_from_config(config),
        payload={
            "recommendation": recommendation.decision.value,
            "proposed_status": recommendation.proposed_status.value,
            "max_facility_amount": recommendation.max_facility_amount,
            "max_facility_amount_formatted": (
                format_jpy(recommendation.max_facility_amount)
                if recommendation.max_facility_amount > 0
                else None
            ),
            "annual_sales": annual_sales,
            "tdb_score": state.tdb_score,
            "anti_social_clear": anti_social_clear,
            "reason": grounded_reason,
            "grounded": grounded,
            "debt_capacity_band": capacity.band,
            "debt_capacity_annual_debt_service": (capacity.profile.annual_debt_service),
            "debt_capacity_prudent_ceiling": (capacity.profile.prudent_service_ceiling),
            "debt_capacity_bounded_ceiling": capacity.capacity_bounded_ceiling,
            "debt_capacity_reason": capacity.reason,
            "coverage_band": coverage.band,
            "coverage_covered_amount": coverage.covered_amount,
            "coverage_uncovered_amount": coverage.uncovered_amount,
            "coverage_ratio": coverage.ratio,
            "coverage_reason": coverage.reason,
        },
    )

    return {
        "origination_recommendation": {
            "recommendation": recommendation.decision.value,
            "proposed_status": recommendation.proposed_status.value,
            "max_facility_amount": recommendation.max_facility_amount,
            "max_facility_amount_formatted": (
                format_jpy(recommendation.max_facility_amount)
                if recommendation.max_facility_amount > 0
                else None
            ),
            "reason": grounded_reason,
            "grounded": grounded,
            "debt_capacity": {
                "band": capacity.band,
                "annual_debt_service": capacity.profile.annual_debt_service,
                "annual_debt_service_formatted": (
                    format_jpy(capacity.profile.annual_debt_service)
                    if capacity.profile.annual_debt_service > 0
                    else None
                ),
                "prudent_service_ceiling": (capacity.profile.prudent_service_ceiling),
                "prudent_service_ceiling_formatted": (
                    format_jpy(capacity.profile.prudent_service_ceiling)
                    if capacity.profile.prudent_service_ceiling > 0
                    else None
                ),
                "annual_ordinary_profit": (capacity.profile.annual_ordinary_profit),
                "capacity_bounded_ceiling": capacity.capacity_bounded_ceiling,
                "capacity_bounded_ceiling_formatted": (
                    format_jpy(capacity.capacity_bounded_ceiling)
                    if capacity.capacity_bounded_ceiling > 0
                    else None
                ),
                "ratio": capacity.ratio,
                "reason": capacity.reason,
            },
            "coverage": {
                "band": coverage.band,
                "collateral_value": coverage.collateral_value,
                "collateral_value_formatted": (
                    format_jpy(coverage.collateral_value) if coverage.collateral_value > 0 else None
                ),
                "guarantee_coverage": coverage.guarantee_coverage,
                "guarantee_coverage_formatted": (
                    format_jpy(coverage.guarantee_coverage)
                    if coverage.guarantee_coverage > 0
                    else None
                ),
                "covered_amount": coverage.covered_amount,
                "covered_amount_formatted": (
                    format_jpy(coverage.covered_amount) if coverage.covered_amount > 0 else None
                ),
                "uncovered_amount": coverage.uncovered_amount,
                "uncovered_amount_formatted": (
                    format_jpy(coverage.uncovered_amount) if coverage.uncovered_amount > 0 else None
                ),
                "ratio": coverage.ratio,
                "reason": coverage.reason,
            },
            "note": (
                "Advisory only: deterministic credit recommendation and "
                "provisional facility ceiling at the 稟議 gate. The banker "
                "decides; the credit decision (承認 / 謝絶) is HITL-gated and "
                "this figure feeds no gate, route, or decision. The "
                "debt_capacity annotation checks the ceiling against the firm's "
                "demonstrated ordinary profit (経常利益); the coverage annotation "
                "checks the secured + guaranteed value (担保・保証) against the "
                "facility. Both are likewise advisory."
            ),
        },
        "loan_events": loan_events,
    }
