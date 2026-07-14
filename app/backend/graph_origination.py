"""LangGraph builder for the loan-origination graph (融資組成).

The breadth half's graph edge: a small, self-contained StateGraph that drives a
NEW facility application through origination as one continuous, auditable,
HITL-gated record — the same spine the post-origination turnaround graph
(``app.backend.graph``) sits on, but at the front of the lifecycle.

Flow::

    START
      → origination_intake      (seed the APPLIED facility log)
      → loan_origination        (grounded, audited advisory recommendation;
                                 records APPLIED → UNDER_REVIEW)
      → origination_hitl        (interrupt; banker decides 承認 / 謝絖;
                                 records UNDER_REVIEW → APPROVED / DECLINED)
      → route_after_credit_decision:
            approve → disbursement   (records APPROVED → DISBURSED) → END
            decline → END

Every boundary the rest of Saisei enforces holds here: numbers are
deterministic, the advisory reason is grounded, the credit decision is
HITL-gated, and each transition is an immutable event on the append-only loan
ledger. This graph is ADDITIVE — it does not touch the turnaround graph; the two
share only the ``SaiseiState`` schema and the loan-lifecycle spine, so a facility
originated here can later be assessed by the turnaround graph on the same record.

The checkpointer is reused from ``app.backend.graph`` (Postgres or the shared
MemorySaver), so the ``interrupt()`` pause persists exactly like the turnaround
HITL pause.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph resolves node ``config`` parameter types at ``add_node`` time without a
spurious UserWarning (mirrors app.backend.graph).
"""

import datetime as dt
from functools import partial
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.backend.agents.origination_orchestrator import (
    disbursement_node,
    origination_hitl_node,
)
from app.backend.nodes.loan_origination import loan_origination_node
from app.backend.state import SaiseiState
from app.backend.tools.provider import MockDataProvider
from app.backend.tools.tdb_api import AntiSocialCheck
from app.shared.logging import get_logger
from app.shared.models.loan import LoanEvent, LoanStatus

__all__ = [
    "build_origination_graph",
    "compile_origination_graph",
    "origination_intake_node",
    "route_after_credit_decision",
]

_log = get_logger(__name__)


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _resolve_applicant(state: SaiseiState, provider: MockDataProvider) -> dict[str, Any]:
    """Load the applicant's TDB report + financials for the 稟議 recommendation.

    The front-of-lifecycle data step the origination graph was missing: a new
    application arrives as a 7-digit TDB code, and the deterministic credit
    recommendation (``loan_origination_node``) reasons over the applicant's
    ``tdb_score``, annualised sales (from ``shisanhyo``), and anti-social-forces
    check. Without this resolution those were never populated over the service
    surface, so every recommendation collapsed to DECLINE (no score) regardless
    of the applicant. This mirrors the turnaround ``intake_node`` exactly, on
    the SAME data seam (``provider.credit_report`` / ``provider.shisanhyo``), so
    a live TDB / core-banking client swaps in transparently later.

    Caller-supplied values win: any field already set on the initial invoke
    (e.g. an explicit ``tdb_score`` in a unit test, or a pre-attached profile)
    is preserved and never overwritten, so existing callers and the graph tests
    that drive a specific score are unaffected.

    Unknown / malformed code is non-fatal here: the lookup failure is recorded
    as an error string and the run continues on whatever the caller supplied
    (the HTTP surface already rejects malformed codes with 422 before the graph
    runs). A FLAGGED anti-social check is carried as the SAME error-string
    sentinel the turnaround intake uses, which ``loan_origination_node`` reads
    to conservatively recommend DECLINE.

    Args:
        state: Current graph state (requires ``tdb_code``).
        provider: Bound data provider (mock by default; live when configured).

    Returns:
        Partial state update with the resolved profile / score / financials /
        identity / stakes / anti-social error, omitting any field the caller
        already supplied.
    """
    try:
        report = provider.credit_report(state.tdb_code)
    except KeyError:
        _log.warning("origination_intake.unknown_tdb_code", tdb_code=state.tdb_code)
        return {"errors": [*state.errors, f"Unknown TDB code: {state.tdb_code}"]}

    update: dict[str, Any] = {}
    if state.company_profile is None:
        update["company_profile"] = report.profile
    if not state.hojin_bango:
        update["hojin_bango"] = report.profile.hojin_bango
    if state.tdb_score is None:
        update["tdb_score"] = report.tdb_score
    if not state.shisanhyo:
        try:
            update["shisanhyo"] = provider.shisanhyo(report.profile.hojin_bango)
        except Exception as exc:  # noqa: BLE001 - financials are best-effort
            _log.warning(
                "origination_intake.shisanhyo_failed",
                error=str(exc),
                hojin_bango=report.profile.hojin_bango,
            )
    if not state.lender_stakes and report.lender_stakes:
        update["lender_stakes"] = report.lender_stakes

    if report.anti_social_check is AntiSocialCheck.FLAGGED and not any(
        "Anti-social" in str(e) for e in state.errors
    ):
        update["errors"] = [
            *state.errors,
            "Anti-social-forces check FLAGGED — decline; no facility.",
        ]

    _log.info(
        "origination_intake.resolved",
        tdb_code=state.tdb_code,
        hojin_bango=report.profile.hojin_bango,
        tdb_score=report.tdb_score,
        anti_social=report.anti_social_check.value,
    )
    return update


