"""Reflex application state for Saisei.

Drives the UI from the compiled LangGraph as a *streamed creditor meeting*: it
streams the graph node-by-node (``stream_mode="updates"``) so each agent's result
appears in the transcript the moment its node finishes, runs to the HITL
interrupt, surfaces proposed strategies + the lead arranger's burden table, and
resumes the graph with the banker's decision via ``Command(resume=...)``. State
persists through the interrupt using the Postgres checkpointer, keyed by a
per-session ``thread_id``.

The UI is display-only: it never computes a verdict or a number. Every value
shown is read from streamed node updates or the final snapshot.

This module is the canonical location under ``app.frontend.state``.
The legacy path ``saisei_ui.state`` re-exports from here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import uuid
from queue import Queue as _Queue
from types import SimpleNamespace
from typing import Any

import reflex as rx
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.backend.graph import compile_graph, make_checkpointer
from app.shared.logging import get_logger
from app.shared.models.classification import FsaClass
from app.shared.models.loan import LoanEvent, current_status
from app.shared.models.money import format_jpy

_log = get_logger(__name__)


class MeetingEvent(BaseModel):
    """A single typed transcript event for the creditor-meeting panel.

    Declared as a pydantic v2 ``BaseModel`` (consistent with every other model
    in the codebase, which is pydantic v2). Reflex introspects the field types
    of a ``BaseModel`` to build a typed var, so ``rx.foreach`` and per-field
    attribute access (``event.title``) in the UI work without raising
    ``UntypedVarError``. A mutable default (``blockers``) uses
    ``Field(default_factory=list)`` so every instance gets its own list.
    """

    kind: str = ""  # system | critic | chair | banker
    speaker: str = "system"  # persona key (matches theme.PERSONAS)
    status: str = ""  # PASS | FAIL | APPROVED | ... (may be empty)
    priority: str = ""  # P0 | P1 | P2 (may be empty)
    title: str = ""
    body: str = ""
    blockers: list[str] = Field(default_factory=list)


class FeasibilityClaim(BaseModel):
    """One advisory feasibility claim with its provenance, for the UI.

    Declared as a pydantic v2 ``BaseModel`` (like ``MeetingEvent``) so Reflex
    introspects the field types and ``rx.foreach`` over a list of these claims
    works without raising ``ForeachVarError`` / ``UntypedVarError``.
    """

    text: str = ""
    status: str = ""  # grounded | unverified
    citations: str = ""  # comma-joined source labels (empty when none)


class FeasibilityRow(BaseModel):
    """One strategy's advisory feasibility note + per-claim provenance.

    A typed model (not a bare ``dict``) so the nested ``provenance`` list keeps
    a concrete element type through Reflex's var system; otherwise the nested
    list resolves to ``Any`` and ``rx.foreach(row.provenance, ...)`` raises
    ``ForeachVarError``.
    """

    title: str = ""
    band: str = ""  # high | medium | low
    score: str = ""
    advisory: str = ""
    provenance: list[FeasibilityClaim] = Field(default_factory=list)


#: Human-readable narration for each graph node as it completes. Keys match the
#: node names registered in ``app/backend/graph.py``. Used to build system
#: transcript bubbles so the banker can follow the assessment in plain language.
_NODE_NARRATION: dict[str, str] = {
    "intake": "企業プロファイルを取得しました。 (Company profile loaded.)",
    "ews": "試算表を分析し、EWSスコアを算出しました。 (Trial balances scored.)",
    "macro": "マクロ指標・資金繰りギャップを算出しました。 (Macro & liquidity gap computed.)",
    "classifier": "債務者区分を判定しました。 (FSA classification decided.)",
    "keieisha_hosho": "経営者保証解除の評価を完了しました。 (Guarantee-release assessed.)",
    "strategist": (
        "再生戦略を立案しました。債権者会議を開始します。 "
        "(Strategies drafted; convening creditors.)"
    ),
    "plan_writer": "経営改善計画書を作成しました。 (Keikakusho drafted.)",
    "workout": (
        "【法的整理・清算引継ぎ】本件はワークアウト担当部署へ引き継がれました。 "
        "(Legal/liquidation handoff recorded. Case transferred to workout team.)"
    ),
}

#: Node names that emit a critic CriticFeedback we render as a persona bubble.
_CRITIC_NODES: frozenset[str] = frozenset(
    {"main_bank_critic", "sub_bank_critic", "guarantor_critic"}
)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a model attribute or a mapping key.

    Snapshot/stream values returned by the LangGraph Postgres checkpointer may be
    rehydrated as plain dicts (or enums as plain strings) rather than the
    original Pydantic models. This helper reads uniformly from either shape so
    the UI never assumes a live object.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fsa_kanji(classification: Any) -> str:
    """Return the FSA classification kanji, accepting an enum, str, or None."""
    if not classification:
        return ""
    if isinstance(classification, FsaClass):
        return classification.kanji
    try:
        return FsaClass(str(classification)).kanji
    except ValueError:
        # Unknown value (e.g. already a kanji label) — display as-is.
        return str(classification)


def _period_str(period: Any) -> str:
    """Return an ISO date string for a date or an already-serialized string."""
    if isinstance(period, dt.date):
        return period.isoformat()
    return str(period or "")


#: Leading machine "code:" prefix on a critic fatal-blocker string (e.g.
#: ``yakuin_hoshu_not_cut:`` / ``pro_rata_deviation:``). The code is retained in
#: the backend state for routing/diagnostics, but it is an internal identifier
#: and must never be shown to the banker, so it is stripped for display.
_BLOCKER_CODE_PREFIX = re.compile(r"^[a-z][a-z0-9_]*:\s*")


def _strip_blocker_code(blocker: str) -> str:
    """Remove the internal ``code:`` prefix from a blocker for banker display."""
    return _BLOCKER_CODE_PREFIX.sub("", blocker).strip()


#: A consolidated blocker line in the lead-arranger directive carries a
#: ``[priority/persona]`` tag and the internal ``code:`` identifier, e.g.
#: ``1. [P1/main_bank] yakuin_hoshu_not_cut: 役員報酬…``. Both are internal and
#: must not reach the banker; this matches the ``[..]`` tag plus the code so we
#: can drop them while keeping the numbering and the human message.
_DIRECTIVE_TAG = re.compile(r"\[[^\]]*\]\s*(?:[a-z][a-z0-9_]*:\s*)?")


def _clean_directive_for_display(directive: str) -> str:
    """Strip internal ``[priority/persona] code:`` tags from a directive.

    Keeps list numbering, headings, and the burden-sharing table intact; only
    removes the engineering tags/codes so the chair's bubble reads as a
    professional, banker-facing summary.
    """
    return "\n".join(_DIRECTIVE_TAG.sub("", line) for line in directive.splitlines())


def _record_companion_query(
    *,
    thread_id: str,
    values: dict[str, Any],
    question: str,
    intent: str,
    grounded: bool,
    citations: list[str],
) -> None:
    """Record a COMPANION_QUERY audit event for one companion turn.

    A case-shaping conversation with the advisory companion must leave a trail
    like any other event a regulator cares about. This rides the SAME
    append-only, hash-chained, data-version-pinned audit ledger as every other
    event, via the shared :func:`record_event` helper, keyed by the run's
    ``thread_id``.

    It is a strict side-record: best-effort and never fatal (the chat must never
    break because the ledger misbehaved), write-only (it returns ``None`` and
    mutates nothing), and an offline no-op (``NullAuditSink``) until
    ``SAISEI_AUDIT_DSN`` is configured. The companion remains read-only and
    advisory; recording the question changes no gate, route, figure, or verdict.

    The snapshot ``values`` dict is wrapped in a ``SimpleNamespace`` so the
    ledger's ``getattr``-based identity + ``data_version`` derivation reads it
    exactly as it reads a live ``SaiseiState`` — pinning the question to the
    figures in force when it was asked. Only the question text + answer metadata
    are stored in the payload; the answer prose itself is not (it is
    reproducible from the pinned data version, and the ledger stays lean).
    """
    if not thread_id:
        # No run thread to attach the event to (e.g. a question before any
        # assessment). Nothing to pin or chain against, so skip the record.
        return
    try:
        from types import SimpleNamespace

        from app.backend.audit.audit_log import AuditEventType
        from app.backend.audit.record import record_event

        state_like = SimpleNamespace(
            shisanhyo=values.get("shisanhyo") or [],
            tdb_code=values.get("tdb_code", ""),
            hojin_bango=values.get("hojin_bango", ""),
            tdb_score=values.get("tdb_score"),
            working_capital_gap=values.get("working_capital_gap"),
            net_worth=values.get("net_worth"),
            is_insolvent=values.get("is_insolvent"),
        )
        record_event(
            AuditEventType.COMPANION_QUERY,
            state=state_like,
            payload={
                "question": question,
                "intent": intent,
                "grounded": grounded,
                "citations": list(citations),
            },
            actor="banker",
            thread_id=thread_id,
        )
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never fatal
        _log.warning("companion.audit_failed", error=str(exc))


def _loan_ledger_rows(loan_id: str) -> list[dict[str, str]]:
    """Read a facility's durable loan-event ledger as display rows.

    Pure module-level helper (no Reflex ``self``) so the read + row-mapping is
    unit-testable without the async state lock. Reads the loan store IN-PROCESS
    via the configured factory; offline-safe (NullLoanStore -> []), a no-op when
    ``loan_id`` is empty. Maps each :class:`~app.shared.models.loan.LoanEvent`
    to a display dict (status kanji/english, actor, note, timestamp). The caller
    wraps this in ``asyncio.to_thread`` and a best-effort guard.

    Args:
        loan_id: The facility id to read (``L-<hojin_bango>``); "" -> [].

    Returns:
        Display-row dicts (oldest-first), or [] when no facility / no store.
    """
    if not loan_id:
        return []
    from app.backend.portfolio.loan_store_postgres import get_loan_store
    from app.shared.settings import get_settings

    settings = get_settings()
    store = get_loan_store(settings.loan_dsn)
    events = store.read(settings.loan_tenant_default, loan_id)
    return [
        {
            "at": str(ev.at),
            "status_kanji": ev.status.kanji,
            "status_english": ev.status.english,
            "actor": str(ev.actor),
            "note": str(ev.note or ""),
        }
        for ev in events
    ]


def _origination_code_valid(tdb_code: str) -> bool:
    """Return whether a TDB code is a well-formed 7-digit string."""
    return tdb_code.isdigit() and len(tdb_code) == 7


def _origination_recommendation_view(values: dict[str, Any]) -> dict[str, str]:
    """Map an origination snapshot's recommendation to display strings.

    Reads the already-computed ``origination_recommendation`` dict the
    deterministic ``loan_origination_node`` wrote (recommendation, grounded
    reason, provisional ceiling). Pure display formatting; computes no figure.
    Returns empty strings when no recommendation is present (defensive).
    """
    rec = values.get("origination_recommendation") or {}
    amount = rec.get("max_facility_amount") or 0
    # Advisory debt-service-capacity block (added by loan_origination_node, !1).
    # Defensive: a DECLINE carries a 0 ceiling and may omit the block entirely,
    # so every field defaults to empty / "—" and the card simply shows no chip.
    cap = rec.get("debt_capacity") or {}
    service = cap.get("annual_debt_service") or 0
    ceiling = cap.get("prudent_service_ceiling") or 0
    # Advisory collateral / guarantee coverage block (breadth #6). Same
    # defensive contract: a missing block yields empty band + "—" figures so the
    # card shows no coverage chip.
    cov = rec.get("coverage") or {}
    covered = cov.get("covered_amount") or 0
    uncovered = cov.get("uncovered_amount") or 0
    return {
        "recommendation": str(rec.get("recommendation", "")),
        "reason": str(rec.get("reason", "")),
        "grounded": "yes" if rec.get("grounded") else "no",
        "max_facility": format_jpy(int(amount)) if int(amount) > 0 else "—",
        "capacity_band": str(cap.get("band", "")),
        "capacity_reason": str(cap.get("reason", "")),
        "capacity_debt_service": (format_jpy(int(service)) if int(service) > 0 else "—"),
        "capacity_ceiling": (format_jpy(int(ceiling)) if int(ceiling) > 0 else "—"),
        "coverage_band": str(cov.get("band", "")),
        "coverage_reason": str(cov.get("reason", "")),
        "coverage_covered": (format_jpy(int(covered)) if int(covered) > 0 else "—"),
        "coverage_uncovered": (format_jpy(int(uncovered)) if int(uncovered) > 0 else "—"),
    }


def _origination_loan_status_kanji(values: dict[str, Any]) -> str:
    """Derive the current loan-lifecycle status kanji from a snapshot.

    Replays the snapshot's append-only ``loan_events`` log and returns the
    current status' kanji label, or ``""`` when there is no facility / a
    malformed log (never raises). Pure display derivation.
    """
    raw = values.get("loan_events") or []
    if not raw:
        return ""
    try:
        events = [LoanEvent.model_validate(e) for e in raw]
        return current_status(events).kanji
    except Exception:  # noqa: BLE001 - display derivation must never raise
        return ""


def _run_origination_to_pause(
    tdb_code: str,
    thread_id: str,
    *,
    collateral_value: int = 0,
    guarantee_coverage: int = 0,
) -> dict[str, Any]:
    """Drive the origination graph to the 稟議 pause (blocking) and read its state.

    Compiles the origination graph on the shared checkpointer (the SAME seam the
    ``/api/v1/origination`` HTTP surface uses), invokes it with the applicant's
    TDB code and the optional underwriting coverage figures (担保 / 保証), and
    returns the resulting snapshot values. Blocking: the caller wraps it in
    ``asyncio.to_thread``. The checkpointer persists the pause so a later resume
    continues from here.

    Args:
        tdb_code: The applicant's 7-digit TDB code.
        thread_id: The run thread id (idempotency / resume key).
        collateral_value: Pledged collateral value (担保評価額) in integer yen;
            feeds only the advisory coverage check. 0 (no collateral) by default.
        guarantee_coverage: Guaranteed portion (保証カバー額) in integer yen;
            feeds only the advisory coverage check. 0 (no guarantee) by default.

    Returns:
        The JSON-ish snapshot ``values`` dict after the run reaches the pause /
        a terminal state.
    """
    from app.backend.graph_origination import compile_origination_graph

    config = {"configurable": {"thread_id": thread_id}}
    payload: dict[str, Any] = {"tdb_code": tdb_code}
    if collateral_value > 0:
        payload["collateral_value"] = int(collateral_value)
    if guarantee_coverage > 0:
        payload["guarantee_coverage"] = int(guarantee_coverage)
    with make_checkpointer() as cp:
        graph_app = compile_origination_graph(checkpointer=cp)
        graph_app.invoke(payload, config=config)
        state = graph_app.get_state(config)
    return dict(state.values)


def _resume_origination(thread_id: str, decision: str) -> dict[str, Any]:
    """Resume a paused origination run with the banker's credit decision.

    Resumes the persisted run via ``Command(resume=...)`` carrying the banker's
    ``approve`` / ``decline`` and returns the post-resume snapshot values.
    Blocking; the caller wraps it in ``asyncio.to_thread``.

    Args:
        thread_id: The paused run's thread id.
        decision: ``"approve"`` or ``"decline"``.

    Returns:
        The snapshot ``values`` dict after the resume completes.
    """
    from app.backend.graph_origination import compile_origination_graph

    config = {"configurable": {"thread_id": thread_id}}
    with make_checkpointer() as cp:
        graph_app = compile_origination_graph(checkpointer=cp)
        graph_app.invoke(Command(resume={"decision": decision}), config=config)
        state = graph_app.get_state(config)
    return dict(state.values)


def _run_servicing(
    loan_id: str,
    action: str,
    thread_id: str,
    *,
    amount: int = 0,
    lender_stakes: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Drive the servicing graph to completion (blocking) and read its state.

    Compiles the servicing graph on the shared checkpointer (the SAME seam the
    ``/api/v1/servicing`` HTTP surface uses) and invokes it with the facility's
    ``loan_id`` and the requested ``servicing_action`` ('confirm' 実行→正常 /
    'repay_amount' 一部入金 / 'repay' 完済). The servicing graph never pauses, so
    this returns the terminal snapshot values directly. The graph's
    ``servicing_intake`` loads the facility's durable loan log by ``loan_id`` so
    the deterministic node reasons over its TRUE current status; a repayment
    additionally needs a principal baseline (``lender_stakes``).

    Blocking: the caller wraps it in ``asyncio.to_thread``.

    Args:
        loan_id: The facility id to service (``L-<hojin_bango>``).
        action: ``"confirm"`` / ``"repay_amount"`` / ``"repay"``.
        thread_id: The run thread id (idempotency key).
        amount: The 一部入金 amount for ``repay_amount`` (integer yen).
        lender_stakes: Optional principal-baseline a repayment draws down.

    Returns:
        The snapshot ``values`` dict after the run completes.
    """
    from app.backend.graph_servicing import compile_servicing_graph

    config = {"configurable": {"thread_id": thread_id}}
    payload: dict[str, Any] = {
        "loan_id": loan_id,
        "servicing_action": action,
        "servicing_amount": int(amount),
    }
    if lender_stakes:
        payload["lender_stakes"] = {k: int(v) for k, v in lender_stakes.items()}
    with make_checkpointer() as cp:
        graph_app = compile_servicing_graph(checkpointer=cp)
        graph_app.invoke(payload, config=config)
        state = graph_app.get_state(config)
    return dict(state.values)


