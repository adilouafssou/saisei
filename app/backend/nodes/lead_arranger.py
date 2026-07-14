"""Lead Arranger consensus engine (Torimatome / 取りまとめ).

Reads accumulated critic_feedbacks from the parallel fan-out; consolidates
PASS/FAIL verdicts with P0/P1/P2 priority hierarchy; produces a revision
directive and a deterministic per-lender burden-sharing table.

Logic:
- ANY FAIL → negotiation_status = 'rejected'.
- ALL PASS → negotiation_status = 'approved'.
- Priority hierarchy for blocker ordering:
  * P0 (guarantor / compliance) — highest priority.
  * P1 (main_bank / accountability).
  * P2 (sub_bank / fairness).
- Revision directive: ordered list of fatal blockers by priority.
- Burden-sharing table: deterministic per-lender allocation derived from
  strategy uplifts and working-capital gap.

The LLM may only phrase the prose of the final report; it never decides
a verdict or produces a figure.

Pro-rata share computation — two modes (consistent with sub_bank.py):

Mode A — Stake-based (preferred, when ``state.lender_stakes`` is populated):
  ``main_bank_share = lender_stakes["main_bank"] / total_stakes``
  This uses the actual outstanding loan balances supplied by the banker/HITL.

Mode B — Heuristic proxy (fallback, when ``state.lender_stakes`` is empty):
  ``main_bank_share = max(strategy_uplifts) / sum(strategy_uplifts)``
  IMPORTANT LIMITATION: this is NOT a true stake-based pro-rata.  It is a
  heuristic that uses the largest single strategy's share of total expected
  uplift as a proxy for the main bank's relative burden.  The proxy is weak
  because (a) it conflates strategy size with lender stake, and (b) it is
  insensitive to the actual loan balances.  It is retained as a fallback only
  when real stake data is unavailable.  Populate ``state.lender_stakes`` to
  enable the accurate stake-based check.

Both sub_bank.py and lead_arranger.py use the same ``compute_main_bank_share``
helper (imported from sub_bank) so the two modules stay consistent.
"""

from __future__ import annotations

from typing import Any

from app.backend.nodes.critics.sub_bank import compute_main_bank_share
from app.backend.state import SaiseiState
from app.shared.logging import get_logger

__all__ = [
    "lead_arranger_node",
    "compute_burden_sharing_table",
    "is_banker_only_blocker",
]

_log = get_logger(__name__)

#: Priority ordering for blocker consolidation.
_PRIORITY_ORDER: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}

#: Blocker codes that ONLY a banker can clear via the HITL interrupt (by setting
#: the commitment flags).  The strategist cannot resolve these by re-proposing
#: strategies, so looping back to the strategist would burn revision cycles and
#: escalate to END before the banker is ever consulted.  When a rejection's
#: fatal blockers are ALL banker-only, lead_arranger routes to HITL instead.
_BANKER_ONLY_BLOCKER_CODES: tuple[str, ...] = (
    "yakuin_hoshu_not_cut",
    "no_asset_disposal",
)


def is_banker_only_blocker(blocker: str) -> bool:
    """Return whether a fatal blocker can only be cleared by the banker (HITL).

    Banker-only blockers correspond to commitment flags (``yakuin_hoshu_cut``,
    ``personal_asset_disposal``) that the strategist cannot set; only the human
    can, during the HITL interrupt.

    Args:
        blocker: A fatal-blocker string (may carry a ``[priority/persona]``
            prefix added during consolidation).

    Returns:
        True if the blocker is banker-only.
    """
    return any(code in blocker for code in _BANKER_ONLY_BLOCKER_CODES)


