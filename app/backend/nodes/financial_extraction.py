"""Financial extraction node.

Merges the intake node (TDB identity + Shisanhyo) and the macro node
(BOJ rates + working-capital gap estimation) into a single blueprint file.

Public functions preserved for test compatibility:
- ``intake_node``: resolve corporate identity from TDB code.
- ``ews_node``: load Shisanhyo (re-exported from ews_scoring for graph wiring).
- ``macro_node``: load macro data and estimate working-capital gap.
- ``estimate_working_capital_gap``: pure function, testable in isolation.

The graph wires these as separate nodes; this file is the single source of truth
for both intake and macro logic.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.backend.state import SaiseiState
from app.backend.tools.boj_macro import RatePoint, SettlementMetrics
from app.backend.tools.hojin_bango import HojinBangoClient
from app.backend.tools.provider import MockDataProvider
from app.backend.tools.tdb_api import AntiSocialCheck, TdbCreditReport
from app.shared.logging import get_logger
from app.shared.models.loan import LoanEvent, LoanStatus

__all__ = [
    "intake_node",
    "macro_node",
    "estimate_working_capital_gap",
]

_log = get_logger(__name__)


def _now_utc() -> dt.datetime:
    """Return the current UTC timestamp (isolated for test patchability)."""
    return dt.datetime.now(dt.UTC)


def _durable_loan_events(loan_id: str) -> list[dict[str, Any]]:
    """Read a facility's persisted loan-lifecycle log from the durable store.

    The loan ledger (written by the workout / HITL side-records) is the TRUE
    cross-run history of a facility: a borrower re-assessed in a later session
    may have already been moved to 条件変更 / 管理回収. Reading it here lets intake
    resume from the real status instead of re-seeding a fresh PERFORMING chain,
    so the live run agrees with the durable ledger.

    Best-effort and offline-safe: with no ``SAISEI_LOAN_DSN`` the factory returns
    ``NullLoanStore`` whose ``read`` is ``[]``, so this returns ``[]`` and intake
    falls back to the bootstrap (byte-stable default behaviour). Any read failure
    is logged and treated as "no durable history" rather than breaking intake.

    Args:
        loan_id: The facility id intake keys on (``f"L-{hojin_bango}"``).

    Returns:
        The persisted LoanEvent dicts (oldest-first), or ``[]`` when there is no
        durable history / no store configured / on any failure.
    """
    try:
        from app.backend.portfolio.loan_store_postgres import read_loan_events

        return read_loan_events(loan_id, log_event="intake.loan_read_failed")
    except Exception as exc:  # noqa: BLE001 - durable read is best-effort, never fatal
        _log.warning("intake.loan_read_failed", error=str(exc), loan_id=loan_id)
        return []


def _bootstrap_loan_events(report: TdbCreditReport) -> list[dict[str, Any]]:
    """Seed an initial loan-lifecycle log for an existing performing facility.

    Saisei's intake represents a borrower ALREADY under monitoring, i.e. a
    facility that has been originated and is currently performing. We therefore
    seed the minimal legal event chain ending at PERFORMING
    (APPLIED -> UNDER_REVIEW -> APPROVED -> DISBURSED -> PERFORMING) so the
    loan-lifecycle wiring (recording an FSA-implied 条件変更 / 管理回収 transition
    at the banker's HITL approval) has a current status to transition from.

    The principal is the borrower's total outstanding balance, taken as the sum
    of ``lender_stakes`` (real per-lender outstanding figures). When the report
    carries no stake data there is no truthful principal to record, so this
    returns ``[]`` and no loan is attached (backward-compatible no-op).

    All events share the intake timestamp and a ``system`` actor and are clearly
    noted as a monitoring bootstrap, so no origination data is invented beyond
    the fact — known at intake — that the facility exists and is performing.

    Args:
        report: The resolved TDB credit report.

    Returns:
        The seeded LoanEvent dicts (oldest-first), or ``[]`` when no outstanding
        balance is known.
    """
    if not report.lender_stakes:
        return []
    at = _now_utc()
    chain = (
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
        LoanStatus.PERFORMING,
    )
    return [
        LoanEvent(
            status=status,
            at=at,
            actor="system",
            note="monitoring bootstrap (existing performing facility)",
        ).model_dump(mode="json")
        for status in chain
    ]


# ---------------------------------------------------------------------------
# Intake node (TDB identity + Shisanhyo identity)
# ---------------------------------------------------------------------------


def intake_node(state: SaiseiState, provider: MockDataProvider | None = None) -> dict[str, Any]:
    """Resolve identity and load the TDB credit report.

    Args:
        state: Current graph state (requires ``tdb_code``).
        provider: Data provider; defaults to the mock engine.

    Returns:
        Partial state update with profile, score, identity, and any errors.
    """
    provider = provider or MockDataProvider()
    try:
        report = provider.credit_report(state.tdb_code)
    except KeyError:
        _log.warning("intake.unknown_tdb_code", tdb_code=state.tdb_code)
        return {"errors": [*state.errors, f"Unknown TDB code: {state.tdb_code}"]}

    errors = list(state.errors)
    if report.anti_social_check is AntiSocialCheck.FLAGGED:
        errors.append("Anti-social-forces check FLAGGED — escalate; no turnaround support.")

    # Validate the 13-digit Corporate Number (法人番号) check digit, and enrich
    # from the NTA registry when configured. Non-fatal: a validation failure is
    # logged and recorded but never hard-fails the graph (the mock profile still
    # drives the run), mirroring the degrade-gracefully data contract.
    hojin_client = HojinBangoClient()
    hojin_bango = report.profile.hojin_bango
    if not hojin_client.validate(hojin_bango):
        _log.warning("intake.hojin_bango_invalid", hojin_bango=hojin_bango)
        errors.append(f"Hojin Bango failed check-digit validation: {hojin_bango}")
    else:
        info = hojin_client.lookup(hojin_bango)
        if info is not None:
            _log.info(
                "intake.hojin_bango_verified",
                hojin_bango=hojin_bango,
                registry_name=info.name,
            )

    _log.info(
        "intake.resolved",
        tdb_code=state.tdb_code,
        hojin_bango=report.profile.hojin_bango,
        tdb_score=report.tdb_score,
        anti_social=report.anti_social_check.value,
    )

    # Attach the loan-lifecycle spine for an existing performing facility, but
    # only when the caller has not already supplied a loan in the initial invoke
    # (caller-provided loan wins) and the report carries a truthful outstanding
    # balance (sum of lender_stakes). Backward-compatible no-op otherwise:
    # loan_id stays '' and loan_events stays empty, so the HITL wiring is inert.
    loan_update: dict[str, Any] = {}
    if not state.loan_id and not state.loan_events:
        loan_id = f"L-{report.profile.hojin_bango}"
        # Prefer the TRUE cross-run history from the durable loan ledger; fall
        # back to a fresh monitoring bootstrap when there is none (or offline).
        # This makes a re-assessed facility resume from its real status (e.g.
        # 管理回収) instead of resetting to PERFORMING.
        durable = _durable_loan_events(loan_id)
        events = durable or _bootstrap_loan_events(report)
        if events:
            loan_update = {
                "loan_id": loan_id,
                "loan_events": events,
            }
            _log.info(
                "intake.loan_attached",
                loan_id=loan_id,
                principal=sum(report.lender_stakes.values()),
                source="durable" if durable else "bootstrap",
            )

    # Seed baseline negotiation / commitment channels so they are always present
    # in snapshot.values, even on paths that never reach the strategist or the
    # creditor-meeting consolidation (e.g. the Normal / 正常先 monitor-only path
    # that routes straight to END, or the pre-HITL snapshot before any banker
    # resume).  LangGraph omits unwritten channels from snapshot.values; without
    # this seed, reads of negotiation_status / yakuin_hoshu_cut /
    # personal_asset_disposal raise KeyError on those paths.  Echoing the
    # commitment flags from state preserves any values supplied in the initial
    # invoke while defaulting to False otherwise.
    return {
        "hojin_bango": report.profile.hojin_bango,
        "company_profile": report.profile,
        "tdb_score": report.tdb_score,
        "errors": errors,
        "negotiation_status": state.negotiation_status,
        "yakuin_hoshu_cut": state.yakuin_hoshu_cut,
        "personal_asset_disposal": state.personal_asset_disposal,
        # Seed lender stakes from the source when present so the sub-bank critic
        # runs the accurate stake-based pro-rata check. Preserve any stakes the
        # caller supplied in the initial invoke; only override from the report
        # when the report actually carries stake data (absent -> keep state).
        "lender_stakes": report.lender_stakes or state.lender_stakes,
        **loan_update,
    }


# ---------------------------------------------------------------------------
# Macro node (BOJ rates + working-capital gap)
# ---------------------------------------------------------------------------


def estimate_working_capital_gap(
    monthly_sales: int,
    monthly_cogs: int,
    metrics: SettlementMetrics,
    rate_curve: list[RatePoint],
    monthly_operating_profit: int = 0,
) -> int:
    """Estimate the working-capital gap (Shikin Kuri / 資金繰り) in integer yen.

    Dimensionally-consistent definition (all terms are yen over the SAME
    cash-conversion-cycle horizon):

        cash_cycle_days       = receivable_days - payable_days   (DSO - DPO)
        daily_cogs            = monthly_cogs / 30
        financing_requirement = cash_cycle_days * daily_cogs      (yen, a stock:
                                the cash the firm must carry to fund the gap
                                between paying suppliers and collecting sales)
        rate_stress           = 1 + latest_policy_rate_bps / 10_000
        buffer                = monthly_operating_profit prorated to the
                                cash_cycle horizon = eigyo_rieki * cycle/30
        gap                   = buffer - financing_requirement * rate_stress

    A NEGATIVE result indicates a funding deficit: the firm's prorated
    operating-profit cushion does not cover its rate-stressed cash-cycle
    financing requirement. Both sides are yen over the same horizon, so the
    magnitude is economically meaningful (unlike the prior flow-minus-stock
    formula).

    Args:
        monthly_sales: Latest monthly sales (Uriage), JPY.
        monthly_cogs: Latest monthly COGS (Uriage Genka), JPY.
        metrics: Settlement / liquidity metrics (DSO/DPO).
        rate_curve: BOJ policy-rate curve (latest point used for stress).
        monthly_operating_profit: Latest monthly operating profit
            (Eigyo Rieki / 営業利益 = sales - COGS - SG&A), JPY. Defaults to 0
            so existing callers without SG&A stay valid (conservative: a 0
            buffer makes any positive financing requirement a deficit).

    Returns:
        Estimated working-capital gap in integer yen (negative = deficit).
    """
    cash_cycle_days = metrics.receivable_days - metrics.payable_days
    daily_cogs = monthly_cogs / 30.0
    financing_requirement = cash_cycle_days * daily_cogs

    latest_bps = rate_curve[-1].policy_rate_bps if rate_curve else 0
    rate_stress = 1.0 + (latest_bps / 10_000.0)

    # Operating-profit cushion prorated to the cash-cycle horizon (yen).
    buffer = monthly_operating_profit * (cash_cycle_days / 30.0)

    gap = buffer - (financing_requirement * rate_stress)
    return int(round(gap))


def macro_node(state: SaiseiState, provider: MockDataProvider | None = None) -> dict[str, Any]:
    """Load macro/settlement data and estimate the working-capital gap.

    Args:
        state: Current graph state (uses the latest Shisanhyo row).
        provider: Data provider; defaults to the mock engine.

    Returns:
        Partial state update with rate curve, settlement metrics, and gap.
    """
    provider = provider or MockDataProvider()
    rate_curve = provider.rate_curve()
    metrics = provider.settlement_metrics()

    if not state.shisanhyo:
        _log.warning("macro.no_shisanhyo")
        return {
            "boj_rate_curve": rate_curve,
            "settlement_metrics": metrics,
            "working_capital_gap": None,
        }

    latest = state.shisanhyo[-1]
    gap = estimate_working_capital_gap(
        monthly_sales=int(latest.uriage),
        monthly_cogs=int(latest.uriage_genka),
        metrics=metrics,
        rate_curve=rate_curve,
        monthly_operating_profit=latest.eigyo_rieki,
    )
    _log.info(
        "macro.gap_estimated",
        working_capital_gap=gap,
        latest_rate_bps=rate_curve[-1].policy_rate_bps if rate_curve else 0,
    )
    return {
        "boj_rate_curve": rate_curve,
        "settlement_metrics": metrics,
        "working_capital_gap": gap,
    }