class SaiseiUIState(rx.State):
    """UI state backing the Saisei meeting-room dashboard."""

    # --- Inputs ---
    tdb_code: str = "1234567"
    thread_id: str = ""
    revision_note_buffer: str = ""

    # --- Origination (融資組成) entry: drive the origination graph from the UI ---
    #: The applicant's TDB code for a NEW facility application (separate input so
    #: it never collides with the assessment ``tdb_code``).
    origination_code: str = ""
    #: Per-run origination thread id (idempotency / resume key).
    origination_thread_id: str = ""
    #: idle | reviewing (paused at 稟議) | approved | declined | error.
    origination_phase: str = "idle"
    #: Whether an origination start / decision is in flight (drives spinners).
    origination_running: bool = False
    #: Optional underwriting coverage inputs (担保 / 保証) the banker supplies at
    #: 申込, fed to the advisory collateral-coverage check. Raw string buffers
    #: (yen); blank / 0 means "no coverage supplied" -> the check bands uncovered.
    origination_collateral_input: str = ""
    origination_guarantee_input: str = ""
    #: Display strings for the advisory recommendation (set at the pause).
    origination_recommendation: str = ""
    origination_reason: str = ""
    origination_grounded: str = ""
    origination_max_facility: str = "—"
    #: Advisory debt-service-capacity band for the proposed ceiling (!1): one of
    #: within_capacity | stretch | over_capacity, "" when no block is present.
    #: Checks the size-anchored ceiling against demonstrated 経常利益; display-only.
    origination_capacity_band: str = ""
    origination_capacity_reason: str = ""
    origination_capacity_debt_service: str = "—"
    origination_capacity_ceiling: str = "—"
    #: Advisory collateral / guarantee coverage band for the proposed facility
    #: (breadth #6): one of well_covered | partial | uncovered, "" when no block
    #: is present. Checks the secured + guaranteed value (担保・保証) against the
    #: facility; display-only, beside the capacity band.
    origination_coverage_band: str = ""
    origination_coverage_reason: str = ""
    origination_coverage_covered: str = "—"
    origination_coverage_uncovered: str = "—"
    #: The facility's current loan-lifecycle status kanji (申込..実行/謝絶).
    origination_loan_status: str = ""
    #: The applicant company name resolved at intake (display).
    origination_company: str = ""
    #: Last origination error message (shown when origination_phase == 'error').
    origination_error: str = ""
    #: Whether the origination entry dialog is open.
    show_origination: bool = False

    # --- Servicing (貸出管理) entry: drive the servicing graph from the UI ---
    #: The facility id (loan_id) to service (実行→正常 / 正常→完済). Separate
    #: input so it never collides with the assessment / origination inputs.
    servicing_loan_id: str = ""
    #: Per-run servicing thread id (idempotency key).
    servicing_thread_id: str = ""
    #: idle | done (the run always completes inline -- servicing never pauses) |
    #: error.
    servicing_phase: str = "idle"
    #: Whether a servicing action is in flight (drives the spinner).
    servicing_running: bool = False
    #: The action just applied ('confirm' / 'repay' / 'repay_amount'), outcome copy.
    servicing_action_taken: str = ""
    #: Banker-entered partial-repayment amount (一部入金) as a raw string buffer.
    servicing_amount_input: str = ""
    #: The facility's current loan-lifecycle status kanji after the action
    #: (正常 / 完済 ...), derived from the snapshot's loan_events (display-only).
    servicing_loan_status: str = ""
    #: Last servicing error message (shown when servicing_phase == 'error').
    servicing_error: str = ""
    #: Whether the servicing entry dialog is open.
    show_servicing: bool = False

    # --- Demo access gate (optional; active only when SAISEI_DEMO_PASSWORD set) ---
    #: Whether the visitor has entered the correct demo password this session.
    #: The configured password is read server-side in the event handler and is
    #: NEVER sent to the client.
    gate_unlocked: bool = False
    gate_input: str = ""
    gate_error: str = ""

    # --- Lifecycle phase (drives progress UI; replaces the blank wait) ---
    #: idle | assessing | meeting | awaiting_decision | drafting | done | error
    phase: str = "idle"
    active_node: str = ""

    # --- Case-file display fields ---
    company_name: str = ""
    fsa_kanji: str = ""
    ews_score: float = 0.0
    working_capital_gap_display: str = "—"

    # --- Loan-lifecycle display fields (display-only) ---
    #: Current loan-facility status derived from the append-only loan_events log
    #: (kanji + english), e.g. 正常 (Performing) / 管理回収 (Workout). Empty when no
    #: loan is attached to the run. This makes the persisted lifecycle status --
    #: including the WORKOUT transition the workout path records -- visible in the
    #: case file on every route, not just the HITL interrupt payload.
    loan_status_kanji: str = ""
    loan_status_english: str = ""
    #: Deterministic loan-loss provision (貸倒引当金) formatted for display, or
    #: "—" when no outstanding balance / loan is known. Mirrors the figure the
    #: HITL loan summary and the workout handoff surface.
    loan_provision_display: str = "—"
    #: The current run's attached facility id (``L-<hojin_bango>``), or "" when
    #: no loan is attached. Used by the Audit tab to read this facility's
    #: durable loan-event ledger. Display-only.
    loan_id_display: str = ""
    #: The current run's per-lender outstanding balances (the facility's
    #: principal baseline). Carried so a servicing repayment of the on-screen
    #: facility can draw down the real balance. Display/transport only.
    run_lender_stakes: dict[str, int] = {}

    # --- Loan-lifecycle durable ledger (Audit tab; display-only) ---
    #: Display rows for this facility's durable loan-event ledger. Each row:
    #: {at, status_kanji, status_english, actor, note}. Read in-process from the
    #: loan store (offline-safe NullLoanStore -> empty). Never written here.
    loan_ledger_rows: list[dict[str, str]] = []
    #: True while the loan ledger is being loaded (shows a spinner).
    loan_ledger_loading: bool = False

    # --- Feature 7 explainability (display-only) ---
    #: Per-signal EWS contributions for the score breakdown panel. Each row:
    #: {key, label, raw_pct, points, weight, share_pct}. Empty until scored.
    ews_breakdown_rows: list[dict[str, str]] = []
    #: Deterministic reason the FSA classification landed in its band.
    classification_reason: str = ""

    # PART 2: Hosho Kaijo display fields.
    hosho_kaijo_score: float = 0.0
    succession_ready: bool = False

    # --- Feature 7 explainability: exportable per-classification report ---
    #: Cached Markdown of the deterministic explainability report, built at
    #: finalize time from the SAME backend snapshot the breakdown panels read.
    #: Held here (like ``recovery_serialised`` for the XLSX export) so the
    #: download button can emit it without re-reading the checkpointer. Empty
    #: until a borrower has been classified; drives the 説明レポート button's cond.
    explainability_report_md: str = ""

    # --- Feature 7 explainability: guarantee-release (Hosho Kaijo) basis ---
    #: Per-pillar breakdown of the 保証解除 score. Each row:
    #: {key, label, met ("yes"/"no"), score, weight, fill_pct, directive}.
    #: Empty until assessed. Display-only.
    hosho_pillar_rows: list[dict[str, str]] = []
    #: Ordered, actionable "what must change to release the guarantee" directives.
    hosho_directives: list[str] = []
    #: Whether the borrower is eligible for guarantee release (score >= floor).
    hosho_eligible: bool = False

    # PART 3: Creditor-meeting display fields.
    negotiation_status: str = "pending"
    revision_directive: str = ""
    revision_count: int = 0

    shisanhyo_rows: list[dict[str, str]] = []
    strategies: list[dict[str, str]] = []
    burden_rows: list[dict[str, str]] = []

    # --- Feature 0 phase 4: advisory feasibility notes + claim provenance ---
    #: Per-strategy feasibility display rows. Each row carries the deterministic
    #: achievability band/score plus the advisory text and a per-claim provenance
    #: list ([{text, status, citations}]) so the UI can show the banker which
    #: advisory claims are attributable and to what. Display-only; empty offline.
    feasibility_rows: list[FeasibilityRow] = []

    # --- Feature 5: P&L recovery projection (display + Excel export cache) ---
    #: JSON-safe serialisation of the deterministic RecoveryProjection, rebuilt
    #: at finalize time. Drives the Excel出力 button (shown only when populated)
    #: and lets download_recovery_xlsx rebuild the projection without re-reading
    #: the checkpointer. Empty when there is no approved strategy / history.
    recovery_serialised: dict[str, Any] = {}

    # --- Threshold-calibration display fields (MR5; display-only, advisory) ---
    #: Reconciliation-threshold calibration surfaced from the backend analysis
    #: of the captured reconciliation_outcomes corpus. Display-only: the UI never
    #: computes a verdict or a number, and never edits RECONCILIATION_BAND_DISTANCE.
    calibration_recommendation: str = ""
    calibration_rationale: str = ""
    calibration_rows: list[dict[str, str]] = []

    # --- Meeting transcript (chat-style, streamed) ---
    #: Typed events so rx.foreach can introspect the element type.
    meeting_events: list[MeetingEvent] = []
    #: Persona key of the speaker currently "considering" (shown as a transient
    #: typing indicator before their bubble lands). Empty when nobody is pending.
    #: Display-only choreography; never affects a verdict, figure, or route.
    pending_speaker: str = ""

    # --- HITL commitment flags (banker-only gates) ---
    yakuin_hoshu_cut: bool = False
    personal_asset_disposal: bool = False

    # --- Part 6: Excel/CSV upload staging ---
    #: Proposed rows from the last upload (display dicts, not TrialBalance objects).
    upload_preview_rows: list[dict[str, str]] = []
    #: Parser warnings from the last upload.
    upload_warnings: list[str] = []
    #: True while the parser is running (shows a spinner in the dropzone).
    upload_processing: bool = False
    #: Internal: serialised parsed rows (period ISO string + int yen) held
    #: between parse and confirm. Not displayed; confirm rebuilds TrialBalance
    #: objects from these and injects them so the banker's uploaded figures
    #: (not the fixture) drive the assessment.
    upload_serialised: list[dict[str, Any]] = []
    #: True when the staged rows came from GUIDED MANUAL ENTRY (Feature 8
    #: channel 4) rather than a parsed file. Guided mode makes the period cell
    #: editable too (a file already supplies periods); it changes nothing about
    #: how rows are validated, confirmed, or fed to the deterministic pipeline.
    upload_is_guided: bool = False

    # --- Outcome ---
    awaiting_decision: bool = False
    keikakusho_draft: str = ""
    error: str = ""

    # --- Feature 9: borrower workspace tabs (meta-interface, display-only) ---
    #: The active borrower tab. One of: assessment | meeting | plan | audit.
    #: Set explicitly by the banker; ``effective_tab`` falls back to the
    #: phase-implied tab when the banker has not chosen one this run. Pure view
    #: concern — never affects a verdict, figure, or route.
    active_tab: str = "assessment"
    #: True once the banker has clicked a tab this run, so the lifecycle
    #: auto-focus (``effective_tab``) stops overriding their explicit choice.
    tab_pinned: bool = False

    # --- Feature 9: Audit tab (Feature 7 immutable ledger, display-only) ---
    #: Display rows for the audit trail of the current thread. Each row:
    #: {created_at, event_type, actor, summary}. Read from the audit sink
    #: in-process (offline-safe NullAuditSink -> empty). Never written here.
    audit_rows: list[dict[str, str]] = []
    #: Hash-chain verdict for the current thread's ledger: "ok" | "broken" | "".
    audit_chain_status: str = ""
    #: Human-readable chain reason (the offending event when broken).
    audit_chain_reason: str = ""
    #: True while the audit trail is being loaded (shows a spinner).
    audit_loading: bool = False

    # --- Feature 5: recovery time-scrubber (display-only) ---
    #: Selected month on the recovery timeline. 0 = baseline (today / actuals);
    #: 1..N index into the projected months. Drives the chart playhead and the
    #: "at month N" readout. The banker drags the slider or presses play.
    selected_month: int = 0
    #: Whether the play animation is auto-advancing the scrubber.
    scrubber_playing: bool = False

    # --- Saisei companion (advisory co-pilot; summonable floating chat) ---
    #: Whether the floating companion chat window is open (summoned). The robot
    #: dock button is always present; this toggles the chat panel. Pure UI.
    companion_open: bool = False
    #: The banker's in-progress question buffer.
    companion_input: str = ""
    #: True while the companion is composing an answer (shows a typing dot).
    companion_thinking: bool = False
    #: The chat transcript. Each turn: {role, text, status} where role is
    #: "banker" | "companion" and status is "" | "grounded" | "unverified"
    #: (companion turns only). Ephemeral — NOT persisted (transcript-at-rest is
    #: a deliberate later, bank-owned decision, like the opt-in audit DSN).
    companion_turns: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Derived helpers (used by the UI)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Saisei companion (advisory co-pilot) — read-only, grounded, ephemeral
    # ------------------------------------------------------------------

    @rx.var
    def companion_has_turns(self) -> bool:
        """Whether the companion transcript has any turns yet."""
        return len(self.companion_turns) > 0

    @rx.event
    def toggle_companion(self) -> None:
        """Summon or dismiss the floating companion chat window.

        Pure display toggle — the companion is advisory and read-only, so opening
        or closing it never touches a gate, route, figure, or verdict.
        """
        self.companion_open = not self.companion_open

    @rx.event
    def set_companion_input(self, value: str) -> None:
        """Update the companion question buffer."""
        self.companion_input = value

    @rx.event
    def companion_key_down(self, key: str):
        """Send the question on Enter (convenience keybinding for the composer).

        Returns the ``ask_companion`` event when the banker presses Enter so a
        question can be sent without reaching for the mouse (the send button
        remains for pointer-only use). Any other key is ignored. ``ask_companion``
        itself guards against an empty/in-flight question, so a bare Enter is a
        safe no-op.
        """
        if key == "Enter":
            return SaiseiUIState.ask_companion
        return None

    @rx.event
    def clear_companion(self) -> None:
        """Clear the companion transcript (the banker re-summons a fresh entity)."""
        self.companion_turns = []

    @rx.event(background=True)
    async def ask_companion(self):
        """Answer the banker's question about the current case (advisory only).

        Reads the finalized graph snapshot for this thread and delegates to the
        deterministic, grounded, READ-ONLY companion agent
        (:func:`app.backend.agents.saisei_chat.answer_question`). The agent
        returns text only — never a state delta — so this can never move a gate,
        route, figure, or verdict. Runs in the background so the chat stays
        responsive; the blocking snapshot read + answer compose happen off the
        event loop.
        """
        async with self:
            question = self.companion_input.strip()
            if not question or self.companion_thinking:
                return
            self.companion_input = ""
            self.companion_thinking = True
            self.companion_turns = [
                *self.companion_turns,
                {"role": "banker", "text": question, "status": ""},
            ]
            thread_id = self.thread_id
        yield

        def _answer() -> dict[str, str]:
            from app.backend.agents.saisei_chat import answer_question

            # Read the finalized snapshot for this thread (empty when no run yet,
            # which the agent handles by abstaining). Snapshot read is blocking,
            # so it runs in this worker thread.
            values: dict[str, Any] = {}
            if thread_id:
                try:
                    with make_checkpointer() as cp:
                        graph_app = compile_graph(checkpointer=cp)
                        snapshot = graph_app.get_state({"configurable": {"thread_id": thread_id}})
                    values = dict(snapshot.values)
                except Exception as exc:  # noqa: BLE001 - advisory, best-effort
                    _log.warning("companion.snapshot_failed", error=str(exc))
                    values = {}
            ans = answer_question(question, values)
            status = "grounded" if ans.grounded else "unverified"
            # AUDIT (Feature 7): record the question + the answer's grounding
            # status as a COMPANION_QUERY event, pinned to the snapshot's
            # data_version. A case-shaping conversation must leave a trail. This
            # is a strict side-record: best-effort, never fatal, write-only, and
            # an offline no-op (NullAuditSink) until SAISEI_AUDIT_DSN is set. It
            # rides the SAME append-only, hash-chained ledger as every other
            # event, keyed by this run's thread_id. The companion is still
            # read-only/advisory; nothing here moves a gate, route, or figure.
            _record_companion_query(
                thread_id=thread_id,
                values=values,
                question=question,
                intent=str(ans.intent),
                grounded=ans.grounded,
                citations=ans.citations,
            )
            return {"role": "companion", "text": ans.text, "status": status}

        try:
            turn = await asyncio.to_thread(_answer)
        except Exception as exc:  # noqa: BLE001 - never break the chat
            _log.warning("companion.answer_failed", error=str(exc))
            turn = {
                "role": "companion",
                "text": (
                    "申し訳ありません、回答を生成できませんでした。 "
                    "(Sorry — could not compose an answer.)"
                ),
                "status": "",
            }

        async with self:
            self.companion_turns = [*self.companion_turns, turn]
            self.companion_thinking = False
        yield

    @rx.var
    def is_running(self) -> bool:
        """Whether the graph is actively executing (show progress UI)."""
        return self.phase in ("assessing", "meeting", "drafting")

    # ------------------------------------------------------------------
    # Feature 5: recovery time-scrubber (pure display; computes no figure)
    # ------------------------------------------------------------------

    @rx.var
    def recovery_month_count(self) -> int:
        """Number of projected months available to scrub (0 when none)."""
        return len(self.recovery_serialised.get("months") or [])

    @rx.var
    def selected_month_view(self) -> dict[str, str]:
        """Display values at the currently selected scrubber month.

        ``selected_month == 0`` is the baseline (today's actuals): EWS is the
        projection baseline and the class is the current classification. A
        non-zero index reads the already-computed projected month. Pure
        lookup/formatting of deterministic values — the UI computes no figure.
        """
        from app.shared.constants import EWS_SUBSTANDARD
        from app.shared.models.classification import FsaClass

        months = self.recovery_serialised.get("months") or []
        threshold = float(EWS_SUBSTANDARD)

        def _class_for(ews: float) -> str:
            # Display-only proxy of the FSA band from EWS for the scrubbed view
            # (the authoritative classification still comes from the spine).
            if ews >= 85:
                return FsaClass.JISSHITSU_HATANSAKI.kanji
            if ews >= 70:
                return FsaClass.HATAN_KENENSAKI.kanji
            if ews >= threshold:
                return FsaClass.YOCHUISAKI.kanji
            return FsaClass.SEIJOSAKI.kanji

        sel = self.selected_month
        if sel <= 0 or not months:
            baseline = float(self.recovery_serialised.get("baseline_ews", self.ews_score))
            return {
                "month": "0",
                "label": "現在 (Today)",
                "ews": f"{baseline:.2f}",
                "keijo": "—",
                "uplift": "—",
                "fsa": self.fsa_kanji or _class_for(baseline),
                "recovered": "yes" if baseline < threshold else "no",
            }
        idx = min(sel, len(months))
        m = months[idx - 1]
        from app.shared.models.money import format_jpy

        ews = float(m.get("ews_score", 0.0))
        return {
            "month": str(idx),
            "label": f"{idx}ヶ月目 (Month {idx})",
            "ews": f"{ews:.2f}",
            "keijo": format_jpy(int(m.get("keijo_rieki", 0))),
            "uplift": format_jpy(int(m.get("monthly_uplift", 0))),
            "fsa": _class_for(ews),
            "recovered": "yes" if bool(m.get("recovered")) else "no",
        }

    @rx.var
    def scrubber_playhead_x(self) -> str:
        """X pixel of the chart playhead for the selected month, as a coord string.

        Returns "" when the playhead should be hidden (baseline / no match);
        use :pyattr:`scrubber_playhead_visible` to gate rendering.
        """
        sel = self.selected_month
        if sel <= 0:
            return ""
        for p in self.recovery_points:
            if p["index"] == str(sel):
                return str(p["cx"])
        return ""

    @rx.var
    def scrubber_playhead_visible(self) -> bool:
        """Whether the scrubber playhead should be drawn (a month is selected)."""
        if self.selected_month <= 0:
            return False
        return any(p["index"] == str(self.selected_month) for p in self.recovery_points)

    @rx.event
    def set_selected_month(self, value: list[int] | int) -> None:
        """Set the scrubber month from a slider (list) or an int."""
        raw = value[0] if isinstance(value, list) else value
        try:
            month = int(raw)
        except (TypeError, ValueError):
            month = 0
        self.selected_month = max(0, min(month, self.recovery_month_count))

    @rx.event
    def scrubber_reset(self) -> None:
        """Reset the scrubber to the baseline (today) and stop playing."""
        self.selected_month = 0
        self.scrubber_playing = False

    @rx.event(background=True)
    async def scrubber_play(self):
        """Auto-advance the scrubber month-by-month to tell the recovery story.

        Toggles play/pause; while playing, advances one month roughly every
        500ms until the horizon end, then stops. Pure display choreography — it
        only moves ``selected_month`` across already-computed projected values.
        """
        import asyncio

        async with self:
            if self.scrubber_playing:
                # Pressing while playing acts as pause.
                self.scrubber_playing = False
                return
            count = self.recovery_month_count
            if count == 0:
                return
            # Restart from baseline if we're at (or past) the end.
            if self.selected_month >= count:
                self.selected_month = 0
            self.scrubber_playing = True

        while True:
            await asyncio.sleep(0.5)
            async with self:
                if not self.scrubber_playing:
                    return
                if self.selected_month >= self.recovery_month_count:
                    self.scrubber_playing = False
                    return
                self.selected_month = self.selected_month + 1
            yield

    # ------------------------------------------------------------------
    # Feature 5: recovery-chart geometry (pure presentation, no figures)
    # ------------------------------------------------------------------
    #
    # The chart is a hand-rolled SVG (zero new deps). These vars convert the
    # deterministic ``recovery_serialised`` values into SVG geometry only — the
    # UI computes no business figure, it only maps already-computed numbers to
    # pixel coordinates inside a fixed viewBox.

    #: SVG viewBox geometry (logical units; the <svg> scales responsively).
    _CHART_W: int = 720
    _CHART_H: int = 300
    _CHART_ML: int = 48  # left margin (EWS axis)
    _CHART_MR: int = 16
    _CHART_MT: int = 24
    _CHART_MB: int = 36  # bottom margin (month axis)
    _CHART_EWS_MAX: float = 100.0  # EWS axis is fixed 0..100 for stable scale

    # The scale/axis math below delegates to the dependency-free charts toolkit
    # (``app.frontend.components.charts``) so the recovery chart is the toolkit's
    # first real consumer (Feature 9 §8) and there is ONE correct value->pixel
    # implementation shared with future multi-series views (the P&L bridge,
    # portfolio sparklines). The toolkit returns float pixels; these helpers
    # keep their existing float contract and the @rx.var callers still stringify,
    # so the emitted SVG coordinates are byte-identical to the hand-rolled
    # version (see tests/test_recovery_chart_geometry.py).

    def _chart_bounds(self):
        """Return the inner plot rectangle as a toolkit ``Bounds``."""
        from app.frontend.components.charts import Bounds

        return Bounds(
            x0=float(self._CHART_ML),
            y0=float(self._CHART_MT),
            x1=float(self._CHART_W - self._CHART_MR),
            y1=float(self._CHART_H - self._CHART_MB),
        )

    def _chart_plot(self) -> tuple[float, float, float, float]:
        """Return (x0, y0, plot_w, plot_h) of the inner plotting rectangle.

        Backed by the toolkit ``Bounds`` so the rectangle is defined once; the
        (x0, y0, width, height) shape is preserved for the existing callers.
        """
        b = self._chart_bounds()
        return b.x0, b.y0, b.width, b.height

    def _month_x(self, index: int, count: int, x0: float, plot_w: float) -> float:
        """X pixel for a 1-based month index across ``count`` months.

        Uses the toolkit's even x-spacing so month positions match any series
        the charts toolkit lays out (line points, bars) exactly.
        """
        from app.frontend.components.charts import Bounds, _x_positions

        if count <= 1:
            return x0 + plot_w / 2.0
        bounds = Bounds(x0=x0, y0=0.0, x1=x0 + plot_w, y1=0.0)
        return _x_positions(count, bounds)[index - 1]

    def _ews_y(self, ews: float, y0: float, plot_h: float) -> float:
        """Y pixel for an EWS score (0 at bottom, EWS_MAX at top).

        Delegates to the toolkit's inverted ``LinearScale``; the ``y0`` /
        ``plot_h`` arguments are retained for call-site compatibility and define
        the same vertical range the scale spans.
        """
        from app.frontend.components.charts import Bounds, LinearScale

        bounds = Bounds(x0=0.0, y0=y0, x1=0.0, y1=y0 + plot_h)
        return LinearScale.for_y(0.0, self._CHART_EWS_MAX, bounds).scale(ews)

    def _threshold_y(self) -> float:
        """Numeric Y pixel of the EWS 40 (正常) threshold line (internal helper)."""
        from app.shared.constants import EWS_SUBSTANDARD

        _, y0, _, plot_h = self._chart_plot()
        return round(self._ews_y(float(EWS_SUBSTANDARD), y0, plot_h), 2)

    @rx.var
    def recovery_threshold_y(self) -> str:
        """Y pixel of the EWS 40 (正常) threshold line, as an SVG-coord string."""
        return str(self._threshold_y())

    @rx.var
    def recovery_plot_geom(self) -> dict[str, float]:
        """Inner-plot rectangle geometry for the chart frame / zones."""
        x0, y0, plot_w, plot_h = self._chart_plot()
        return {
            "x0": round(x0, 2),
            "y0": round(y0, 2),
            "w": round(plot_w, 2),
            "h": round(plot_h, 2),
            "x1": round(x0 + plot_w, 2),
            "y1": round(y0 + plot_h, 2),
        }

    # Scalar geometry vars, returned as SVG-coordinate STRINGS. Reflex's SVG
    # coordinate props (rect.x, line.y1, text.x, ...) are typed ``str | int``
    # and reject a ``float`` Var at component-create time with
    # "Invalid var passed for prop Rect.x, expected type str|int, got ... float".
    # The recovery_points dict already uses ``str(round(...))`` for the same
    # reason; these scalars match that contract so every coordinate binds to a
    # string Var the validator accepts.

    @rx.var
    def recovery_x0(self) -> str:
        """Left edge (x) of the inner plot rectangle, in SVG units."""
        return str(round(self._chart_plot()[0], 2))

    @rx.var
    def recovery_y0(self) -> str:
        """Top edge (y) of the inner plot rectangle, in SVG units."""
        return str(round(self._chart_plot()[1], 2))

    @rx.var
    def recovery_w(self) -> str:
        """Width of the inner plot rectangle, in SVG units."""
        return str(round(self._chart_plot()[2], 2))

    @rx.var
    def recovery_x1(self) -> str:
        """Right edge (x) of the inner plot rectangle, in SVG units."""
        x0, _, plot_w, _ = self._chart_plot()
        return str(round(x0 + plot_w, 2))

    @rx.var
    def recovery_y1(self) -> str:
        """Bottom edge (y) of the inner plot rectangle, in SVG units."""
        _, y0, _, plot_h = self._chart_plot()
        return str(round(y0 + plot_h, 2))

    @rx.var
    def recovery_threshold_label_x(self) -> str:
        """X of the 正常 EWS 40 threshold caption (left edge + small inset)."""
        return str(round(self._chart_plot()[0] + 6.0, 2))

    @rx.var
    def recovery_threshold_label_y(self) -> str:
        """Y of the 正常 EWS 40 threshold caption (just above the line)."""
        return str(round(self._threshold_y() - 6.0, 2))

    @rx.var
    def recovery_healthy_zone_h(self) -> str:
        """Height of the healthy-zone band (threshold line down to plot floor)."""
        _, y0, _, plot_h = self._chart_plot()
        return str(round((y0 + plot_h) - self._threshold_y(), 2))

    @rx.var
    def recovery_points(self) -> list[dict[str, str]]:
        """Per-month chart points: bar + EWS dot geometry, hover band, and labels.

        Pure geometry/labels derived from ``recovery_serialised``. Bars encode
        the monthly uplift (height proportional to the max uplift in the
        series); the EWS dot encodes the recomputed score. Each point also
        carries an invisible full-height hover band (``band_*``) used as a wide
        cursor target, and pre-formatted display strings (``ews_label``,
        ``uplift_label``, ``keijo_label``) for the CSS hover tooltip — so the
        tooltip reads already-computed values (the UI computes no figure).
        ``recovered`` flags the dot the recovery marker should pulse on. Empty
        when no projection.
        """
        months = self.recovery_serialised.get("months") or []
        if not months:
            return []
        from app.shared.models.money import format_jpy

        x0, y0, plot_w, plot_h = self._chart_plot()
        count = len(months)
        max_uplift = max((int(m.get("monthly_uplift", 0)) for m in months), default=0) or 1
        bar_w = max(2.0, min(22.0, plot_w / max(count, 1) * 0.5))
        band_w = plot_w / max(count, 1)
        # Tooltip box dimensions (logical SVG units; matches the component).
        tip_w, tip_h = 150.0, 64.0
        points: list[dict[str, str]] = []
        for m in months:
            idx = int(m.get("month_index", 0))
            ews = float(m.get("ews_score", 0.0))
            uplift = int(m.get("monthly_uplift", 0))
            keijo = int(m.get("keijo_rieki", 0))
            cx = self._month_x(idx, count, x0, plot_w)
            cy = self._ews_y(ews, y0, plot_h)
            bar_h = plot_h * (uplift / max_uplift)
            bar_y = y0 + plot_h - bar_h
            # Keep the tooltip inside the plot horizontally.
            tip_x = min(max(cx - tip_w / 2.0, x0), x0 + plot_w - tip_w)
            tip_y = max(cy - tip_h - 12.0, y0)
            points.append(
                {
                    "index": str(idx),
                    "cx": str(round(cx, 2)),
                    "cy": str(round(cy, 2)),
                    "bar_x": str(round(cx - bar_w / 2.0, 2)),
                    "bar_y": str(round(bar_y, 2)),
                    "bar_w": str(round(bar_w, 2)),
                    "bar_h": str(round(max(0.0, bar_h), 2)),
                    "recovered": "yes" if m.get("recovered") else "no",
                    # Invisible hover band (wide cursor target).
                    "band_x": str(round(cx - band_w / 2.0, 2)),
                    "band_y": str(round(y0, 2)),
                    "band_w": str(round(band_w, 2)),
                    "band_h": str(round(plot_h, 2)),
                    # Tooltip box + text positions.
                    "tip_x": str(round(tip_x, 2)),
                    "tip_y": str(round(tip_y, 2)),
                    "tip_w": str(round(tip_w, 2)),
                    "tip_h": str(round(tip_h, 2)),
                    "tip_text_x": str(round(tip_x + 10.0, 2)),
                    "tip_l1_y": str(round(tip_y + 18.0, 2)),
                    "tip_l2_y": str(round(tip_y + 34.0, 2)),
                    "tip_l3_y": str(round(tip_y + 50.0, 2)),
                    # Pre-formatted display strings (no figure computed in UI).
                    "month_label": f"{idx}ヶ月目 (Month {idx})",
                    "ews_label": f"EWS {ews:.2f}",
                    "uplift_label": f"月次改善 {format_jpy(uplift)}",
                    "keijo_label": f"経常利益 {format_jpy(keijo)}",
                }
            )
        return points

    @rx.var
    def recovery_line_path(self) -> str:
        """SVG polyline 'points' attribute string for the EWS curve."""
        pts = self.recovery_points
        return " ".join(f"{p['cx']},{p['cy']}" for p in pts)

    @rx.var
    def recovery_area_path(self) -> str:
        """SVG path 'd' for the soft area fill under the EWS curve."""
        pts = self.recovery_points
        if not pts:
            return ""
        y1 = self.recovery_y1
        first_x = pts[0]["cx"]
        last_x = pts[-1]["cx"]
        body = " ".join(f"L {p['cx']} {p['cy']}" for p in pts)
        return f"M {first_x} {y1} {body} L {last_x} {y1} Z"

    @rx.var
    def recovery_marker(self) -> dict[str, str]:
        """Coordinates + label of the recovery (EWS<40) marker, if reached."""
        idx = self.recovery_serialised.get("recovery_month_index")
        if not idx:
            return {}
        for p in self.recovery_points:
            if p["index"] == str(idx):
                return {"cx": p["cx"], "cy": p["cy"], "month": str(idx)}
        return {}

    @rx.var
    def recovery_caption(self) -> str:
        """Human-readable recovery verdict for the chart header."""
        months = self.recovery_serialised.get("months") or []
        if not months:
            return ""
        from app.shared.constants import EWS_SUBSTANDARD

        idx = self.recovery_serialised.get("recovery_month_index")
        threshold = int(EWS_SUBSTANDARD)
        if idx:
            return f"{idx}ヶ月目で正常化見込 ・ EWS < {threshold} (Normalises in month {idx})"
        return (
            f"{len(months)}ヶ月以内には未達 ・ EWS ≥ {threshold} "
            f"(No recovery within {len(months)} months)"
        )

    # ------------------------------------------------------------------
    # Feature 5: multi-period P&L bridge (損益ブリッジ) — Plan tab
    # ------------------------------------------------------------------
    #
    # The recovery curve answers "when does EWS normalise?"; the P&L bridge
    # answers "how does 経常利益 (ordinary profit) climb to break-even, period by
    # period?". It is a DUAL-AXIS view built on the shared charts toolkit
    # (Feature 9 §8): EWS on the LEFT axis (line) and 経常利益 on the RIGHT axis
    # (bars growing from the break-even zero line). Pure presentation — it maps
    # the deterministic projection's already-computed figures to pixels and
    # computes no business value.

    def _bridge_keijo_domain(self, months: list[dict[str, Any]]) -> tuple[float, float]:
        """Return the right-axis 経常利益 domain, always including break-even (0).

        The bars grow from the zero (break-even) line, so 0 must be inside the
        domain even when every projected month is profitable; a small headroom
        pad keeps the tallest bar off the frame edge.
        """
        values = [float(m.get("keijo_rieki", 0)) for m in months]
        lo = min(0.0, min(values, default=0.0))
        hi = max(0.0, max(values, default=0.0))
        if lo == hi:
            hi = lo + 1.0  # degenerate flat series -> non-zero span
        pad = (hi - lo) * 0.08
        return lo - pad, hi + pad

    @rx.var
    def bridge_points(self) -> list[dict[str, str]]:
        """Per-month P&L-bridge geometry: 経常利益 bars + EWS dots + hover labels.

        Dual-axis via the charts toolkit: EWS uses the left (0..100, inverted)
        scale; 経常利益 uses the right scale spanning the projected profit range
        (with the break-even 0 line inside it). Bars grow from break-even up to
        each month's 経常利益 (down for a loss). All coordinates are emitted as
        SVG-coordinate STRINGS to satisfy Reflex's str|int SVG prop contract
        (mirrors ``recovery_points``). Empty when there is no projection.
        """
        months = self.recovery_serialised.get("months") or []
        if not months:
            return []
        from app.frontend.components.charts import (
            LinearScale,
            Series,
            SeriesKind,
            build_bars,
        )
        from app.shared.models.money import format_jpy

        bounds = self._chart_bounds()
        count = len(months)
        ews_scale = LinearScale.for_y(0.0, self._CHART_EWS_MAX, bounds)
        lo, hi = self._bridge_keijo_domain(months)
        keijo_scale = LinearScale.for_y(lo, hi, bounds)

        keijo_series = Series(
            key="keijo",
            label="経常利益",
            kind=SeriesKind.BARS,
            values=tuple(float(m.get("keijo_rieki", 0)) for m in months),
            accent="positive",
            axis="right",
        )
        bars = build_bars(keijo_series, keijo_scale, bounds, width_ratio=0.5, baseline=0.0)

        points: list[dict[str, str]] = []
        for m, bar in zip(months, bars, strict=True):
            idx = int(m.get("month_index", 0))
            ews = float(m.get("ews_score", 0.0))
            keijo = int(m.get("keijo_rieki", 0))
            uplift = int(m.get("monthly_uplift", 0))
            cx = self._month_x(idx, count, bounds.x0, bounds.width)
            cy = ews_scale.scale(ews)
            profitable = keijo >= 0
            points.append(
                {
                    "index": str(idx),
                    "cx": str(round(cx, 2)),
                    "cy": str(round(cy, 2)),
                    "bar_x": str(round(bar.x, 2)),
                    "bar_y": str(round(bar.y, 2)),
                    "bar_w": str(round(bar.width, 2)),
                    "bar_h": str(round(bar.height, 2)),
                    "profitable": "yes" if profitable else "no",
                    "recovered": "yes" if m.get("recovered") else "no",
                    "month_label": f"{idx}ヶ月目 (Month {idx})",
                    "ews_label": f"EWS {ews:.2f}",
                    "keijo_label": f"経常利益 {format_jpy(keijo)}",
                    "uplift_label": f"月次改善 {format_jpy(uplift)}",
                }
            )
        return points

    @rx.var
    def bridge_line_path(self) -> str:
        """SVG polyline 'points' string for the EWS curve over the bridge."""
        return " ".join(f"{p['cx']},{p['cy']}" for p in self.bridge_points)

    @rx.var
    def bridge_breakeven_y(self) -> str:
        """Y pixel of the 経常利益 break-even (0) line, as an SVG-coord string.

        Empty string when there is no projection (so the component can gate the
        line). The break-even line is where the profit bars cross from loss to
        profit, the single most important reference in the bridge.
        """
        months = self.recovery_serialised.get("months") or []
        if not months:
            return ""
        from app.frontend.components.charts import LinearScale

        bounds = self._chart_bounds()
        lo, hi = self._bridge_keijo_domain(months)
        return str(round(LinearScale.for_y(lo, hi, bounds).scale(0.0), 2))

    @rx.var
    def bridge_caption(self) -> str:
        """Human-readable break-even verdict for the bridge header.

        Reports the first projected month whose 経常利益 turns non-negative
        (break-even), or that no month does within the horizon. Display-only —
        it reads the deterministic projected figures, computing nothing.
        """
        months = self.recovery_serialised.get("months") or []
        if not months:
            return ""
        for m in months:
            if int(m.get("keijo_rieki", 0)) >= 0:
                idx = int(m.get("month_index", 0))
                return f"{idx}ヶ月目で黒字化見込 (Break-even in month {idx})"
        return f"{len(months)}ヶ月以内には黒字化未達 (No break-even within {len(months)} months)"

    @rx.var
    def bridge_table_rows(self) -> list[dict[str, str]]:
        """Per-month bridge figures, pre-formatted for the accessible data table.

        Mirrors the SVG for assistive tech / print (an SVG is opaque to screen
        readers), exposing the same deterministic 経常利益 / 月次改善 / EWS values
        as a real table. Display-only; empty when there is no projection.
        """
        rows: list[dict[str, str]] = []
        for m in self.recovery_serialised.get("months") or []:
            idx = int(m.get("month_index", 0))
            keijo = int(m.get("keijo_rieki", 0))
            rows.append(
                {
                    "month": str(idx),
                    "period": str(m.get("period", "")),
                    "ews": f"{float(m.get('ews_score', 0.0)):.1f}",
                    "uplift": format_jpy(int(m.get("monthly_uplift", 0))),
                    "keijo": format_jpy(keijo),
                    "state": "黒字" if keijo >= 0 else "赤字",
                }
            )
        return rows

    @rx.var
    def bridge_aria_label(self) -> str:
        """One-line screen-reader summary of the P&L bridge."""
        months = self.recovery_serialised.get("months") or []
        if not months:
            return "損益ブリッジはありません (No P&L bridge available)."
        n = len(months)
        be = next(
            (int(m.get("month_index", 0)) for m in months if int(m.get("keijo_rieki", 0)) >= 0),
            None,
        )
        tail = f"{n}ヶ月以内に黒字化しません。" if be is None else f"{be}ヶ月目に黒字化します。"
        return (
            f"損益ブリッジ: {n}ヶ月にわたる経常利益とEWSの推移。{tail} "
            f"(P&L bridge of ordinary profit and EWS over {n} months.)"
        )

    @rx.var
    def has_pnl_bridge(self) -> bool:
        """Whether a P&L bridge is available to render (a projection exists)."""
        return bool(self.recovery_serialised.get("months"))

    @rx.var
    def code_valid(self) -> bool:
        """Whether the TDB code is a well-formed 7-digit code."""
        return self.tdb_code.isdigit() and len(self.tdb_code) == 7

    @rx.var
    def gate_required(self) -> bool:
        """Whether a demo password gate is configured (server-side check)."""
        from app.shared.settings import get_settings

        return bool(get_settings().demo_password)

    @rx.var
    def show_app(self) -> bool:
        """Whether the main app should render (no gate, or already unlocked)."""
        return self.gate_unlocked or not self.gate_required

    @rx.event
    def set_gate_input(self, value: str) -> None:
        """Update the password-gate input buffer."""
        self.gate_input = value
        self.gate_error = ""

    @rx.event
    def submit_gate(self) -> None:
        """Validate the entered password against the configured one server-side.

        The configured password never leaves the server: only the boolean
        ``gate_unlocked`` result is sent to the client.
        """
        from app.shared.settings import get_settings

        expected = get_settings().demo_password
        if expected and self.gate_input == expected:
            self.gate_unlocked = True
            self.gate_input = ""
            self.gate_error = ""
        else:
            self.gate_unlocked = False
            self.gate_error = "パスワードが正しくありません。 (Incorrect password.)"

    @rx.var
    def has_started(self) -> bool:
        """Whether an assessment has been started this session."""
        return self.phase != "idle"

    @rx.var
    def phase_index(self) -> int:
        """Ordinal of the current lifecycle phase for the progress stepper.

        Maps the phase string to a 0-based step so the top-bar stepper can show
        which stage the run is at. ``assessing`` covers intake→EWS→classify (the
        case-file build), ``meeting`` is the creditor meeting, ``awaiting_decision``
        is the banker's turn, and ``drafting``/``done`` finish the Keikakusho.
        ``error`` maps to -1 (no active step). Display-only.
        """
        return {
            "idle": 0,
            "assessing": 1,
            "meeting": 2,
            "awaiting_decision": 3,
            "drafting": 4,
            "done": 5,
            "error": -1,
        }.get(self.phase, 0)

    @rx.var
    def burden_share_basis(self) -> str:
        """The pro-rata share basis for the burden table ('' when no rows).

        Reads the first burden row's ``share_basis`` (all rows share one mode):
        ``stake_based`` means the split rests on real outstanding loan balances;
        ``heuristic_proxy`` means the weaker uplift proxy was used because no
        stake data was available. Display-only transparency for the banker.
        """
        if not self.burden_rows:
            return ""
        return str(self.burden_rows[0].get("share_basis", ""))

    @rx.var
    def classification_label(self) -> str:
        """FSA classification with a graceful empty fallback."""
        return self.fsa_kanji or "—"

    @rx.var
    def ews_accent(self) -> str:
        """Gradient colour for the EWS metric (higher score = worse = redder).

        Display-only: maps the already-computed EWS score onto the shared health
        gradient so the banker reads severity by colour, not by parsing digits.
        """
        from app.frontend.theme import ews_color

        return ews_color(self.ews_score)

    @rx.var
    def hosho_accent(self) -> str:
        """Gradient colour for the guarantee-release score (higher = better).

        Display-only: a high Hosho Kaijo score is good, so it greens up; a low
        score reds out, mirroring the EWS gradient in the opposite direction.
        """
        from app.frontend.theme import score_color

        return score_color(self.hosho_kaijo_score)

    # ------------------------------------------------------------------
    # Internal mapping helpers
    # ------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    def _reset_run(self) -> None:
        """Clear per-run UI state before a fresh assessment."""
        self.error = ""
        self.keikakusho_draft = ""
        self.meeting_events = []
        self.pending_speaker = ""
        self.strategies = []
        self.burden_rows = []
        self.feasibility_rows = []
        self.hosho_pillar_rows = []
        self.hosho_directives = []
        self.ews_breakdown_rows = []
        self.classification_reason = ""
        self.explainability_report_md = ""
        self.shisanhyo_rows = []
        self.calibration_recommendation = ""
        self.calibration_rationale = ""
        self.calibration_rows = []
        self.recovery_serialised = {}
        self.selected_month = 0
        self.scrubber_playing = False
        self.awaiting_decision = False
        self.negotiation_status = "pending"
        self.revision_directive = ""
        self.revision_count = 0
        self.active_node = ""

    def _pending_speaker_for(self, node: str, update: dict[str, Any]) -> str:
        """Return the persona key to show as 'considering' before this bubble.

        Used purely for the transient typing indicator while pacing the stream.
        Critic nodes surface their persona; the chair surfaces lead_arranger;
        narrated nodes surface the system narrator. Returns ``""`` for nodes that
        produce no visible bubble (so no indicator flashes for them).
        """
        if node in _CRITIC_NODES:
            for fb in update.get("critic_feedbacks", []) or []:
                return str(_attr(fb, "persona", "system"))
            return "system"
        if node == "lead_arranger":
            return "lead_arranger"
        if _NODE_NARRATION.get(node):
            return "system"
        return ""

    def _push_event(self, event: MeetingEvent) -> None:
        """Append a transcript event (kept as a new list for Reflex reactivity)."""
        self.meeting_events = [*self.meeting_events, event]

    def _ingest_node_update(self, node: str, update: dict[str, Any]) -> None:
        """Translate one streamed node update into transcript event(s).

        Args:
            node: The graph node name that just completed.
            update: The partial state dict the node returned.
        """
        self.active_node = node

        # Critic nodes append CriticFeedback dicts — render each as a persona bubble.
        if node in _CRITIC_NODES:
            for fb in update.get("critic_feedbacks", []) or []:
                self._push_event(
                    MeetingEvent(
                        kind="critic",
                        speaker=str(_attr(fb, "persona", "system")),
                        status=str(_attr(fb, "status", "")),
                        priority=str(_attr(fb, "priority", "")),
                        title=str(_attr(fb, "rationale", "")),
                        blockers=[
                            _strip_blocker_code(str(b))
                            for b in _attr(fb, "fatal_blockers", []) or []
                        ],
                        # PART 4 + Feature 0: the persona's advisory negotiating
                        # stance, already grounded (flag mode) by the critic so any
                        # unattributable assertion is marked 【未検証 / unverified】.
                        # Surfaced as the bubble body so the banker can rehearse
                        # each creditor's voice. Empty offline (no LLM) -> the
                        # bubble renders exactly as before.
                        body=str(_attr(fb, "simulated_argument", "") or ""),
                    )
                )
            return

        # Lead arranger speaks as the chair with the consolidated outcome.
        if node == "lead_arranger":
            self.phase = "meeting"
            status = str(update.get("negotiation_status", ""))
            self._push_event(
                MeetingEvent(
                    kind="chair",
                    speaker="lead_arranger",
                    status=status.upper(),
                    priority="",
                    title=_CHAIR_TITLES.get(status, "取りまとめ (Consolidation)"),
                    body=_clean_directive_for_display(str(update.get("revision_directive", ""))),
                    blockers=[],
                )
            )
            return

        # Otherwise emit a plain system-narration bubble when we have copy for it.
        narration = _NODE_NARRATION.get(node)
        if narration:
            self._push_event(
                MeetingEvent(
                    kind="system",
                    speaker="system",
                    status="",
                    priority="",
                    title=narration,
                    body="",
                    blockers=[],
                )
            )

    def _apply_snapshot(self, values: dict[str, Any]) -> None:
        """Map final graph state values onto case-file display fields."""
        profile = values.get("company_profile")
        self.company_name = _attr(profile, "name", "") or self.tdb_code
        self.fsa_kanji = _fsa_kanji(values.get("fsa_classification"))
        self.ews_score = float(values.get("ews_score") or 0.0)
        self.classification_reason = str(values.get("classification_reason") or "")
        self.ews_breakdown_rows = [
            {
                "key": str(_attr(s, "key", "")),
                "label": str(_attr(s, "label_ja", "")),
                # raw is a 0-1 fraction; show as a percentage measure.
                "raw_pct": f"{float(_attr(s, 'raw', 0.0)) * 100:.1f}%",
                "points": f"{float(_attr(s, 'points', 0.0)):.1f}",
                "weight": f"{float(_attr(s, 'weight', 0.0)):.0f}",
                # Share of this signal's weight that was 'used' (points/weight),
                # as a 0-100 string for the contribution bar width.
                "fill_pct": (
                    f"{(float(_attr(s, 'points', 0.0)) / float(_attr(s, 'weight', 1.0)) * 100):.0f}"
                    if float(_attr(s, "weight", 0.0)) > 0
                    else "0"
                ),
            }
            for s in values.get("ews_breakdown", []) or []
        ]

        gap = values.get("working_capital_gap")
        self.working_capital_gap_display = format_jpy(gap) if gap is not None else "—"

        # Loan-lifecycle: derive the current facility status + provision for the
        # case file (display-only). Surfaces what the spine persists on every
        # route -- notably the WORKOUT transition the terminal workout path
        # records, which never reaches the HITL interrupt payload.
        self._apply_loan_summary(values)

        # PART 2: Hosho Kaijo.
        self.hosho_kaijo_score = float(values.get("hosho_kaijo_score") or 0.0)
        self.succession_ready = bool(values.get("succession_ready") or False)
        self.hosho_eligible = bool(values.get("hosho_kaijo_eligible") or False)
        self._apply_hosho_conditions(values.get("hosho_kaijo_conditions"))

        # PART 3: Creditor meeting.
        self.negotiation_status = str(values.get("negotiation_status") or "pending")
        self.revision_directive = str(values.get("revision_directive") or "")
        self.revision_count = int(values.get("revision_count") or 0)

        self.shisanhyo_rows = [
            {
                "period": _period_str(_attr(tb, "period")),
                "uriage": format_jpy(int(_attr(tb, "uriage", 0))),
                "uriage_genka": format_jpy(int(_attr(tb, "uriage_genka", 0))),
                "keijo_rieki": format_jpy(int(_attr(tb, "keijo_rieki", 0))),
            }
            for tb in values.get("shisanhyo", [])
        ]
        self.strategies = [
            {
                "index": str(i),
                "title": str(_attr(s, "title", "")),
                "rationale": str(_attr(s, "rationale", "")),
                "uplift": format_jpy(int(_attr(s, "expected_keijo_uplift", 0))),
            }
            for i, s in enumerate(values.get("proposed_strategies", []))
        ]
        self.keikakusho_draft = values.get("keikakusho_draft") or ""

        # Feature 7: cache the exportable explainability report. Built from the
        # SAME backend snapshot the breakdown panels above read, so the report
        # and the on-screen panels tell one consistent story. Best-effort: a
        # build failure clears the cache (hiding the button) but never breaks the
        # run. Empty when the borrower has not been classified yet.
        self._refresh_explainability_report(values)

    def _apply_loan_summary(self, values: dict[str, Any]) -> None:
        """Derive the loan-facility status + provision display from the snapshot.

        Display-only: reads the append-only ``loan_events`` log and derives the
        current :class:`~app.shared.models.loan.LoanStatus` (kanji + english) via
        the same ``current_status`` the spine uses, plus the deterministic
        loan-loss provision (貸倒引当金) from outstanding principal (sum of
        ``lender_stakes``) and the FSA class. This makes the persisted lifecycle
        status visible in the case file on EVERY route -- including the terminal
        workout path, which records a WORKOUT transition but never produces a
        HITL interrupt payload (where the loan summary was previously the only
        place these figures surfaced).

        Best-effort and rehydration-safe: ``loan_events`` may be plain dicts from
        the checkpointer, so each is validated back into a ``LoanEvent``. Any
        failure (or no attached loan) clears the fields rather than breaking the
        run. The UI computes no figure of its own -- the provision is the
        deterministic ``provision_amount`` of the spine.
        """
        events_raw = values.get("loan_events") or []
        if not events_raw:
            self.loan_status_kanji = ""
            self.loan_status_english = ""
            self.loan_provision_display = "—"
            self.loan_id_display = ""
            self.run_lender_stakes = {}
            return
        self.loan_id_display = str(values.get("loan_id") or "")
        self.run_lender_stakes = {
            str(k): int(v) for k, v in (values.get("lender_stakes") or {}).items()
        }
        try:
            events = [LoanEvent.model_validate(e) for e in events_raw]
            status = current_status(events)
        except Exception as exc:  # noqa: BLE001 - display must never break the run
            _log.warning("ui.loan_summary_failed", error=str(exc))
            self.loan_status_kanji = ""
            self.loan_status_english = ""
            self.loan_provision_display = "—"
            return

        self.loan_status_kanji = status.kanji
        self.loan_status_english = status.english

        # Provision: TRUE outstanding principal (残高 = original − cumulative
        # repayments) x FSA-class reserve ratio, mirroring the HITL loan summary
        # and workout handoff. Uses the shared outstanding_principal_for_state
        # seam so a partially-repaid facility reserves against its real declining
        # balance, not the full lender-stakes snapshot.
        from app.shared.models.loan import outstanding_principal_for_state

        classification = values.get("fsa_classification")
        outstanding = outstanding_principal_for_state(
            SimpleNamespace(
                lender_stakes=values.get("lender_stakes") or {},
                loan_events=values.get("loan_events") or [],
            )
        )
        if classification and outstanding > 0:
            try:
                from app.shared.models.loan import provision_amount

                fsa = (
                    classification
                    if isinstance(classification, FsaClass)
                    else FsaClass(str(classification))
                )
                provision = provision_amount(
                    outstanding,
                    fsa,
                    special_attention=bool(values.get("special_attention")),
                )
                self.loan_provision_display = format_jpy(provision)
            except Exception as exc:  # noqa: BLE001 - display-only, never fatal
                _log.warning("ui.loan_provision_failed", error=str(exc))
                self.loan_provision_display = "—"
        else:
            self.loan_provision_display = "—"

    def _refresh_explainability_report(self, values: dict[str, Any]) -> None:
        """Build + cache the deterministic explainability report Markdown.

        Mirrors :meth:`_refresh_recovery`: it renders a JSON-safe artifact (here
        a Markdown string) from the final snapshot at finalize time so the
        download button can emit it on click without re-reading the checkpointer.
        Pure/offline (the renderer formats already-computed figures and computes
        nothing). Reads the raw ``values`` dict directly — the renderer is
        rehydration-safe. Cleared (button hidden) when there is no classification
        or on any failure; never fatal to the UI.
        """
        if not values.get("fsa_classification"):
            self.explainability_report_md = ""
            return
        try:
            from app.backend.export.explainability_report import (
                build_explainability_report,
            )

            self.explainability_report_md = build_explainability_report(values)
        except Exception as exc:  # noqa: BLE001 - export cache is best-effort
            _log.warning("ui.explainability_refresh_failed", error=str(exc))
            self.explainability_report_md = ""

    @rx.var
    def has_explainability_report(self) -> bool:
        """Whether a deterministic explainability report is available to export."""
        return self.explainability_report_md != ""

    @rx.var
    def pdf_export_available(self) -> bool:
        """Whether PDF export is possible (a CJK font is vendored / configured).

        PDF export embeds a Japanese font that is a build/deploy input; when none
        is resolvable the renderer raises rather than emit unreadable tofu. The
        UI gates its PDF buttons on this so a banker is never offered a PDF that
        would fail — DOCX (always available) remains the fallback.
        """
        from app.backend.export._markdown_pdf import pdf_font_available

        return pdf_font_available()

    @rx.event
    def download_explainability_docx(self):
        """Download the deterministic explainability report as a Word (.docx) file.

        Re-renders the report cached at finalize time (``explainability_report_md``)
        to ``.docx`` via the shared, number-safe markdown->docx renderer, so a
        banker/examiner can archive or attach the classification basis as an
        editable Word artifact (the format banks / FSA examiners exchange). Every
        figure is carried verbatim (numeric-preservation holds); the UI computes
        nothing here. No-op until a borrower has been classified. The cached
        Markdown is retained only as the internal source of truth this renderer
        consumes.
        """
        if not self.explainability_report_md:
            return None
        from app.backend.export._markdown_docx import render_markdown_to_docx
        from app.backend.export.explainability_report import (
            explainability_docx_filename,
        )

        filename = explainability_docx_filename(self.company_name or self.tdb_code)
        return rx.download(
            data=render_markdown_to_docx(self.explainability_report_md),
            filename=filename,
        )

    @rx.event
    def download_explainability_pdf(self):
        """Download the deterministic explainability report as a PDF (.pdf) file.

        The archival PDF path for the SAME report: re-renders the cached report
        Markdown to a CJK-correct, searchable ``.pdf`` via the shared number-safe
        renderer. Every figure is carried verbatim; the UI computes nothing here.
        No-op until a borrower has been classified, and a safe no-op (best-effort,
        logged) if no CJK font is available — the button is gated on
        ``pdf_export_available`` so this should not normally be reached.
        """
        if not self.explainability_report_md:
            return None
        from app.backend.export._markdown_pdf import (
            PdfFontUnavailableError,
            render_markdown_to_pdf,
        )
        from app.backend.export.explainability_report import (
            explainability_pdf_filename,
        )

        try:
            data = render_markdown_to_pdf(self.explainability_report_md)
        except PdfFontUnavailableError as exc:  # gated by the UI; defensive here
            _log.warning("ui.explainability_pdf_unavailable", error=str(exc))
            return None
        filename = explainability_pdf_filename(self.company_name or self.tdb_code)
        return rx.download(data=data, filename=filename)

    @rx.event
    def download_model_card_docx(self):
        """Download the engine model card as a Word (.docx) file.

        The Word path for the SAME card ``download_model_card`` emits as
        Markdown, rendered via the shared number-safe markdown->docx renderer so
        a regulator can receive the card (engine type, FSA cascade with live
        thresholds, the full governing-constants table, intended use + limits) as
        an editable Word document. Engine-level (always available); pure/offline.
        """
        from app.backend.export.model_card import (
            build_model_card_docx,
            model_card_docx_filename,
        )

        return rx.download(
            data=build_model_card_docx(),
            filename=model_card_docx_filename(),
        )

    @rx.event
    def download_constants_changelog_docx(self):
        """Download the governing-constants change log as a Word (.docx) file.

        The Word path for ``download_constants_changelog``: the live thresholds
        diffed against the committed baseline (old -> new), as an editable Word
        document an examiner can attach. Pure/offline; figures carried verbatim.
        """
        from app.backend.export.model_card import (
            build_constants_changelog_docx,
            constants_changelog_docx_filename,
            load_constants_baseline,
        )

        return rx.download(
            data=build_constants_changelog_docx(previous=load_constants_baseline()),
            filename=constants_changelog_docx_filename(),
        )

    @rx.event
    def download_model_card_pdf(self):
        """Download the engine model card as a PDF (.pdf) file.

        The archival PDF path for the model card, rendered CJK-correct + searchable
        via the shared number-safe renderer. Engine-level (always available when a
        font is configured); pure/offline. Safe best-effort no-op (logged) if no
        CJK font is available — the button is gated on ``pdf_export_available``.
        """
        from app.backend.export._markdown_pdf import PdfFontUnavailableError
        from app.backend.export.model_card import (
            build_model_card_pdf,
            model_card_pdf_filename,
        )

        try:
            data = build_model_card_pdf()
        except PdfFontUnavailableError as exc:  # gated by the UI; defensive here
            _log.warning("ui.model_card_pdf_unavailable", error=str(exc))
            return None
        return rx.download(data=data, filename=model_card_pdf_filename())

    @rx.event
    def download_constants_changelog_pdf(self):
        """Download the governing-constants change log as a PDF (.pdf) file.

        The archival PDF path for the change log (live thresholds diffed against
        the committed baseline, old -> new), CJK-correct + searchable. Pure/offline.
        Safe best-effort no-op (logged) if no CJK font is available.
        """
        from app.backend.export._markdown_pdf import PdfFontUnavailableError
        from app.backend.export.model_card import (
            build_constants_changelog_pdf,
            constants_changelog_pdf_filename,
            load_constants_baseline,
        )

        try:
            data = build_constants_changelog_pdf(previous=load_constants_baseline())
        except PdfFontUnavailableError as exc:  # gated by the UI; defensive here
            _log.warning("ui.changelog_pdf_unavailable", error=str(exc))
            return None
        return rx.download(data=data, filename=constants_changelog_pdf_filename())

    def _apply_hosho_conditions(self, conditions: Any) -> None:
        """Map the HoshoKaijoConditions into per-pillar display rows + directives.

        Feature 7 explainability for the guarantee-release (保証解除) score: the
        backend computes a rich three-pillar breakdown (法人個人分離 /
        財務基盤 / 情報開示) with per-pillar met/score/weight and actionable
        directives, but only the final number was shown. This surfaces the basis
        so the banker sees which condition is unmet and exactly what must change
        to release the personal guarantee. Display-only and best-effort; accepts
        either a model or a rehydrated dict (via ``_attr``). Cleared when absent.
        """
        if not conditions:
            self.hosho_pillar_rows = []
            self.hosho_directives = []
            return
        pillars = [
            ("bunri", "法人個人分離（Asset separation）", 40.0),
            ("zaimu", "財務基盤（Financial base）", 35.0),
            ("kaiji", "情報開示（Disclosure）", 25.0),
        ]
        rows: list[dict[str, str]] = []
        for key, label, weight in pillars:
            score = float(_attr(conditions, f"{key}_score", 0.0) or 0.0)
            met = bool(_attr(conditions, f"{key}_met", False))
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "met": "yes" if met else "no",
                    "score": f"{score:.1f}",
                    "weight": f"{weight:.0f}",
                    "fill_pct": f"{(score / weight * 100):.0f}" if weight > 0 else "0",
                    "directive": str(_attr(conditions, f"{key}_directive", "") or ""),
                }
            )
        self.hosho_pillar_rows = rows
        self.hosho_directives = [
            str(d) for d in (_attr(conditions, "ordered_directives", []) or [])
        ]

    def _apply_feasibility_rows(self, values: dict[str, Any]) -> None:
        """Map advisory feasibility notes (+ claim provenance) to display rows.

        Feature 0 phase 4: surfaces the deterministic achievability band/score,
        the abstaining advisory text, and the per-claim provenance the grounding
        pipeline produced, so the banker can see which advisory claims are
        attributable and to what. Display-only and best-effort: it never edits a
        figure, gate, or route. Rows with no advisory AND no provenance are
        dropped so an offline (no-LLM) run shows nothing new.
        """
        rows: list[FeasibilityRow] = []
        for note in values.get("feasibility_notes", []) or []:
            advisory = str(_attr(note, "advisory", "") or "")
            raw_prov = _attr(note, "advisory_provenance", []) or []
            provenance = [
                FeasibilityClaim(
                    text=str(_attr(p, "text", "") or ""),
                    status=str(_attr(p, "status", "") or ""),
                    citations=", ".join(str(c) for c in (_attr(p, "citations", []) or [])),
                )
                for p in raw_prov
                # Headings / fragments carry no banker signal; only show claims.
                if str(_attr(p, "status", "")) in ("grounded", "unverified")
            ]
            if not advisory and not provenance:
                continue
            rows.append(
                FeasibilityRow(
                    title=str(_attr(note, "strategy_title", "") or ""),
                    band=str(_attr(note, "achievability", "") or ""),
                    score=f"{float(_attr(note, 'achievability_score', 0.0) or 0.0):.0f}",
                    advisory=advisory,
                    provenance=provenance,
                )
            )
        self.feasibility_rows = rows

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @rx.event(background=True)
    async def run_assessment(self):
        """Stream the graph from start to the HITL interrupt as a live meeting.

        Runs in the background so the UI stays responsive and each node's result
        is appended to the transcript as it completes (no blank wait).
        """
        async with self:
            if not self.code_valid:
                self.error = "TDB企業コードは7桁の数字で入力してください。 (Enter a 7-digit code.)"
                return
            self._reset_run()
            self.thread_id = str(uuid.uuid4())
            self.phase = "assessing"
            tdb_code = self.tdb_code
            config = self._config()
            self._push_event(
                MeetingEvent(
                    kind="system",
                    speaker="system",
                    title=f"診断を開始しました・TDB {tdb_code} (Assessment started.)",
                )
            )

        async for _ in self._drive_stream({"tdb_code": tdb_code}, config, resume=False):
            yield

    async def _drive_stream(self, payload: dict[str, Any], config: dict[str, Any], *, resume: bool):
        """Drive a graph stream, applying each node update on the event loop.

        A worker thread runs the *blocking* LangGraph stream and pushes each
        ``{node: update}`` chunk onto a thread-safe queue. This coroutine drains
        the queue, applies each update under the Reflex state lock
        (``async with self``), and ``yield``s so the delta is flushed to the
        browser before the next event — giving a true live transcript.

        Mutating Reflex state must happen on the event loop under the lock, never
        from the worker thread; the queue is the hand-off boundary.
        """
        queue: _Queue[Any] = _Queue()
        _DONE = object()

        def _worker() -> None:
            # Catch BaseException (not just Exception): a BaseException raised in
            # the worker (e.g. KeyboardInterrupt/SystemExit, or one surfaced by
            # the checkpointer/graph) must still be reported AND the sentinel
            # enqueued. Otherwise the draining coroutine blocks forever on
            # ``queue.get`` because no ``done`` is ever pushed. The ``finally``
            # below already guarantees the sentinel, but catching BaseException
            # ensures the error is surfaced to the UI rather than swallowed.
            try:
                with make_checkpointer() as cp:
                    graph_app = compile_graph(checkpointer=cp)
                    command = Command(resume=payload) if resume else payload
                    for chunk in graph_app.stream(command, config=config, stream_mode="updates"):
                        queue.put(("chunk", chunk))
            except BaseException as exc:  # noqa: BLE001 - propagate to the UI thread
                queue.put(("error", str(exc)))
            finally:
                queue.put(("done", _DONE))

        worker = asyncio.create_task(asyncio.to_thread(_worker))
        # Deliberate per-bubble pacing so the deterministic spine (which streams
        # in milliseconds offline) reads as a live, turn-by-turn meeting rather
        # than an all-at-once dump. Read once; 0 disables (tests/CI).
        from app.shared.settings import get_settings

        pace = max(0.0, float(get_settings().ui_meeting_pace_seconds))
        try:
            while True:
                kind, payload_item = await asyncio.to_thread(queue.get)
                if kind == "done":
                    break
                if kind == "error":
                    async with self:
                        self.error = str(payload_item)
                        self.phase = "error"
                        self.pending_speaker = ""
                    await worker
                    return
                # kind == "chunk"
                for node, update in payload_item.items():
                    if not isinstance(update, dict):
                        continue
                    # Pace BEFORE applying the update: show a transient
                    # "considering" indicator for the upcoming speaker, wait,
                    # then reveal the bubble. Skipped entirely when pace == 0.
                    if pace > 0:
                        speaker = self._pending_speaker_for(node, update)
                        if speaker:
                            async with self:
                                self.pending_speaker = speaker
                                self.active_node = node
                            yield
                            await asyncio.sleep(pace)
                    async with self:
                        self.pending_speaker = ""
                        self._ingest_node_update(node, update)
                    yield
        finally:
            await worker

        async with self:
            self.pending_speaker = ""
            self._finalize_after_stream(config)
        yield

    def _finalize_after_stream(self, config: dict[str, Any]) -> None:
        """Read the final snapshot and settle the phase after streaming."""
        with make_checkpointer() as cp:
            graph_app = compile_graph(checkpointer=cp)
            snapshot = graph_app.get_state(config)
        values = dict(snapshot.values)
        self._apply_snapshot(values)
        self._apply_feasibility_rows(values)
        self._refresh_burden_rows(values)
        self._refresh_calibration_rows(values)
        self._refresh_recovery(values)
        self._capture_portfolio_snapshot()
        self.active_node = ""
        if snapshot.next:
            self.awaiting_decision = True
            self.phase = "awaiting_decision"
        else:
            self.phase = "done"

    def _refresh_recovery(self, values: dict[str, Any]) -> None:
        """Recompute the Feature 5 recovery projection cache from current state.

        Deterministic and best-effort, mirroring :meth:`_refresh_burden_rows`:
        rebuilds ``SaiseiState`` and runs the pure ``project_recovery`` over the
        approved strategy's uplift, caching a JSON-safe serialisation of the
        projection so the Excel export can rebuild it on a button click without
        re-reading the checkpointer. Cleared when there is no approved strategy
        or insufficient history (so the Excel button hides). Never fatal to UI.
        """
        try:
            from app.backend.analysis.pnl_recovery import project_recovery
            from app.backend.state import SaiseiState

            state = SaiseiState(**values)
            if state.approved_strategy is None or len(state.shisanhyo) < 2:
                self.recovery_serialised = {}
                return
            proj = project_recovery(
                state.shisanhyo, int(state.approved_strategy.expected_keijo_uplift)
            )
        except Exception as exc:  # noqa: BLE001 - export cache is best-effort
            _log.warning("ui.recovery_refresh_failed", error=str(exc))
            self.recovery_serialised = {}
            return
        if not proj.months:
            self.recovery_serialised = {}
            return
        self.recovery_serialised = {
            "annual_uplift": int(proj.annual_uplift),
            "full_monthly_uplift": int(proj.full_monthly_uplift),
            "ramp_months": int(proj.ramp_months),
            "recovery_month_index": proj.recovery_month_index,
            "baseline_ews": float(proj.baseline_ews),
            "months": [
                {
                    "month_index": int(m.month_index),
                    "period": m.period.isoformat(),
                    "monthly_uplift": int(m.monthly_uplift),
                    "keijo_rieki": int(m.keijo_rieki),
                    "ews_score": float(m.ews_score),
                    "recovered": bool(m.recovered),
                }
                for m in proj.months
            ],
        }

    def _refresh_burden_rows(self, values: dict[str, Any]) -> None:
        """Recompute the burden-sharing table for display from current state.

        Deterministic and display-only — reuses the backend helper so the table
        shown matches exactly what the lead arranger consolidated.
        """
        try:
            from app.backend.nodes.lead_arranger import compute_burden_sharing_table
            from app.backend.state import SaiseiState

            state = SaiseiState(**values)
            rows = compute_burden_sharing_table(state)
        except Exception:  # noqa: BLE001 - table is best-effort, never fatal to UI
            self.burden_rows = []
            return
        self.burden_rows = [
            {
                "persona": str(r.get("persona", "")),
                "lender": str(r.get("lender", "")),
                "share": f"{r.get('share_pct', 0)}%",
                "grace": f"{r.get('grace_period_months', 0)}ヶ月",
                "haircut": f"{r.get('haircut_pct', 0)}%",
                "new_money": format_jpy(int(r.get("new_money_jpy", 0))),
                "allocation": str(r.get("allocation_type", "")),
                "share_basis": str(r.get("share_basis", "")),
            }
            for r in rows
        ]

    def _refresh_calibration_rows(self, values: dict[str, Any]) -> None:
        """Recompute the threshold-calibration panel from captured outcomes.

        Display-only and best-effort, mirroring :meth:`_refresh_burden_rows`: it
        reuses the backend ``calibrate_reconciliation_threshold`` over the
        captured ``reconciliation_outcomes`` and renders the result. It never
        edits ``RECONCILIATION_BAND_DISTANCE``. Any failure clears the panel
        (and logs a diagnostic) rather than breaking the run.

        The panel is conditional on captured outcomes existing: when none are
        present (the common no-reconciliation run) the panel stays empty so
        existing runs look unchanged.
        """
        try:
            from app.backend.analysis.threshold_calibration import (
                calibrate_reconciliation_threshold,
                report_to_display_rows,
            )

            raw = values.get("reconciliation_outcomes") or []
            outcomes = [o for o in raw if isinstance(o, dict)]
            # If there are no usable outcomes, keep the panel empty rather than
            # rendering a misleading zero-filled report from an empty corpus.
            if not outcomes:
                self.calibration_recommendation = ""
                self.calibration_rationale = ""
                self.calibration_rows = []
                return
            report = calibrate_reconciliation_threshold(outcomes)
            rows = report_to_display_rows(report)
        except Exception as exc:  # noqa: BLE001 - panel is best-effort, never fatal
            _log.warning("ui.calibration_refresh_failed", error=str(exc))
            self.calibration_recommendation = ""
            self.calibration_rationale = ""
            self.calibration_rows = []
            return
        rec = report.recommended_band_distance
        self.calibration_recommendation = "" if rec is None else str(rec)
        self.calibration_rationale = report.rationale
        self.calibration_rows = rows

    # --- Resume (HITL decisions), streamed ---

    @rx.event(background=True)
    async def resume_streamed(self, payload: dict[str, Any]):
        """Resume the graph after a banker decision, streaming the continuation.

        Drives the same queue-based ``_drive_stream`` used by the initial run
        (with ``resume=True``) so node updates are applied under the Reflex state
        lock on the event loop and flushed live per chunk — never mutated from
        the worker thread. ``_drive_stream`` also finalizes the snapshot itself.
        """
        async with self:
            self.awaiting_decision = False
            self.phase = "drafting" if payload.get("decision") == "approve" else "meeting"
            config = self._config()
            self._push_event(
                MeetingEvent(
                    kind="banker",
                    speaker="banker",
                    status=str(payload.get("decision", "")).upper(),
                    title=_BANKER_TITLES.get(str(payload.get("decision", "")), "決定 (Decision)"),
                    body=str(payload.get("revision_note", "") or ""),
                )
            )

        async for _ in self._drive_stream(payload, config, resume=True):
            yield

    # --- Part 6: Excel/CSV upload events ---

    @rx.event
    async def handle_upload_and_stage(self, files: list[rx.UploadFile]):
        """Read an uploaded .xlsx / .csv trial-balance file and start parsing.

        IMPORTANT: this MUST be a FOREGROUND event. Reflex does not support
        ``@rx.event(background=True)`` for an upload handler that receives
        ``rx.UploadFile`` objects (it raises an UploadTypeError) — the file must
        be read on the foreground event that the upload request is bound to.

        Reading the file (``await file.read()``) is awaitable I/O and is safe on
        a foreground event. The CPU-bound parse is then delegated to the
        background event :meth:`parse_uploaded_bytes`, so the dropzone spinner
        stays responsive without making the upload handler itself a background
        task.

        The pipeline is NOT triggered; the banker must explicitly confirm via
        :meth:`confirm_upload`. The UI never computes a figure — it only shows
        what the parser returned.
        """
        if not files:
            return
        file = files[0]
        self.upload_processing = True
        self.upload_preview_rows = []
        self.upload_warnings = []

        try:
            data = await file.read()
            filename = file.filename or "upload"
        except Exception as exc:  # noqa: BLE001
            self.upload_warnings = [f"Failed to read uploaded file: {exc}"]
            self.upload_processing = False
            return

        # Hand the raw bytes off to the background parser. ``data`` is bytes and
        # ``filename`` is a str, both JSON-serialisable, so they cross the event
        # boundary cleanly (unlike the rx.UploadFile objects, which cannot be
        # passed to a background event).
        return SaiseiUIState.parse_uploaded_bytes(data, filename)

    @rx.event(background=True)
    async def parse_uploaded_bytes(self, data: bytes, filename: str):
        """Parse already-read upload bytes in the background and stage for review.

        Runs the deterministic
        :func:`~app.backend.tools.shisanhyo_parser.parse_shisanhyo` parser in a
        worker thread, then stores the proposed rows and any warnings in staging
        state for the banker to review. Separated from
        :meth:`handle_upload_and_stage` because the upload handler must run in the
        foreground (Reflex requirement) while the blocking parse belongs off the
        event loop.
        """

        def _parse() -> tuple[list[dict[str, str]], list[dict[str, Any]], list[str]]:
            from app.backend.tools.shisanhyo_parser import parse_shisanhyo

            parsed = parse_shisanhyo(data, filename)
            preview = [
                {
                    "index": str(i),
                    "period": _period_str(_attr(tb, "period")),
                    "uriage": format_jpy(int(_attr(tb, "uriage", 0))),
                    "uriage_genka": format_jpy(int(_attr(tb, "uriage_genka", 0))),
                    "uriage_raw": str(int(_attr(tb, "uriage", 0))),
                    "uriage_genka_raw": str(int(_attr(tb, "uriage_genka", 0))),
                    "keijo_rieki": format_jpy(int(tb.keijo_rieki)),
                }
                for i, tb in enumerate(parsed.rows)
            ]
            # Serialise typed rows (JSON-safe dicts) so confirm can rebuild them
            # and feed the pipeline; survives the Reflex state boundary.
            serialised = [
                {
                    "period": tb.period.isoformat(),
                    "uriage": int(tb.uriage),
                    "uriage_genka": int(tb.uriage_genka),
                    "hanbaihi": int(tb.hanbaihi),
                    "eigai_shueki": int(tb.eigai_shueki),
                    "eigai_hiyo": int(tb.eigai_hiyo),
                }
                for tb in parsed.rows
            ]
            return preview, serialised, parsed.warnings

        try:
            preview_rows, serialised_rows, warnings = await asyncio.to_thread(_parse)
        except Exception as exc:  # noqa: BLE001
            async with self:
                self.upload_warnings = [f"Parser error: {exc}"]
                self.upload_processing = False
            return

        async with self:
            self.upload_preview_rows = preview_rows
            self.upload_serialised = serialised_rows
            self.upload_warnings = warnings
            self.upload_processing = False

    @rx.event
    def start_guided_entry(self, months: int = 12) -> None:
        """Seed blank staged rows for GUIDED MANUAL ENTRY (Feature 8 channel 4).

        The no-data case: for a prospect absent from core banking (and with no
        spreadsheet to upload), the banker enters the handful of figures EWS
        needs directly. This deliberately reuses the EXISTING upload staging
        seam: it seeds ``months`` blank rows (zeroed yen, trailing month-end
        periods, newest last) into ``upload_serialised`` + ``upload_preview_rows``
        so the SAME editable preview, per-row J-GAAP validation
        (``upload_row_errors``), and ``confirm_upload`` path take over unchanged.
        No new graph/confirm logic, no LLM, no network — the deterministic spine
        consumes byte-identical ``TrialBalance`` rows whether typed or uploaded.

        Display-only: it builds empty rows for the banker to fill; it computes
        no figure (the zeros are placeholders the banker overwrites).
        """
        import calendar

        n = max(1, min(36, int(months)))
        today = dt.date.today()

        def _month_end_back(k: int) -> dt.date:
            """Return the month-end k months before the current month (k>=0)."""
            month_index = (today.year * 12 + (today.month - 1)) - k
            year, month0 = divmod(month_index, 12)
            month = month0 + 1
            last_day = calendar.monthrange(year, month)[1]
            return dt.date(year, month, last_day)

        # Oldest first so the newest period is the last row (matches a file's
        # natural chronological order the pipeline expects).
        periods = [_month_end_back(k).isoformat() for k in range(n - 1, -1, -1)]

        serialised: list[dict[str, Any]] = []
        preview: list[dict[str, str]] = []
        for i, period in enumerate(periods):
            serialised.append(
                {
                    "period": period,
                    "uriage": 0,
                    "uriage_genka": 0,
                    "hanbaihi": 0,
                    "eigai_shueki": 0,
                    "eigai_hiyo": 0,
                }
            )
            preview.append(
                {
                    "index": str(i),
                    "period": period,
                    "uriage": format_jpy(0),
                    "uriage_genka": format_jpy(0),
                    "uriage_raw": "0",
                    "uriage_genka_raw": "0",
                    "keijo_rieki": format_jpy(0),
                }
            )
        self.upload_is_guided = True
        self.upload_serialised = serialised
        self.upload_preview_rows = preview
        self.upload_warnings = []
        self.upload_processing = False

    @rx.event(background=True)
    async def confirm_upload(self):
        """Confirm the proposed upload rows and trigger the normal assessment run.

        Clears the staging display fields, then triggers the normal assessment
        run on the confirmed TDB code (reusing the existing run/stream flow).
        The banker has reviewed the proposed rows and chosen to proceed.

        The UI never computes a figure: it only rebuilds the typed rows the
        deterministic parser already produced and injects them as the initial
        ``shisanhyo`` so the pipeline (EWS/classification/macro) consumes the
        banker's uploaded figures instead of the MockDataProvider fixture.
        """
        import datetime as _dt

        from app.shared.models.accounting import TrialBalance as _TB

        async with self:
            if not self.upload_serialised:
                return
            serialised = list(self.upload_serialised)
            # Clear staging display state before running.
            self.upload_preview_rows = []
            self.upload_warnings = []
            self.upload_serialised = []
            self.upload_processing = False
            self.upload_is_guided = False
            # Trigger the normal assessment run.
            self._reset_run()
            self.thread_id = str(uuid.uuid4())
            self.phase = "assessing"
            tdb_code = self.tdb_code
            config = self._config()
            self._push_event(
                MeetingEvent(
                    kind="system",
                    speaker="system",
                    title=(
                        f"アップロードされた試算表を確認しました・TDB {tdb_code} "
                        "(Upload confirmed — starting assessment.)"
                    ),
                )
            )

        # Rebuild TrialBalance rows from the confirmed staging dicts and inject
        # them as the initial shisanhyo so the deterministic pipeline uses the
        # uploaded figures (byte-equivalent to a fixture run per the Part 6 tests).
        rows: list[_TB] = []
        for raw in serialised:
            try:
                rows.append(
                    _TB(
                        period=_dt.date.fromisoformat(str(raw["period"])),
                        uriage=int(raw["uriage"]),
                        uriage_genka=int(raw["uriage_genka"]),
                        hanbaihi=int(raw["hanbaihi"]),
                        eigai_shueki=int(raw.get("eigai_shueki", 0)),
                        eigai_hiyo=int(raw.get("eigai_hiyo", 0)),
                    )
                )
            except Exception:  # noqa: BLE001 - best-effort; skip malformed staged rows
                continue

        init_payload: dict[str, Any] = {"tdb_code": tdb_code}
        if rows:
            init_payload["shisanhyo"] = rows

        async for _ in self._drive_stream(init_payload, config, resume=False):
            yield

    @rx.var
    def upload_has_preview(self) -> bool:
        """Whether a parsed upload is staged for review (rows or warnings)."""
        return (
            len(self.upload_serialised) > 0
            or len(self.upload_warnings) > 0
            or self.upload_processing
        )

    @rx.var
    def upload_row_errors(self) -> list[str]:
        """Per-row J-GAAP validation messages for the staged (editable) rows.

        Display-only re-validation so the banker sees, live, whether an edited
        cell still satisfies the gross-profit identity (粗利 = 売上 − 売上原価)
        and stays a non-negative integer. One string per row ('' = valid). The
        UI never computes a verdict; this only surfaces whether the banker's own
        corrections are well-formed before they confirm.
        """
        errors: list[str] = []
        for raw in self.upload_serialised:
            try:
                uriage = int(raw.get("uriage", 0))
                genka = int(raw.get("uriage_genka", 0))
            except (TypeError, ValueError):
                errors.append("数値を入力してください (numeric values required)")
                continue
            if uriage < 0 or genka < 0:
                errors.append("負の値は不可 (values must be ≥ 0)")
            elif genka > uriage:
                errors.append("売上原価 > 売上 (COGS exceeds sales)")
            else:
                errors.append("")
        return errors

    @rx.var
    def upload_is_valid(self) -> bool:
        """Whether every staged row passes validation (enables Confirm)."""
        return len(self.upload_serialised) > 0 and all(e == "" for e in self.upload_row_errors)

    @rx.event
    def edit_upload_cell(self, index: int, field: str, value: str) -> None:
        """Edit one staged cell in place (banker correction before confirm).

        Updates both the serialised row (the source of truth fed to the
        pipeline) and the display row, and recomputes the displayed keijo_rieki
        preview where possible. Strictly display-side correction: the banker is
        fixing a misread figure; nothing is committed until they confirm.
        """
        if not (0 <= index < len(self.upload_serialised)):
            return
        # In guided manual entry the period is also editable (a parsed file
        # already supplies it). Period is an identity string, not a figure, so
        # it is stored verbatim on both the serialised and display rows; the
        # confirm path validates it when rebuilding the TrialBalance.
        if field == "period":
            if not self.upload_is_guided:
                return
            period = value.strip()
            serialised = [dict(r) for r in self.upload_serialised]
            serialised[index]["period"] = period
            self.upload_serialised = serialised
            rows = [dict(r) for r in self.upload_preview_rows]
            if 0 <= index < len(rows):
                rows[index]["period"] = period
                self.upload_preview_rows = rows
            return
        allowed = {"uriage", "uriage_genka", "hanbaihi"}
        if field not in allowed:
            return
        cleaned = value.replace(",", "").replace("￥", "").strip()
        try:
            parsed = int(cleaned) if cleaned else 0
        except ValueError:
            # Keep the raw text so the validation var can flag it; store as-is.
            parsed = cleaned  # type: ignore[assignment]
        serialised = [dict(r) for r in self.upload_serialised]
        serialised[index][field] = parsed
        self.upload_serialised = serialised

        # Refresh the display row for this index (best-effort formatting).
        #
        # A money cell may currently hold the raw (non-numeric) text the banker
        # just typed -- kept verbatim so ``upload_row_errors`` can flag it. The
        # PREVIOUS implementation coerced all three terms in one ``try`` and, on
        # the first non-numeric field, aborted the whole refresh, leaving a STALE
        # keijo_rieki preview next to the just-edited (invalid) cell. Coerce each
        # term independently (invalid -> 0 for DISPLAY only) so the edited cell
        # and the keijo preview always reflect the current input. The serialised
        # string is untouched, so the J-GAAP gate (upload_is_valid) still blocks
        # Confirm until the banker fixes it -- no invalid figure can reach the
        # pipeline; only the on-screen preview is kept honest.
        def _as_int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        rows = [dict(r) for r in self.upload_preview_rows]
        if 0 <= index < len(rows):
            u = _as_int(serialised[index].get("uriage", 0))
            g = _as_int(serialised[index].get("uriage_genka", 0))
            h = _as_int(serialised[index].get("hanbaihi", 0))
            rows[index]["uriage"] = format_jpy(u)
            rows[index]["uriage_genka"] = format_jpy(g)
            rows[index]["uriage_raw"] = str(u)
            rows[index]["uriage_genka_raw"] = str(g)
            rows[index]["keijo_rieki"] = format_jpy(u - g - h)
            self.upload_preview_rows = rows

    @rx.event
    def cancel_upload(self) -> None:
        """Cancel the pending upload: discard proposed rows, no state change."""
        self.upload_preview_rows = []
        self.upload_warnings = []
        self.upload_serialised = []
        self.upload_processing = False
        self.upload_is_guided = False

    # --- Input + flag setters ---

    # --- Keikakusho export (display-only: emits the draft unchanged) ---

    @rx.event
    def download_keikakusho_docx(self):
        """Download the current Keikakusho draft as an editable Word (.docx) file.

        Builds the DOCX from ``keikakusho_draft`` by copying each line's text
        verbatim into Word paragraphs (mapping only Markdown structure), so no
        figure is reformatted and the numeric-preservation invariant holds: an
        export must never add, drop, or alter a figure. DOCX is the editable
        format Japanese banks annotate before submission. No-op when there is
        no draft yet.
        """
        if not self.keikakusho_draft:
            return None
        from app.backend.export.keikakusho_docx import (
            build_keikakusho_docx,
            docx_filename,
        )

        data = build_keikakusho_docx(self.keikakusho_draft)
        filename = docx_filename(self.company_name or self.tdb_code)
        return rx.download(data=data, filename=filename)

    @rx.event
    def print_keikakusho(self):
        """Open the browser print dialog for a PDF (正式版) export.

        Triggers ``window.print()``; an ``@media print`` rule in THEME_CSS
        isolates the element tagged ``saisei-print-region`` (the rendered
        document) so the saved PDF is the plan only, with no app chrome.
        Browser print-to-PDF uses the OS fonts, so Japanese renders correctly
        with zero extra dependencies. No-op when there is no draft yet.
        """
        if not self.keikakusho_draft:
            return None
        return rx.call_script("window.print()")

    @rx.var
    def has_recovery_projection(self) -> bool:
        """Whether a Feature 5 recovery projection is available for Excel export."""
        return bool(self.recovery_serialised.get("months"))

    @rx.var
    def recovery_table_rows(self) -> list[dict[str, str]]:
        """Per-month recovery figures, pre-formatted for an accessible table.

        Feeds the screen-reader / print data table that accompanies the SVG
        recovery chart (an SVG is opaque to assistive tech, so the same
        deterministic figures are exposed as a real ``<table>``). Display-only:
        every value is the exact projection figure, formatted for reading; no
        figure is computed here. Empty when there is no projection (the table,
        like the chart, then self-hides).
        """
        rows: list[dict[str, str]] = []
        for m in self.recovery_serialised.get("months") or []:
            idx = int(m["month_index"])
            rows.append(
                {
                    "month": f"{idx}",
                    "period": str(m["period"]),
                    "ews": f"{float(m['ews_score']):.1f}",
                    "uplift": format_jpy(int(m["monthly_uplift"])),
                    "keijo": format_jpy(int(m["keijo_rieki"])),
                    "recovered": "正常" if bool(m["recovered"]) else "",
                }
            )
        return rows

    @rx.var
    def recovery_aria_label(self) -> str:
        """A one-line text summary of the recovery chart for screen readers.

        Describes the deterministic recovery story (baseline EWS, month count,
        and the 正常 crossing month when one exists) so a non-sighted user gets
        the same at-a-glance read the SVG gives a sighted one. Display-only.
        """
        cache = self.recovery_serialised
        months = cache.get("months") or []
        if not months:
            return "回復予測はありません (No recovery projection available)."
        baseline = float(cache.get("baseline_ews", 0.0))
        n = len(months)
        rec_idx = cache.get("recovery_month_index")
        if rec_idx is None:
            tail = "投影期間内に正常水準（EWS 40未満）には到達しません。"
        else:
            tail = f"{int(rec_idx)}ヶ月目に正常水準（EWS 40未満）へ回復します。"
        return (
            f"回復カーブ: ベースラインEWS {baseline:.1f} から {n}ヶ月の投影。{tail} "
            f"(Recovery curve: from baseline EWS {baseline:.1f} over {n} months.)"
        )

    @rx.event
    def download_recovery_xlsx(self):
        """Download the Feature 5 P&L recovery projection as an Excel (.xlsx) file.

        Rebuilds the deterministic ``RecoveryProjection`` from the cached
        serialisation and writes the month-by-month grid (uplift / 経常利益 /
        EWS) to a workbook. Every figure is the exact value the projection
        produced (numeric-preservation holds). Excel is where banks exchange
        the numbers, so the grid is the natural XLSX export (the prose document
        is PDF/DOCX). No-op when no projection is available.
        """
        cache = self.recovery_serialised
        if not cache.get("months"):
            return None
        import datetime as _dt

        from app.backend.analysis.pnl_recovery import (
            RecoveryMonth,
            RecoveryProjection,
        )
        from app.backend.export.recovery_xlsx import build_recovery_xlsx, xlsx_filename

        months = [
            RecoveryMonth(
                month_index=int(m["month_index"]),
                period=_dt.date.fromisoformat(str(m["period"])),
                monthly_uplift=int(m["monthly_uplift"]),
                keijo_rieki=int(m["keijo_rieki"]),
                ews_score=float(m["ews_score"]),
                recovered=bool(m["recovered"]),
            )
            for m in cache["months"]
        ]
        projection = RecoveryProjection(
            months=months,
            annual_uplift=int(cache.get("annual_uplift", 0)),
            full_monthly_uplift=int(cache.get("full_monthly_uplift", 0)),
            ramp_months=int(cache.get("ramp_months", 6)),
            recovery_month_index=cache.get("recovery_month_index"),
            baseline_ews=float(cache.get("baseline_ews", 0.0)),
        )
        data = build_recovery_xlsx(projection)
        filename = xlsx_filename(self.company_name or self.tdb_code)
        return rx.download(data=data, filename=filename)

    @rx.event
    def set_tdb_code(self, tdb_code: str) -> None:
        """Update the TDB code and reset dependent UI fields."""
        self.tdb_code = tdb_code.strip()
        self.error = ""

    # --- Feature 9: borrower workspace tabs (meta-interface, display-only) ---

    _VALID_TABS: tuple[str, ...] = ("assessment", "meeting", "plan", "audit")

    # --- Feature 8.1: Portfolio altitude (ephemeral, tenant-scoped projection) ---
    #
    # The book-level watchlist, built the GOVERNANCE-LIGHT way: it is a VIEW over
    # the borrowers ALREADY ASSESSED in this session, not a data warehouse. Each
    # finalized assessment appends a lightweight display snapshot to
    # ``portfolio_rows`` (display strings + a short EWS series for the
    # sparkline); nothing is persisted at rest. ``show_portfolio`` toggles the
    # Altitude-1 watchlist over the borrower workspace. True at-rest book
    # storage is a SEPARATE, opt-in, bank-owned decision (a future config flag,
    # mirroring the opt-in SAISEI_AUDIT_DSN) — deliberately NOT enabled here.
    #: Per-borrower watchlist snapshots captured this session (display-only).
    #: Each row: {tdb_code, company_name, ews, fsa_kanji, crossed, ews_series}
    #: where ews_series is a comma-joined string of real computed EWS figures.
    portfolio_rows: list[dict[str, str]] = []
    #: Whether the Altitude-1 Portfolio watchlist is shown (over the workspace).
    show_portfolio: bool = False
    #: Watchlist deterioration filter (display-only): which borrowers to show.
    #: One of: "all" | "crossed" (just crossed the 要注意 floor this session) |
    #: "distressed" (EWS at/above the 要注意 floor). Never changes a figure.
    portfolio_filter: str = "all"

    # --- Portfolio credit-signal roll-up (origination book; ephemeral) ---
    #: Per-facility origination credit-signal snapshots captured this session,
    #: appended by ``_apply_origination_snapshot`` when a facility is taken to
    #: the 稟議 gate. Each row: {tdb_code, company, recommendation, capacity_band,
    #: coverage_band}. The book-level twin of ``portfolio_rows`` for the two
    #: ADVISORY origination credit lenses (返済余力 / 担保・保証), so a banker sees
    #: how the freshly-originated book splits across capacity / coverage bands.
    #: Display-only, nothing persisted at rest; latest run per tdb_code wins.
    origination_book: list[dict[str, str]] = []

    @rx.var
    def effective_tab(self) -> str:
        """The tab to actually render: the banker's pick, else phase-implied.

        Lifecycle auto-focus so the workspace follows the run without the banker
        hunting: while no explicit tab is pinned this run, the meeting tab shows
        during the creditor meeting / decision, and the plan tab once a draft
        exists. Once the banker clicks any tab (``tab_pinned``) their choice
        wins. Reads ``phase`` — never writes it (no cross-thread state mutation).
        """
        if self.tab_pinned:
            return self.active_tab
        if self.phase in ("meeting", "awaiting_decision"):
            return "meeting"
        if self.phase in ("drafting", "done") and self.keikakusho_draft:
            return "plan"
        if self.phase in ("drafting", "done"):
            return "plan"
        return self.active_tab

    @rx.event
    def set_active_tab(self, tab: str):
        """Switch the active borrower tab (banker's explicit choice).

        Validates against the known tabs (ignores anything else), pins the
        choice so lifecycle auto-focus stops overriding it, and lazily loads the
        audit trail the first time the Audit tab is opened. Display-only.
        """
        if tab not in self._VALID_TABS:
            return None
        self.active_tab = tab
        self.tab_pinned = True
        if tab == "audit":
            return SaiseiUIState.load_audit_trail
        return None

    # --- Feature 9 §6: deep-linkable borrower tabs (forward-compatible routes) ---
    #
    # Phase 1 kept ``active_tab`` an enum-like string set specifically so it
    # could map 1:1 onto a route segment in Phase 2 without a rewrite. These
    # ``on_load`` handlers are that mapping, delivered as the smallest safe
    # slice: each static borrower route (/borrower/<tab>) pre-selects its tab on
    # page load by delegating to the validated ``set_active_tab`` with a literal
    # tab key. No router/query-param introspection is needed (so this is robust
    # across Reflex versions), and the URL itself is the deep link — a banker can
    # bookmark or share "this borrower's Audit tab". Display-only: it only
    # selects which already-rendered panel is shown.

    @rx.event
    def open_assessment_tab(self):
        """on_load for /borrower/assessment — select the Assessment tab."""
        return SaiseiUIState.set_active_tab("assessment")

    @rx.event
    def open_meeting_tab(self):
        """on_load for /borrower/meeting — select the Meeting tab."""
        return SaiseiUIState.set_active_tab("meeting")

    @rx.event
    def open_plan_tab(self):
        """on_load for /borrower/plan — select the Plan tab."""
        return SaiseiUIState.set_active_tab("plan")

    @rx.event
    def open_audit_tab(self):
        """on_load for /borrower/audit — select the Audit tab (loads the ledger)."""
        return SaiseiUIState.set_active_tab("audit")

    # --- Feature 8.1: Portfolio watchlist (ephemeral projection, display-only) ---

    def _capture_portfolio_snapshot(self) -> None:
        """Append/update this borrower's watchlist snapshot from current state.

        Called at finalize time, after ``_apply_snapshot`` has populated the
        display fields, so it reads ALREADY-COMPUTED values off ``self`` and
        computes no figure. The EWS series is the deterministic recovery
        projection's EWS curve when available (baseline + projected months),
        else just the single current EWS point — every plotted value is a real
        computed figure (no fabricated trend). ``crossed`` is set when this
        borrower moved from below the 要注意 floor (EWS_SUBSTANDARD) to at/above
        it versus a prior in-session snapshot of the SAME tdb_code. Best-effort
        and ephemeral: it only updates the in-memory session view, never a store.
        """
        from app.shared.constants import EWS_SUBSTANDARD

        code = self.tdb_code
        if not code:
            return
        ews = float(self.ews_score)

        # Build the EWS series from real computed figures only.
        series: list[float] = []
        cache = self.recovery_serialised
        months = cache.get("months") or []
        if months:
            series.append(float(cache.get("baseline_ews", ews)))
            series.extend(float(m.get("ews_score", 0.0)) for m in months)
        else:
            series = [ews]
        ews_series = ",".join(f"{v:.2f}" for v in series)

        # Determine "just crossed" vs any prior snapshot of the same borrower.
        threshold = float(EWS_SUBSTANDARD)
        prior = next((r for r in self.portfolio_rows if r.get("tdb_code") == code), None)
        crossed = False
        if prior is not None:
            try:
                prior_ews = float(prior.get("ews", "0"))
                crossed = prior_ews < threshold <= ews
            except (TypeError, ValueError):
                crossed = False

        row = {
            "tdb_code": code,
            "company_name": self.company_name or code,
            "ews": f"{ews:.2f}",
            "fsa_kanji": self.fsa_kanji or "—",
            "loan_status": self.loan_status_kanji,
            "crossed": "yes" if crossed else "no",
            "ews_series": ews_series,
            "updated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        # Replace any existing row for this borrower (latest assessment wins),
        # else append. Rebuilt as a new list for Reflex reactivity.
        others = [r for r in self.portfolio_rows if r.get("tdb_code") != code]
        self.portfolio_rows = [*others, row]

        # Opt-in persistence (Feature 8.1): best-effort upsert to the configured
        # store. With no SAISEI_PORTFOLIO_DSN this is the no-op NullPortfolioStore
        # (default), so nothing is stored at rest and behaviour is unchanged.
        # Persistence is the bank's explicit decision; a failure never breaks the
        # run (the in-session watchlist already has the row).
        try:
            import datetime as _dt

            from app.backend.identity import current_tenant_id
            from app.backend.portfolio.store import (
                PortfolioSnapshot,
                get_portfolio_store,
            )
            from app.shared.settings import get_settings

            settings = get_settings()
            store = get_portfolio_store(settings)
            store.upsert(
                PortfolioSnapshot(
                    tenant_id=current_tenant_id(settings),
                    tdb_code=code,
                    company_name=row["company_name"],
                    ews=ews,
                    fsa_kanji=row["fsa_kanji"],
                    ews_series=ews_series,
                    loan_status=self.loan_status_kanji,
                    updated_at=_dt.datetime.now(_dt.UTC).isoformat(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            _log.warning("ui.portfolio_persist_failed", error=str(exc))

    @rx.var
    def portfolio_ranked(self) -> list[dict[str, str]]:
        """The watchlist rows ordered worst-first via the charts toolkit ranking.

        Uses the deterministic ``rank_by_deterioration`` (just-crossed first,
        then EWS descending, ties by tdb_code) so the banker sees who needs
        attention at the top. Display order only — computes no figure. Empty
        until at least one borrower has been assessed this session.
        """
        from app.frontend.components.charts import (
            DeteriorationRow,
            rank_by_deterioration,
        )

        rows = self._filtered_portfolio_rows()
        ranked = rank_by_deterioration(
            [
                DeteriorationRow(
                    key=str(r.get("tdb_code", "")),
                    ews=float(r.get("ews", "0") or 0.0),
                    crossed=r.get("crossed") == "yes",
                )
                for r in rows
            ]
        )
        by_code = {r.get("tdb_code"): r for r in rows}
        return [by_code[d.key] for d in ranked if d.key in by_code]

    @rx.var
    def portfolio_count(self) -> int:
        """Number of borrowers assessed in this session's watchlist."""
        return len(self.portfolio_rows)

    _PORTFOLIO_FILTERS: tuple[str, ...] = ("all", "crossed", "distressed")

    def _filtered_portfolio_rows(self) -> list[dict[str, str]]:
        """Return the watchlist rows kept by the active deterioration filter.

        Pure display selection — it never edits a figure, only chooses which
        already-captured rows are shown: ``all`` keeps everything; ``crossed``
        keeps borrowers that just crossed the 要注意 floor this session;
        ``distressed`` keeps borrowers whose EWS is at/above that floor. An
        unknown filter value falls back to ``all`` (defensive).
        """
        from app.shared.constants import EWS_SUBSTANDARD

        flt = self.portfolio_filter
        if flt == "crossed":
            return [r for r in self.portfolio_rows if r.get("crossed") == "yes"]
        if flt == "distressed":
            floor = float(EWS_SUBSTANDARD)
            kept: list[dict[str, str]] = []
            for r in self.portfolio_rows:
                try:
                    if float(r.get("ews", "0") or 0.0) >= floor:
                        kept.append(r)
                except (TypeError, ValueError):
                    continue
            return kept
        return list(self.portfolio_rows)

    @rx.var
    def portfolio_filtered_count(self) -> int:
        """How many borrowers the active filter currently shows."""
        return len(self._filtered_portfolio_rows())

    @rx.event
    def set_portfolio_filter(self, value: str | list[str]) -> None:
        """Set the watchlist deterioration filter (ignores unknown values).

        Accepts ``str | list[str]`` because Reflex's ``segmented_control.root``
        ``on_change`` event spec yields that union (a single-select control still
        delivers the value as a one-element list in some paths). Normalise a list
        to its first element, then apply only a known filter value.
        """
        if isinstance(value, list):
            value = value[0] if value else ""
        if value in self._PORTFOLIO_FILTERS:
            self.portfolio_filter = value

    #: Sparkline cell geometry (logical SVG units; the <svg> scales responsively).
    _SPARK_W: int = 120
    _SPARK_H: int = 28

    @rx.var
    def portfolio_view_rows(self) -> list[dict[str, str]]:
        """Ranked watchlist rows enriched with sparkline geometry for the panel.

        Builds, per ranked borrower, the SVG sparkline ``points`` string and a
        trend sign from the row's real EWS series via the charts toolkit
        primitives, plus an EWS colour accent. Pure presentation: it maps
        already-computed figures to pixels/colour and computes no figure. The
        component stays dumb (it only renders these strings).
        """
        from app.frontend.components.charts import (
            Bounds,
            build_sparkline,
            sparkline_trend,
        )
        from app.frontend.theme import ews_color

        bounds = Bounds(x0=2.0, y0=3.0, x1=float(self._SPARK_W - 2), y1=float(self._SPARK_H - 3))
        out: list[dict[str, str]] = []
        for r in self.portfolio_ranked:
            raw = str(r.get("ews_series", ""))
            try:
                series = [float(v) for v in raw.split(",") if v != ""]
            except ValueError:
                series = []
            trend = sparkline_trend(series)
            out.append(
                {
                    **r,
                    "spark_points": build_sparkline(series, bounds),
                    "trend": ("up" if trend > 0 else "down" if trend < 0 else "flat"),
                    "ews_color": ews_color(float(r.get("ews", "0") or 0.0)),
                }
            )
        return out

    @rx.var
    def portfolio_crossed_count(self) -> int:
        """How many watchlist borrowers just crossed the 要注意 threshold."""
        return sum(1 for r in self.portfolio_rows if r.get("crossed") == "yes")

    @rx.var
    def portfolio_distribution(self) -> list[dict[str, str]]:
        """Book-level EWS-band distribution for the at-a-glance overview bar.

        Feature 9 §7 / 8.1: "where does the book sit?" — tallies this session's
        assessed borrowers into the four FSA health bands by their already-
        computed EWS score and returns each band's count + stacked-bar width via
        the deterministic ``build_band_distribution`` toolkit primitive. The
        bands use the authoritative thresholds from ``shared.constants`` (no
        magic numbers): 正常 (<40), 要注意 (40–70), 破綻懸念 (70–85), 実質破綻 (≥85).
        Display-only: it bins already-computed scores and computes no figure.
        Empty (all-zero) until at least one borrower has been assessed.
        """
        from app.frontend.components.charts import build_band_distribution
        from app.shared.constants import EWS_DANGER, EWS_DOUBTFUL, EWS_SUBSTANDARD

        # Ordered best-first so the bar reads 正常 → 実質破綻 left-to-right; the
        # accent tokens follow the shared health gradient (green → red).
        bands = [
            ("normal", "正常", "positive"),
            ("attention", "要注意", "warn"),
            ("doubtful", "破綻懸念", "chrome"),
            ("danger", "実質破綻", "fail"),
        ]
        counts: dict[str, int] = {"normal": 0, "attention": 0, "doubtful": 0, "danger": 0}
        for r in self.portfolio_rows:
            try:
                ews = float(r.get("ews", "0") or 0.0)
            except (TypeError, ValueError):
                continue
            if ews >= EWS_DANGER:
                counts["danger"] += 1
            elif ews >= EWS_DOUBTFUL:
                counts["doubtful"] += 1
            elif ews >= EWS_SUBSTANDARD:
                counts["attention"] += 1
            else:
                counts["normal"] += 1
        return [
            {
                "key": b.key,
                "label": b.label,
                "accent": b.accent,
                "count": str(b.count),
                "width_pct": f"{b.width_pct:.2f}",
            }
            for b in build_band_distribution(bands, counts)
        ]

    @rx.event
    def open_portfolio(self):
        """Show the Altitude-1 Portfolio watchlist (also the /portfolio on_load).

        Triggers a best-effort load of any persisted rows (opt-in; a no-op when
        no portfolio store is configured) so the watchlist can include borrowers
        from prior sessions when the bank has enabled persistence.
        """
        self.show_portfolio = True
        return [
            SaiseiUIState.load_persisted_portfolio,
            SaiseiUIState.load_persisted_origination_book,
        ]

    @rx.event(background=True)
    async def load_persisted_origination_book(self):
        """Merge any persisted origination-book rows into the in-session book.

        The origination twin of ``load_persisted_portfolio``. Offline-safe by
        construction: with no SAISEI_PORTFOLIO_DSN the store is the no-op
        NullOriginationBookStore, so this reads nothing and the book stays purely
        in-session (default). When the bank has enabled Portfolio persistence,
        facilities originated in prior sessions are merged in WITHOUT overwriting
        a fresher snapshot captured this session (the in-session row wins on a
        tdb_code clash). Best-effort: a load failure is logged, never fatal.
        """

        def _read() -> list[dict[str, str]]:
            from app.backend.identity import current_tenant_id
            from app.backend.portfolio.origination_store import (
                get_origination_book_store,
            )
            from app.shared.settings import get_settings

            settings = get_settings()
            store = get_origination_book_store(settings)
            snaps = store.read(current_tenant_id(settings))
            return [
                {
                    "tdb_code": s.tdb_code,
                    "company": s.company or s.tdb_code,
                    "recommendation": s.recommendation,
                    "capacity_band": s.capacity_band,
                    "coverage_band": s.coverage_band,
                }
                for s in snaps
            ]

        try:
            persisted = await asyncio.to_thread(_read)
        except Exception as exc:  # noqa: BLE001 - load is best-effort
            _log.warning("ui.origination_book_load_failed", error=str(exc))
            return

        if not persisted:
            return

        async with self:
            # In-session rows win on a tdb_code clash (they are the freshest).
            have = {r.get("tdb_code") for r in self.origination_book}
            merged = list(self.origination_book)
            merged.extend(r for r in persisted if r["tdb_code"] not in have)
            self.origination_book = merged

    @rx.event(background=True)
    async def load_persisted_portfolio(self):
        """Merge any persisted watchlist rows into the in-session view (opt-in).

        Offline-safe by construction: with no SAISEI_PORTFOLIO_DSN the store is
        the no-op NullPortfolioStore, so this reads nothing and the watchlist
        stays purely in-session (default). When the bank has enabled
        persistence, prior-session borrowers are merged in WITHOUT overwriting a
        fresher snapshot captured this session (the in-session row wins on a
        tdb_code clash). Best-effort: a load failure is logged, never fatal.
        """

        def _read() -> list[dict[str, str]]:
            from app.backend.identity import current_tenant_id
            from app.backend.portfolio.store import get_portfolio_store
            from app.shared.settings import get_settings

            settings = get_settings()
            store = get_portfolio_store(settings)
            snaps = store.read(current_tenant_id(settings))
            return [
                {
                    "tdb_code": s.tdb_code,
                    "company_name": s.company_name or s.tdb_code,
                    "ews": f"{float(s.ews):.2f}",
                    "fsa_kanji": s.fsa_kanji or "—",
                    "loan_status": s.loan_status,
                    "crossed": "no",
                    "ews_series": s.ews_series or f"{float(s.ews):.2f}",
                    "updated_at": s.updated_at or "",
                }
                for s in snaps
            ]

        try:
            persisted = await asyncio.to_thread(_read)
        except Exception as exc:  # noqa: BLE001 - load is best-effort
            _log.warning("ui.portfolio_load_failed", error=str(exc))
            return

        if not persisted:
            return

        async with self:
            # In-session rows win on a tdb_code clash (they are the freshest).
            have = {r.get("tdb_code") for r in self.portfolio_rows}
            merged = list(self.portfolio_rows)
            merged.extend(r for r in persisted if r["tdb_code"] not in have)
            self.portfolio_rows = merged

    @rx.event
    def close_portfolio(self):
        """Return from the watchlist to the borrower workspace."""
        self.show_portfolio = False

    # --- Origination (融資組成) entry handlers ---------------------------------

    @rx.var
    def origination_code_valid(self) -> bool:
        """Whether the origination input is a well-formed 7-digit TDB code."""
        return _origination_code_valid(self.origination_code)

    @rx.event
    def set_origination_code(self, code: str) -> None:
        """Update the origination applicant code and clear any prior error."""
        self.origination_code = code.strip()
        self.origination_error = ""

    @rx.event
    def set_origination_collateral(self, value: str) -> None:
        """Update the pledged-collateral (担保) input buffer (raw yen string)."""
        self.origination_collateral_input = value.strip()
        self.origination_error = ""

    @rx.event
    def set_origination_guarantee(self, value: str) -> None:
        """Update the guarantee-coverage (保証) input buffer (raw yen string)."""
        self.origination_guarantee_input = value.strip()
        self.origination_error = ""

    @staticmethod
    def _parse_yen(raw: str) -> int:
        """Parse a banker-typed yen string to a non-negative int (0 on garbage)."""
        cleaned = raw.replace(",", "").replace("￥", "").replace("¥", "").strip()
        try:
            return max(0, int(cleaned)) if cleaned else 0
        except ValueError:
            return 0

    @rx.event
    def open_origination(self):
        """Open the origination (申込) entry dialog."""
        self.show_origination = True

    @rx.event
    def close_origination(self):
        """Close the origination entry dialog (leaves any captured result)."""
        self.show_origination = False

    def _apply_origination_snapshot(self, values: dict[str, Any]) -> None:
        """Populate the origination display fields from a snapshot (display-only)."""
        view = _origination_recommendation_view(values)
        self.origination_recommendation = view["recommendation"]
        self.origination_reason = view["reason"]
        self.origination_grounded = view["grounded"]
        self.origination_max_facility = view["max_facility"]
        self.origination_capacity_band = view["capacity_band"]
        self.origination_capacity_reason = view["capacity_reason"]
        self.origination_capacity_debt_service = view["capacity_debt_service"]
        self.origination_capacity_ceiling = view["capacity_ceiling"]
        self.origination_coverage_band = view["coverage_band"]
        self.origination_coverage_reason = view["coverage_reason"]
        self.origination_coverage_covered = view["coverage_covered"]
        self.origination_coverage_uncovered = view["coverage_uncovered"]
        self.origination_loan_status = _origination_loan_status_kanji(values)
        profile = values.get("company_profile") or {}
        name = profile.get("name") if isinstance(profile, dict) else getattr(profile, "name", "")
        self.origination_company = str(name or "") or self.origination_code
        self._capture_origination_book_row(view)

    def _capture_origination_book_row(self, view: dict[str, str]) -> None:
        """Append/update this applicant's origination credit-signal book row.

        Reads the ALREADY-MAPPED recommendation view (no figure computed here)
        and records the two advisory credit-lens bands (返済余力 / 担保・保証) for
        the book-level roll-up. Skipped when no recommendation is present (an
        empty / errored run). Latest run per tdb_code wins (rebuilt as a new list
        for Reflex reactivity). Ephemeral and display-only — nothing persisted.
        """
        if not view.get("recommendation"):
            return
        code = self.origination_code or self.origination_company
        row = {
            "tdb_code": code,
            "company": self.origination_company or code,
            "recommendation": view["recommendation"],
            "capacity_band": view.get("capacity_band", ""),
            "coverage_band": view.get("coverage_band", ""),
        }
        others = [r for r in self.origination_book if r.get("tdb_code") != code]
        self.origination_book = [*others, row]

        # Opt-in persistence: best-effort upsert to the configured origination
        # book store. With no SAISEI_PORTFOLIO_DSN this is the no-op
        # NullOriginationBookStore (default), so nothing is stored at rest and
        # behaviour is unchanged. Reuses the SAME opt-in gate + tenant seam as
        # the watchlist (the origination book is part of the one Portfolio
        # persistence decision). A failure never breaks the run (the in-session
        # book already has the row), exactly like _capture_portfolio_snapshot.
        try:
            import datetime as _dt

            from app.backend.identity import current_tenant_id
            from app.backend.portfolio.origination_store import (
                OriginationBookSnapshot,
                get_origination_book_store,
            )
            from app.shared.settings import get_settings

            settings = get_settings()
            store = get_origination_book_store(settings)
            store.upsert(
                OriginationBookSnapshot(
                    tenant_id=current_tenant_id(settings),
                    tdb_code=code,
                    company=row["company"],
                    recommendation=row["recommendation"],
                    capacity_band=row["capacity_band"],
                    coverage_band=row["coverage_band"],
                    updated_at=_dt.datetime.now(_dt.UTC).isoformat(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            _log.warning("ui.origination_book_persist_failed", error=str(exc))

    @rx.var
    def origination_book_count(self) -> int:
        """Number of facilities taken to the 稟議 gate this session."""
        return len(self.origination_book)

    @rx.var
    def origination_capacity_distribution(self) -> list[dict[str, str]]:
        """Book-level debt-service-capacity (返済余力) band distribution.

        The origination twin of ``portfolio_distribution``: tallies this
        session's originated facilities into the three capacity bands by their
        already-computed band and returns each band's count + stacked-bar width
        via the deterministic ``build_band_distribution`` toolkit primitive. The
        accent follows the chip palette (within=positive / stretch=warn /
        over=fail). Display-only: it bins already-computed bands and computes no
        figure. Empty (all-zero) until at least one facility is originated. Rows
        with no capacity band (a DECLINE may omit it) are skipped.
        """
        from app.frontend.components.charts import build_band_distribution

        bands = [
            ("within_capacity", "返済余力内", "positive"),
            ("stretch", "余力上限", "warn"),
            ("over_capacity", "余力超過", "fail"),
        ]
        counts: dict[str, int] = {
            "within_capacity": 0,
            "stretch": 0,
            "over_capacity": 0,
        }
        for r in self.origination_book:
            band = r.get("capacity_band", "")
            if band in counts:
                counts[band] += 1
        return [
            {
                "key": b.key,
                "label": b.label,
                "accent": b.accent,
                "count": str(b.count),
                "width_pct": f"{b.width_pct:.2f}",
            }
            for b in build_band_distribution(bands, counts)
        ]

    @rx.var
    def origination_coverage_distribution(self) -> list[dict[str, str]]:
        """Book-level collateral/guarantee coverage (担保・保証) band distribution.

        The coverage twin of ``origination_capacity_distribution``: tallies this
        session's originated facilities into the three coverage bands
        (well_covered / partial / uncovered) and returns each band's count +
        stacked-bar width via ``build_band_distribution``. The accent follows the
        coverage chip palette (well=positive / partial=warn / uncovered=fail).
        Display-only: it bins already-computed bands and computes no figure.
        Empty until at least one facility is originated; rows with no coverage
        band are skipped.
        """
        from app.frontend.components.charts import build_band_distribution

        bands = [
            ("well_covered", "保全十分", "positive"),
            ("partial", "一部保全", "warn"),
            ("uncovered", "保全不足", "fail"),
        ]
        counts: dict[str, int] = {
            "well_covered": 0,
            "partial": 0,
            "uncovered": 0,
        }
        for r in self.origination_book:
            band = r.get("coverage_band", "")
            if band in counts:
                counts[band] += 1
        return [
            {
                "key": b.key,
                "label": b.label,
                "accent": b.accent,
                "count": str(b.count),
                "width_pct": f"{b.width_pct:.2f}",
            }
            for b in build_band_distribution(bands, counts)
        ]

    @rx.var
    def origination_book_view_rows(self) -> list[dict[str, str]]:
        """Per-facility origination book rows mapped to display strings.

        The row-level twin of ``origination_capacity_distribution`` /
        ``origination_coverage_distribution``: it maps each captured book row's
        raw bands to localized labels + accent tokens (the SAME palette the two
        distribution bars use) and the recommendation to a label + badge colour,
        so the panel table renders as a thin ``foreach`` over already-mapped
        strings. Worst-first within the book: over-capacity / uncovered rise to
        the top (a banker reads the riskiest freshly-originated facility first),
        then by company for a stable order. Display-only: it maps already-
        computed bands and computes no figure; raw bands carry the drill-in code.
        """
        cap_label = {
            "within_capacity": "返済余力内",
            "stretch": "余力上限",
            "over_capacity": "余力超過",
        }
        cap_accent = {
            "within_capacity": "positive",
            "stretch": "warn",
            "over_capacity": "fail",
        }
        cov_label = {
            "well_covered": "保全十分",
            "partial": "一部保全",
            "uncovered": "保全不足",
        }
        cov_accent = {
            "well_covered": "positive",
            "partial": "warn",
            "uncovered": "fail",
        }
        rec_label = {"approve": "承認", "decline": "謝絶"}
        rec_accent = {"approve": "positive", "decline": "fail"}
        # Worst-first ordering: rank by the more severe of the two lenses
        # (fail=2 > warn=1 > positive/unknown=0), then by company for stability.
        severity = {"fail": 2, "warn": 1, "positive": 0}

        def _rank(r: dict[str, str]) -> tuple[int, str]:
            cap = severity.get(cap_accent.get(r.get("capacity_band", ""), ""), 0)
            cov = severity.get(cov_accent.get(r.get("coverage_band", ""), ""), 0)
            return (-max(cap, cov), r.get("company", ""))

        rows: list[dict[str, str]] = []
        for r in sorted(self.origination_book, key=_rank):
            cap_band = r.get("capacity_band", "")
            cov_band = r.get("coverage_band", "")
            rec = r.get("recommendation", "")
            rows.append(
                {
                    "tdb_code": r.get("tdb_code", ""),
                    "company": r.get("company", "") or r.get("tdb_code", ""),
                    "recommendation": rec,
                    "recommendation_label": rec_label.get(rec, rec),
                    "recommendation_accent": rec_accent.get(rec, "chrome"),
                    "capacity_band": cap_band,
                    "capacity_label": cap_label.get(cap_band, "—"),
                    "capacity_accent": cap_accent.get(cap_band, "chrome"),
                    "coverage_band": cov_band,
                    "coverage_label": cov_label.get(cov_band, "—"),
                    "coverage_accent": cov_accent.get(cov_band, "chrome"),
                }
            )
        return rows

    @rx.event(background=True)
    async def start_origination(self):
        """Start a new facility application: drive the graph to the 稟議 pause.

        Runs the origination graph in-process to the credit-decision interrupt
        (off the event loop via ``asyncio.to_thread``), then surfaces the
        deterministic, grounded recommendation for the banker to act on. A new
        thread id is minted per application. Best-effort: any failure lands in
        ``origination_phase == 'error'`` rather than breaking the UI.
        """
        async with self:
            if self.origination_running or not _origination_code_valid(self.origination_code):
                return
            self.origination_running = True
            self.origination_phase = "reviewing"
            self.origination_error = ""
            self.origination_thread_id = f"orig-{uuid.uuid4()}"
            code = self.origination_code
            thread_id = self.origination_thread_id
            collateral = self._parse_yen(self.origination_collateral_input)
            guarantee = self._parse_yen(self.origination_guarantee_input)

        try:
            values = await asyncio.to_thread(
                _run_origination_to_pause,
                code,
                thread_id,
                collateral_value=collateral,
                guarantee_coverage=guarantee,
            )
        except Exception as exc:  # noqa: BLE001 - surface as an error phase
            _log.warning("ui.origination_start_failed", error=str(exc))
            async with self:
                self.origination_phase = "error"
                self.origination_error = str(exc)
                self.origination_running = False
            return

        async with self:
            self._apply_origination_snapshot(values)
            self.origination_phase = "reviewing"
            self.origination_running = False

    @rx.event(background=True)
    async def decide_origination(self, decision: str):
        """Resume the paused origination run with the banker's credit decision.

        ``decision`` is ``"approve"`` (承認 → 実行) or ``"decline"`` (謝絶).
        The banker is the only decider; this transports the decision to the
        HITL-gated graph node and refreshes the display from the resulting
        terminal snapshot. No-op when not paused at the review.
        """
        if decision not in ("approve", "decline"):
            return
        async with self:
            if self.origination_running or self.origination_phase != "reviewing":
                return
            self.origination_running = True
            thread_id = self.origination_thread_id

        try:
            values = await asyncio.to_thread(_resume_origination, thread_id, decision)
        except Exception as exc:  # noqa: BLE001 - surface as an error phase
            _log.warning("ui.origination_decide_failed", error=str(exc))
            async with self:
                self.origination_phase = "error"
                self.origination_error = str(exc)
                self.origination_running = False
            return

        async with self:
            self._apply_origination_snapshot(values)
            self.origination_phase = "approved" if decision == "approve" else "declined"
            self.origination_running = False

    # --- Servicing (貸出管理) entry handlers -----------------------------------

    @rx.var
    def servicing_loan_id_valid(self) -> bool:
        """Whether a non-empty facility id has been entered."""
        return bool(self.servicing_loan_id.strip())

    @rx.event
    def set_servicing_loan_id(self, loan_id: str) -> None:
        """Update the servicing facility id and clear any prior error."""
        self.servicing_loan_id = loan_id.strip()
        self.servicing_error = ""

    @rx.event
    def set_servicing_amount(self, amount: str) -> None:
        """Update the partial-repayment (一部入金) amount buffer."""
        self.servicing_amount_input = amount.strip()
        self.servicing_error = ""

    @rx.var
    def servicing_amount_valid(self) -> bool:
        """Whether the entered 一部入金 amount is a positive integer."""
        raw = self.servicing_amount_input.replace(",", "").strip()
        return raw.isdigit() and int(raw) > 0

    @rx.event
    def open_servicing(self):
        """Open the servicing (貸出管理) entry dialog, pre-filling the facility id.

        Pre-fills the facility id from the current run's attached loan when one
        is present (``loan_id_display``), so a banker servicing the borrower
        already on screen does not retype it. Display-only.
        """
        if self.loan_id_display and not self.servicing_loan_id:
            self.servicing_loan_id = self.loan_id_display
        self.servicing_phase = "idle"
        self.servicing_error = ""
        self.show_servicing = True

    @rx.event
    def close_servicing(self):
        """Close the servicing entry dialog (leaves any captured result)."""
        self.show_servicing = False

    @rx.event(background=True)
    async def service_facility(self, action: str):
        """Record a deterministic servicing move for the entered facility.

        ``action`` is ``"confirm"`` (実行→正常, enter normal servicing),
        ``"repay_amount"`` (一部入金, a partial paydown of ``servicing_amount_input``
        yen), or ``"repay"`` (完済, full payoff). Drives the servicing graph in
        process to completion off the event loop (the graph never pauses), then
        refreshes the facility's lifecycle status from the terminal snapshot.
        Servicing transitions are non-distress operational facts, so there is no
        banker-decision interrupt here -- the action IS the operator's record.

        A repayment draws down a principal baseline: when the dialog targets the
        on-screen facility, the current run's ``lender_stakes`` are passed so the
        paydown lands; for an arbitrarily typed facility id with no known
        baseline the graph records a no-op (honest -- it cannot guess a balance).
        Best-effort: any failure lands in ``servicing_phase == 'error'``.
        """
        if action not in ("confirm", "repay", "repay_amount"):
            return
        async with self:
            if self.servicing_running or not self.servicing_loan_id.strip():
                return
            if action == "repay_amount" and not self.servicing_amount_valid:
                self.servicing_error = (
                    "一部入金の金額を正の整数で入力してください。 "
                    "(Enter a positive 一部入金 amount.)"
                )
                return
            self.servicing_running = True
            self.servicing_error = ""
            self.servicing_thread_id = f"svc-{uuid.uuid4()}"
            loan_id = self.servicing_loan_id.strip()
            thread_id = self.servicing_thread_id
            amount = (
                int(self.servicing_amount_input.replace(",", "").strip())
                if action == "repay_amount" and self.servicing_amount_valid
                else 0
            )
            # Pass the on-screen facility's principal baseline so a repayment can
            # draw it down; only when the dialog targets that same facility.
            stakes = (
                dict(self.run_lender_stakes)
                if self.run_lender_stakes and loan_id == (self.loan_id_display or "")
                else {}
            )

        try:
            values = await asyncio.to_thread(
                _run_servicing,
                loan_id,
                action,
                thread_id,
                amount=amount,
                lender_stakes=stakes,
            )
        except Exception as exc:  # noqa: BLE001 - surface as an error phase
            _log.warning("ui.servicing_failed", error=str(exc))
            async with self:
                self.servicing_phase = "error"
                self.servicing_error = str(exc)
                self.servicing_running = False
            return

        async with self:
            self.servicing_loan_status = _origination_loan_status_kanji(values)
            self.servicing_action_taken = action
            self.servicing_phase = "done"
            self.servicing_running = False

    @rx.event
    def open_examiner(self):
        """Navigate to the Examiner (audit) altitude as a single rail action.

        The examiner surface is the read-only Feature 7 audit trail, which lives
        in the borrower workspace's Audit tab. This closes the Portfolio book
        view (if open) and selects the Audit tab, delegating to the validated
        ``set_active_tab`` (which also lazily loads the ledger). Display-only.
        """
        self.show_portfolio = False
        return SaiseiUIState.set_active_tab("audit")

    @rx.var
    def active_altitude(self) -> str:
        """Which altitude the left rail should highlight: portfolio|examiner|borrower.

        Deterministic view selector so the rail highlights exactly one item:
        the Portfolio book when it is open; else the Examiner when the borrower
        workspace is focused on the Audit tab; else the Borrower case. Reads
        display state only; computes no figure and writes nothing.
        """
        if self.show_portfolio:
            return "portfolio"
        if self.effective_tab == "audit":
            return "examiner"
        return "borrower"

    @rx.event
    def open_borrower_from_portfolio(self, tdb_code: str):
        """Drill from a watchlist row into that borrower (set code, leave list).

        Display-only navigation: it sets the TDB code and closes the watchlist so
        the banker lands on the borrower workspace. It does NOT auto-run the
        assessment (the banker presses 診断実行) — keeping the run an explicit,
        auditable human action.
        """
        self.tdb_code = str(tdb_code)
        self.show_portfolio = False

    @rx.event
    def clear_portfolio(self):
        """Clear the in-session watchlist AND any persisted rows for the tenant.

        Clears the in-memory view immediately, then best-effort clears the
        persisted book (a no-op when no store is configured). A banker-initiated
        wipe must not leave persisted rows behind, so it covers both; a failure
        to clear the store is logged but never fatal to the UI.
        """
        self.portfolio_rows = []
        self.portfolio_filter = "all"
        self.origination_book = []
        try:
            from app.backend.identity import current_tenant_id
            from app.backend.portfolio.origination_store import (
                get_origination_book_store,
            )
            from app.backend.portfolio.store import get_portfolio_store
            from app.shared.settings import get_settings

            settings = get_settings()
            tenant = current_tenant_id(settings)
            get_portfolio_store(settings).clear(tenant)
            # The origination book is part of the same Portfolio book, so a
            # banker wipe clears it too (both persisted twins, one decision).
            get_origination_book_store(settings).clear(tenant)
        except Exception as exc:  # noqa: BLE001 - clear is best-effort
            _log.warning("ui.portfolio_clear_failed", error=str(exc))

    @rx.event(background=True)
    async def load_audit_trail(self):
        """Load the immutable audit trail for the current thread (Feature 7).

        Reads the audit ledger IN-PROCESS via the configured sink (no network
        hop to our own /audit route). Offline-safe by construction: with no
        ``SAISEI_AUDIT_DSN`` the sink is the no-op NullAuditSink, so this yields
        an empty trail and an OK chain rather than an error. Display-only and
        best-effort — a load failure clears the panel and is logged, never fatal.
        """
        async with self:
            self.audit_loading = True
            thread_id = self.thread_id

        def _read() -> tuple[list[dict[str, str]], str, str]:
            from app.backend.audit.record import summarise_event
            from app.backend.audit.sink import get_audit_sink

            sink = get_audit_sink()
            events = sink.read(thread_id) if thread_id else []
            verdict = sink.verify_chain(thread_id) if thread_id else None
            rows = [
                {
                    "created_at": str(ev.created_at),
                    "event_type": str(ev.event_type.value),
                    "actor": str(ev.actor),
                    "summary": summarise_event(ev),
                }
                for ev in events
            ]
            if verdict is None:
                return rows, "", ""
            status = "ok" if verdict.ok else "broken"
            return rows, status, (verdict.reason or "")

        try:
            rows, status, reason = await asyncio.to_thread(_read)
        except Exception as exc:  # noqa: BLE001 - audit panel is best-effort
            _log.warning("ui.audit_load_failed", error=str(exc))
            async with self:
                self.audit_rows = []
                self.audit_chain_status = ""
                self.audit_chain_reason = ""
                self.audit_loading = False
            return

        async with self:
            self.audit_rows = rows
            self.audit_chain_status = status
            self.audit_chain_reason = reason
            self.audit_loading = False

        # The Audit tab is the examiner surface; load this facility's durable
        # loan-event ledger alongside the hash-chained audit trail (both are
        # in-process, offline-safe reads). Separate read so an audit-sink issue
        # never hides the loan ledger and vice versa.
        await self._load_loan_ledger()

    async def _load_loan_ledger(self) -> None:
        """Load this facility's durable loan-event ledger for the Audit tab.

        Reads the loan store IN-PROCESS via the configured factory (no network
        hop). Offline-safe by construction: with no ``SAISEI_LOAN_DSN`` the store
        is the no-op NullLoanStore, so this yields an empty ledger rather than an
        error. A no-op when no facility is attached to the run
        (``loan_id_display`` is ""). Display-only and best-effort -- a load
        failure clears the panel and is logged, never fatal.
        """
        async with self:
            self.loan_ledger_loading = True
            loan_id = self.loan_id_display

        try:
            ledger = await asyncio.to_thread(_loan_ledger_rows, loan_id)
        except Exception as exc:  # noqa: BLE001 - loan ledger panel is best-effort
            _log.warning("ui.loan_ledger_load_failed", error=str(exc))
            async with self:
                self.loan_ledger_rows = []
                self.loan_ledger_loading = False
            return

        async with self:
            self.loan_ledger_rows = ledger
            self.loan_ledger_loading = False

    @rx.event
    def set_revision_note_buffer(self, note: str) -> None:
        """Update the banker's revision note buffer."""
        self.revision_note_buffer = note
        if self.error:
            self.error = ""

    @rx.event
    def toggle_yakuin_hoshu_cut(self, value: bool) -> None:
        """Banker toggles the executive-compensation-cut commitment flag."""
        self.yakuin_hoshu_cut = value

    @rx.event
    def toggle_personal_asset_disposal(self, value: bool) -> None:
        """Banker toggles the personal-asset-disposal commitment flag."""
        self.personal_asset_disposal = value

    # --- HITL decision events (delegate to the streamed resume) ---

    @rx.event
    def approve(self, index: int):
        """Approve a proposed strategy by index and resume the graph."""
        return SaiseiUIState.resume_streamed(
            {
                "decision": "approve",
                "strategy_index": index,
                "yakuin_hoshu_cut": self.yakuin_hoshu_cut,
                "personal_asset_disposal": self.personal_asset_disposal,
            }
        )

    @rx.event
    def revise(self):
        """Request a revision with the buffered banker note and resume.

        Carries the commitment flags too: a ``needs_human`` rejection is
        cleared by the banker toggling the flags and re-submitting, so the
        revise payload must persist them or the deadlock would never break.
        """
        return SaiseiUIState.resume_streamed(
            {
                "decision": "revise",
                "revision_note": self.revision_note_buffer,
                "yakuin_hoshu_cut": self.yakuin_hoshu_cut,
                "personal_asset_disposal": self.personal_asset_disposal,
            }
        )

    @rx.event
    def reject(self):
        """Reject all strategies and resume the graph to escalation."""
        return SaiseiUIState.resume_streamed(
            {
                "decision": "reject",
                "yakuin_hoshu_cut": self.yakuin_hoshu_cut,
                "personal_asset_disposal": self.personal_asset_disposal,
            }
        )


#: Chair (lead arranger) bubble titles keyed by negotiation_status.
_CHAIR_TITLES: dict[str, str] = {
    "approved": "【承認】全評価者がPASSしました (Approved by all critics)",
    "rejected": "【差し戻し】修正が必要です (Revision required)",
    "needs_human": "【担当者確認】コミットメントが必要です (Needs your confirmation)",
    "pending": "取りまとめ中 (Consolidating…)",
}

#: Banker bubble titles keyed by decision.
_BANKER_TITLES: dict[str, str] = {
    "approve": "承認しました (Approved)",
    "revise": "修正を依頼しました (Requested revision)",
    "reject": "却下しました (Rejected)",
}