def compute_burden_sharing_table(state: SaiseiState) -> list[dict[str, Any]]:
    """Compute a deterministic per-lender burden-sharing table.

    Derives per-lender grace period, haircut %, new-money ask, and pro-rata
    vs main-bank-heavy allocation.

    Share computation uses the same logic as sub_bank.py (via
    ``compute_main_bank_share``):
    - Stake-based when ``state.lender_stakes`` is populated (preferred).
    - Heuristic proxy (largest strategy uplift / total uplift) otherwise.
      See module docstring for the documented limitation of the proxy.

    The table has three rows: main_bank, sub_bank, guarantor.
    All figures are deterministic; no LLM involvement.

    Args:
        state: Current graph state (reads proposed_strategies,
               working_capital_gap, lender_stakes).

    Returns:
        List of per-lender burden-sharing dicts.
    """
    gap = state.working_capital_gap or 0

    # Use the shared helper so sub_bank and lead_arranger stay consistent.
    main_bank_share, mode = compute_main_bank_share(state)

    # Fallback to a balanced 50/50 only when the share is NOT derived from real
    # outstanding loan balances AND there are no strategies to base the heuristic
    # proxy on. When stake data is present (mode == "stake_based") the share is a
    # true pro-rata from the banker-supplied balances and must be kept even with
    # no proposed strategies — overwriting it here previously discarded real
    # stake data and mislabelled the allocation. (The heuristic path already
    # returns 0.5 for zero/absent uplift, so this only guards the no-stake case.)
    if mode != "stake_based" and not state.proposed_strategies:
        main_bank_share = 0.5

    sub_bank_share = 1.0 - main_bank_share

    # Grace period: proportional to share (main bank gets longer grace).
    # Base: 12 months; main bank up to 18 months, sub bank up to 12 months.
    main_grace_months = round(12 + 6 * main_bank_share)
    sub_grace_months = round(12 * sub_bank_share * 2)
    sub_grace_months = max(6, min(sub_grace_months, 12))

    # Haircut %: main bank absorbs more (accountability role).
    main_haircut_pct = round(main_bank_share * 30, 1)  # up to 30%
    sub_haircut_pct = round(sub_bank_share * 15, 1)  # up to 15%

    # New-money ask: proportional to gap (negative gap = deficit).
    # Allocate the integer total so the per-lender shares ALWAYS sum back to it:
    # round the main-bank share, then derive the sub-bank share as the exact
    # remainder. Two independent round() calls can drift by ¥1 on a .5-yen
    # boundary (e.g. total=5, share=0.5 -> 2 + 2 = 4 != 5), which would leave a
    # regulated burden table whose columns do not reconcile to the stated
    # deficit. Mirrors the "largest segment absorbs the remainder" discipline in
    # app.frontend.components.charts.build_band_distribution.
    new_money_total = abs(gap) if gap < 0 else 0
    main_new_money = round(new_money_total * main_bank_share)
    sub_new_money = new_money_total - main_new_money

    return [
        {
            "lender": "main_bank",
            "persona": "主幹事銀行（Lead Bank）",
            "share_pct": round(main_bank_share * 100, 1),
            "grace_period_months": main_grace_months,
            "haircut_pct": main_haircut_pct,
            "new_money_jpy": main_new_money,
            "allocation_type": "main_bank_heavy" if main_bank_share > 0.5 else "pro_rata",
            # Provenance of the share figure: which mode produced it, so the
            # banker knows whether the pro-rata split rests on real outstanding
            # loan balances (stake_based) or the weak uplift proxy.
            "share_basis": mode,
        },
        {
            "lender": "sub_bank",
            "persona": "協調融資銀行（Syndicate Lender）",
            "share_pct": round(sub_bank_share * 100, 1),
            "grace_period_months": sub_grace_months,
            "haircut_pct": sub_haircut_pct,
            "new_money_jpy": sub_new_money,
            "allocation_type": "pro_rata",
            "share_basis": mode,
        },
        {
            "lender": "guarantor",
            "persona": "信用保証協会（Credit Guarantee Corp）",
            "share_pct": 0.0,
            "grace_period_months": 0,
            "haircut_pct": 0.0,
            "new_money_jpy": 0,
            "allocation_type": "guarantee_only",
            "note": "保証履行リスクを負担（Guarantee execution risk）",
            "share_basis": mode,
        },
    ]