def origination_intake_node(
    state: SaiseiState,
    config: RunnableConfig | None = None,
    provider: MockDataProvider | None = None,
) -> dict[str, Any]:
    """Resolve the applicant and seed the APPLIED facility log for a new run.

    Two responsibilities at the front of the origination lifecycle:

    1. **Resolve the applicant** (:func:`_resolve_applicant`): load the TDB
       credit report + monthly financials on the shared data seam so the 稟議
       recommendation reasons over real signals. Skipped when a loan is already
       attached AND the profile is already present (a fully caller-provided run),
       and per-field caller values always win.
    2. **Seed the APPLIED facility** (申込): when the caller has not supplied a
       loan, seed ``loan_id`` + a single APPLIED event so the downstream nodes
       can record the legal transitions (APPLIED → UNDER_REVIEW →
       APPROVED/DECLINED → DISBURSED). The id uses the resolved 13-digit Hojin
       Bango (``L-<hojin_bango>``, matching the turnaround intake convention)
       when available, falling back to ``L-<tdb_code>`` otherwise.

    Backward-compatible: a caller-provided loan wins (no re-seed), and a fully
    pre-populated state produces no data update.

    Args:
        state: Current graph state (requires ``tdb_code``).
        config: LangGraph run config (injected; unused).
        provider: Data provider bound at build time (mock by default).

    Returns:
        Partial state update with the resolved applicant data and/or the seeded
        APPLIED facility, or an empty update when nothing is needed.
    """
    provider = provider or MockDataProvider()

    # Resolve the applicant unless the run is already fully caller-provided
    # (a loan attached AND a profile present -> nothing to load).
    data_update: dict[str, Any] = {}
    if state.company_profile is None or not (state.loan_id or state.loan_events):
        data_update = _resolve_applicant(state, provider)

    # Seed the APPLIED facility only when no loan is attached (caller wins).
    if state.loan_id or state.loan_events:
        return data_update

    resolved_profile = data_update.get("company_profile") or state.company_profile
    hojin_bango = (
        data_update.get("hojin_bango")
        or state.hojin_bango
        or (resolved_profile.hojin_bango if resolved_profile else "")
    )
    loan_id = f"L-{hojin_bango}" if hojin_bango else f"L-{state.tdb_code}"
    applied = LoanEvent(
        status=LoanStatus.APPLIED,
        at=_now_utc(),
        actor="system",
        note="Origination intake: new facility application (申込).",
    )
    _log.info("origination_intake.applied", loan_id=loan_id, tdb_code=state.tdb_code)
    applied_events = [applied.model_dump(mode="json")]
    # Persist the APPLIED seed to the append-only loan ledger so the facility's
    # lifecycle is durable from inception (申込). The persist seam reads
    # ``loan_id`` from state, which the seed has only just assigned, so pass a
    # lightweight copy carrying the new id. Best-effort / offline no-op.
    from app.backend.portfolio.loan_store_postgres import persist_loan_events

    persist_loan_events(
        state.model_copy(update={"loan_id": loan_id}),
        applied_events,
        log_event="origination_intake.loan_persisted",
    )
    return {
        **data_update,
        "loan_id": loan_id,
        "loan_events": applied_events,
    }


def route_after_credit_decision(state: SaiseiState) -> str:
    """Route on the banker's 稟議 credit decision.

    - ``approve`` → disbursement (records APPROVED → DISBURSED).
    - anything else (``decline`` / unset) → END.

    Keyed off ``origination_decision`` (set by origination_hitl_node), never a
    string literal scattered elsewhere.
    """
    if state.origination_decision == "approve":
        return "disbursement"
    return END


def build_origination_graph(
    provider: MockDataProvider | None = None,
) -> StateGraph[SaiseiState]:
    """Build (but do not compile) the loan-origination StateGraph.

    Args:
        provider: Data provider bound into the intake node so the 稟議
            recommendation reasons over real applicant signals; defaults to the
            deterministic mock engine. Bound via ``partial`` (mirroring
            ``app.backend.graph.build_graph``) so a live TDB / core-banking
            client swaps in without structural changes.

    Returns:
        The assembled, uncompiled origination ``StateGraph``.
    """
    provider = provider or MockDataProvider()
    graph: StateGraph[SaiseiState] = StateGraph(SaiseiState)

    graph.add_node("origination_intake", partial(origination_intake_node, provider=provider))
    graph.add_node("loan_origination", loan_origination_node)
    graph.add_node("origination_hitl", origination_hitl_node)
    graph.add_node("disbursement", disbursement_node)

    graph.add_edge(START, "origination_intake")
    graph.add_edge("origination_intake", "loan_origination")
    graph.add_edge("loan_origination", "origination_hitl")
    graph.add_conditional_edges(
        "origination_hitl",
        route_after_credit_decision,
        {"disbursement": "disbursement", END: END},
    )
    graph.add_edge("disbursement", END)

    return graph


def compile_origination_graph(
    provider: MockDataProvider | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[SaiseiState]:
    """Compile the loan-origination graph.

    Args:
        provider: Data provider bound into the intake node (mock by default).
        checkpointer: Optional checkpointer enabling the HITL interrupt/resume.
            When omitted the graph compiles without persistence (useful for
            tests that do not exercise the pause).

    Returns:
        The compiled, runnable origination graph.
    """
    graph = build_origination_graph(provider)
    return graph.compile(checkpointer=checkpointer)