def lead_arranger_node(state: SaiseiState) -> dict[str, Any]:
    """Consolidate critic feedbacks and produce the negotiation outcome.

    Reads ``critic_feedbacks`` accumulated by the parallel fan-out.
    ANY FAIL → rejected; ALL PASS → approved.
    Consolidates fatal blockers by P0 > P1 > P2 priority.
    Computes deterministic burden-sharing table.

    Args:
        state: Current graph state (reads critic_feedbacks, proposed_strategies).

    Returns:
        Partial state update with ``negotiation_status``, ``revision_directive``,
        and ``revision_count`` (incremented on rejection).
    """
    feedbacks = state.critic_feedbacks

    if not feedbacks:
        _log.warning("lead_arranger.no_feedbacks")
        return {
            "negotiation_status": "rejected",
            "revision_directive": "致命的エラー: 評価フィードバックがありません。",
            "revision_count": state.revision_count + 1,
            "meeting_briefing": (
                "【債権者会議リハーサル / Creditor-Meeting Briefing】\n"
                "評価フィードバックがないため、リハーサルを生成できません。"
            ),
        }

    # Sort feedbacks by priority (P0 first).
    sorted_feedbacks = sorted(
        feedbacks,
        key=lambda f: _PRIORITY_ORDER.get(str(f.get("priority", "P2")), 2),
    )

    # Collect all fatal blockers in priority order.
    all_blockers: list[str] = []
    any_fail = False
    for fb in sorted_feedbacks:
        if str(fb.get("status", "PASS")) == "FAIL":
            any_fail = True
            priority = fb.get("priority", "P2")
            persona = fb.get("persona", "unknown")
            for blocker in fb.get("fatal_blockers", []):
                all_blockers.append(f"[{priority}/{persona}] {blocker}")

    # Compute burden-sharing table (always, for transparency).
    burden_sharing = compute_burden_sharing_table(state)

    # Assemble the advisory creditor-meeting briefing (rehearsal for the banker).
    # Deterministic skeleton; persona arguments / feasibility notes are advisory
    # and may be empty offline. NEVER feeds any gate, route, or figure.
    meeting_briefing = _format_meeting_briefing(
        sorted_feedbacks, state.feasibility_notes, burden_sharing
    )

    if any_fail:
        # Determine whether the rejection is escalatable to the banker.
        # If EVERY fatal blocker is banker-only (commitment flags the strategist
        # cannot set), route to HITL instead of looping the strategist and
        # burning revision cycles toward a premature escalation to END.
        all_banker_only = bool(all_blockers) and all(
            is_banker_only_blocker(b) for b in all_blockers
        )

        if all_banker_only:
            negotiation_status = "needs_human"
            revision_directive = (
                "\u3010\u62c5\u5f53\u8005\u78ba\u8a8d\u4f9d\u983c / "
                "Needs Human Confirmation\u3011\n"
                "\u4ee5\u4e0b\u306f\u62c5\u5f53\u8005\uff08HITL\uff09\u306e\u30b3\u30df\u30c3\u30c8"
                "\u30e1\u30f3\u30c8\u304c\u5fc5\u8981\u3067\u3059\u3002\n"
                + "\n".join(f"{i + 1}. {b}" for i, b in enumerate(all_blockers))
                + "\n\n\u3010\u8ca0\u62c5\u5206\u62c5\u8868 / Burden-Sharing Table\u3011\n"
                + _format_burden_table(burden_sharing)
            )
            _log.info(
                "lead_arranger.needs_human",
                blockers=len(all_blockers),
                revision_count=state.revision_count,
            )
            # Do NOT increment revision_count: this is a handoff, not a revision.
            return {
                "negotiation_status": negotiation_status,
                "revision_directive": revision_directive,
                "revision_count": state.revision_count,
                "meeting_briefing": meeting_briefing,
            }

        negotiation_status = "rejected"
        revision_directive = (
            "【修正指示 / Revision Directive】\n"
            + "\n".join(f"{i + 1}. {b}" for i, b in enumerate(all_blockers))
            + "\n\n【負担分担表 / Burden-Sharing Table】\n"
            + _format_burden_table(burden_sharing)
        )
        new_revision_count = state.revision_count + 1
        _log.info(
            "lead_arranger.rejected",
            blockers=len(all_blockers),
            revision_count=new_revision_count,
        )
        return {
            "negotiation_status": negotiation_status,
            "revision_directive": revision_directive,
            "revision_count": new_revision_count,
            "meeting_briefing": meeting_briefing,
        }
    else:
        negotiation_status = "approved"
        revision_directive = (
            "【承認 / Approved】全評価者がPASSしました。\n\n"
            "【負担分担表 / Burden-Sharing Table】\n" + _format_burden_table(burden_sharing)
        )
        _log.info("lead_arranger.approved")
        return {
            "negotiation_status": negotiation_status,
            "revision_directive": revision_directive,
            "revision_count": state.revision_count,
            "meeting_briefing": meeting_briefing,
        }


def _format_burden_table(rows: list[dict[str, Any]]) -> str:
    """Format the burden-sharing table as a Markdown table string."""
    lines = [
        "| 貸出人 | 負担比率 | 猶予期間 | ヘアカット | 新規融資 | 配分方式 |",
        "|--------|----------|----------|------------|----------|----------|",
    ]
    for row in rows:
        note = f" ({row['note']})" if "note" in row else ""
        lines.append(
            f"| {row['persona']} "
            f"| {row['share_pct']}% "
            f"| {row['grace_period_months']}ヶ月 "
            f"| {row['haircut_pct']}% "
            f"| ¥{row['new_money_jpy']:,} "
            f"| {row['allocation_type']}{note} |"
        )
    return "\n".join(lines)


#: Display labels for each critic persona in the meeting briefing.
_PERSONA_LABELS: dict[str, str] = {
    "guarantor": "信用保証協会（Credit Guarantee Corp / P0）",
    "main_bank": "主幹事銀行（Lead Bank / P1）",
    "sub_bank": "協調融資銀行（Syndicate Lender / P2）",
}

#: Banker-facing labels for the deterministic uplift-credibility bands (depth
#: step 4). Display only — the band itself is computed deterministically in
#: app.backend.analysis.uplift_grounding and feeds no gate, route, or figure.
_UPLIFT_CREDIBILITY_LABELS: dict[str, str] = {
    "grounded": "根拠あり（grounded）",
    "stretch": "野心的・要根拠補強（stretch）",
    "implausible": "非現実的・過大計上（implausible）",
}


def _format_meeting_briefing(
    sorted_feedbacks: list[dict[str, Any]],
    feasibility_notes: list[dict[str, Any]],
    burden_sharing: list[dict[str, Any]],
) -> str:
    """Assemble the advisory creditor-meeting rehearsal briefing.

    ADVISORY ONLY. Deterministic structure built from the consolidated verdicts;
    each persona's ``simulated_argument`` and the ``feasibility_notes`` are
    advisory and may be empty offline (the briefing then degrades gracefully to
    a deterministic skeleton). This text is never read by any gate, route, or
    figure — it is the banker's pre-HITL preparation only.

    Args:
        sorted_feedbacks: Critic feedback dicts, already priority-sorted.
        feasibility_notes: Advisory per-strategy feasibility note dicts.
        burden_sharing: The deterministic burden-sharing table rows.

    Returns:
        A Markdown briefing string.
    """
    lines: list[str] = ["【債権者会議リハーサル / Creditor-Meeting Briefing】"]

    # Section 1: each persona's stance (deterministic verdict + advisory argument).
    lines.append("\n■ 各債権者のスタンス / Creditor stances")
    for fb in sorted_feedbacks:
        persona = str(fb.get("persona", "unknown"))
        label = _PERSONA_LABELS.get(persona, persona)
        status = str(fb.get("status", "PASS"))
        lines.append(f"\n• {label} — {status}")
        rationale = str(fb.get("rationale", "")).strip()
        if rationale:
            lines.append(f"  根拠: {rationale}")
        argument = str(fb.get("simulated_argument", "")).strip()
        if argument:
            lines.append(f"  主張: {argument}")

    # Section 2: feasibility notes (advisory, may be empty).
    if feasibility_notes:
        lines.append("\n■ 施策の実現可能性 / Strategy feasibility")
        for note in feasibility_notes:
            title = str(note.get("strategy_title", ""))
            band = str(note.get("achievability", ""))
            lines.append(f"\n• {title} — 実現可能性: {band}")
            advisory = str(note.get("advisory", "")).strip()
            if advisory:
                lines.append(f"  所見: {advisory}")
            # Depth step 4: surface the deterministic uplift-credibility verdict
            # (claimed uplift vs the firm's OWN self-derived headroom) IN the
            # rehearsal prose, so an over-claimed uplift is read by the banker
            # here -- not just in the structured payload. A stretch/implausible
            # band is the line a creditor will press on. Advisory only; rendered
            # only when assessed (empty band -> omitted, so no-history briefings
            # stay byte-identical).
            credibility = str(note.get("uplift_credibility", "")).strip()
            if credibility:
                label = _UPLIFT_CREDIBILITY_LABELS.get(credibility, credibility)
                ratio = note.get("uplift_credibility_ratio")
                multiple = (
                    f"（実現上限の {ratio:.1f}倍）"
                    if isinstance(ratio, int | float) and ratio > 1.0
                    else ""
                )
                lines.append(f"  上乗せ妥当性: {label}{multiple}")
            # Depth step 4 part 3: surface a cross-signal CONTRADICTION (the
            # realism flag) in the rehearsal prose. Only the contradiction cases
            # (optimistic_uplift / pessimistic_uplift) are shown -- a 'consistent'
            # verdict and an unassessed note add no line, keeping the briefing
            # scannable and such runs byte-identical. Advisory only.
            realism_flag = str(note.get("realism_flag", "")).strip()
            if realism_flag and realism_flag != "consistent":
                realism_note = str(note.get("realism_note", "")).strip()
                if realism_note:
                    lines.append(f"  ❗ 整合性: {realism_note}")

    # Section 3: burden-sharing table (deterministic).
    lines.append("\n■ 負担分担表 / Burden-Sharing Table")
    lines.append(_format_burden_table(burden_sharing))

    return "\n".join(lines)
