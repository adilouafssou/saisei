"""Sub-bank (syndicate lender) critic node.

Persona: Regional Syndicate Lender (協調融資銀行 / Kyocho Yushi Ginko).
Priority: P2 — Fairness.

DETERMINISTIC FAIL gate condition (rule-based, no LLM):
Grace period / loss-absorption must be proportional to the Main Bank's stake
(pro-rata / zandaka pro-rata, Anbun-shugi / 按分主義) within a defined
tolerance.

Pro-rata share computation — two modes (consistent with lead_arranger.py):

Mode A — Stake-based (preferred, when ``state.lender_stakes`` is populated):
  ``main_bank_share = lender_stakes["main_bank"] / total_stakes``
  This uses the actual outstanding loan balances supplied by the banker/HITL
  and is a true stake-based pro-rata check.

Mode B — Heuristic proxy (fallback, when ``state.lender_stakes`` is empty):
  ``main_bank_share = max(strategy_uplifts) / sum(strategy_uplifts)``
  IMPORTANT LIMITATION: this is NOT a true stake-based pro-rata.  It is a
  heuristic that uses the largest single strategy's share of total expected
  uplift as a proxy for the main bank's relative burden.  The proxy is weak
  because (a) it conflates strategy size with lender stake, and (b) it is
  insensitive to the actual loan balances.  It is retained as a fallback only
  when real stake data is unavailable.  Populate ``state.lender_stakes`` to
  enable the accurate stake-based check.

Both modes use the same tolerance band and blocker messages so the gate
behaviour is consistent regardless of which mode is active.

This is a deterministic rule; no LLM involvement.
"""

from __future__ import annotations

from typing import Any

from app.backend.nodes.critics._persona import simulate_persona_argument
from app.backend.state import CriticFeedback, SaiseiState
from app.shared.constants import PRO_RATA_TOLERANCE as _PRO_RATA_TOLERANCE
from app.shared.logging import get_logger
from app.shared.settings import Settings

__all__ = ["sub_bank_critic_node", "compute_main_bank_share"]

_log = get_logger(__name__)

_PERSONA = "sub_bank"
_PRIORITY = "P2"
_PROMPT = "critic_sub_bank"

#: Pro-rata deviation tolerance (fraction of total stake/uplift).
#: Single source of truth: app.shared.constants.PRO_RATA_TOLERANCE.


def compute_main_bank_share(state: SaiseiState) -> tuple[float, str]:
    """Compute the main bank's share of total burden and the mode used.

    Returns:
        A ``(share, mode)`` tuple where ``share`` is in [0, 1] and ``mode``
        is ``"stake_based"`` or ``"heuristic_proxy"``.
    """
    stakes = state.lender_stakes
    if stakes and "main_bank" in stakes and "sub_bank" in stakes:
        total_stakes = stakes["main_bank"] + stakes["sub_bank"]
        if total_stakes > 0:
            return stakes["main_bank"] / total_stakes, "stake_based"

    # Heuristic proxy: largest strategy uplift / total uplift.
    # See module docstring for the documented limitation of this proxy.
    strategies = state.proposed_strategies
    uplifts = [int(s.expected_keijo_uplift) for s in strategies]
    total_uplift = sum(uplifts)
    if total_uplift == 0:
        return 0.5, "heuristic_proxy"  # no burden → treat as balanced
    max_uplift = max(uplifts)
    return max_uplift / total_uplift, "heuristic_proxy"


def sub_bank_critic_node(state: SaiseiState, settings: Settings | None = None) -> dict[str, Any]:
    """Evaluate the Keikakusho from the syndicate lender's perspective.

    DETERMINISTIC FAIL gate:
    - Burden-sharing is not proportional (pro-rata deviation > tolerance).

    Uses stake-based pro-rata when ``state.lender_stakes`` is populated;
    falls back to the heuristic uplift proxy otherwise (see module docstring).

    Args:
        state: Current graph state (reads proposed_strategies, lender_stakes).

    Returns:
        Partial state update appending one :class:`CriticFeedback` dict.
    """
    blockers: list[str] = []

    strategies = state.proposed_strategies
    if not strategies:
        blockers.append(
            "no_strategies: 改善施策が提案されていません。協調融資銀行は負担分担を評価できません。"
        )
        status = "FAIL"
        rationale = "協調融資銀行評価: 提案された改善施策がありません。"
        feedback = CriticFeedback(
            persona=_PERSONA,
            status=status,
            fatal_blockers=blockers,
            priority=_PRIORITY,
            rationale=rationale,
            simulated_argument=simulate_persona_argument(
                _PROMPT,
                _PERSONA,
                status,
                blockers,
                rationale,
                settings=settings,
                state=state,
            ),
        )
        _log.info("critic.sub_bank", status=status, blockers=len(blockers))
        return {"critic_feedbacks": [feedback.model_dump()]}

    # Check for zero total uplift (heuristic proxy path only).
    uplifts = [int(s.expected_keijo_uplift) for s in strategies]
    total_uplift = sum(uplifts)
    stakes = state.lender_stakes
    stake_based = bool(stakes and "main_bank" in stakes and "sub_bank" in stakes)

    if not stake_based and total_uplift == 0:
        # No uplift and no stake data — cannot assess pro-rata; treat as PASS.
        status = "PASS"
        rationale = "協調融資銀行評価: 期待改善額がゼロのため、負担分担の評価を見送りました。"
        feedback = CriticFeedback(
            persona=_PERSONA,
            status=status,
            fatal_blockers=[],
            priority=_PRIORITY,
            rationale=rationale,
            simulated_argument=simulate_persona_argument(
                _PROMPT,
                _PERSONA,
                status,
                [],
                rationale,
                settings=settings,
                state=state,
            ),
        )
        _log.info("critic.sub_bank", status=status, blockers=0)
        return {"critic_feedbacks": [feedback.model_dump()]}

    main_bank_share, mode = compute_main_bank_share(state)

    lower_bound = 0.5 - _PRO_RATA_TOLERANCE
    upper_bound = 0.5 + _PRO_RATA_TOLERANCE

    mode_label = "実残高ベース" if mode == "stake_based" else "推定ベース"

    if main_bank_share > upper_bound:
        deviation_pct = round((main_bank_share - 0.5) * 100, 1)
        blockers.append(
            f"pro_rata_deviation: 主幹事銀行の負担比率（{main_bank_share:.1%}、{mode_label}）が"
            f"許容範囲（50% ± {_PRO_RATA_TOLERANCE:.0%}）を超えています。"
            f"偏差: {deviation_pct:.1f}%。"
            "協調融資銀行との負担分担を均等化してください（按分主義 / Anbun-shugi）。"
        )
    elif main_bank_share < lower_bound:
        deviation_pct = round((0.5 - main_bank_share) * 100, 1)
        blockers.append(
            f"pro_rata_deviation: 協調融資銀行の負担比率が過大です"
            f"（主幹事銀行比率: {main_bank_share:.1%}、{mode_label}）。"
            f"偏差: {deviation_pct:.1f}%。"
            "主幹事銀行の負担を増やし、協調融資銀行の負担を軽減してください。"
        )

    status = "FAIL" if blockers else "PASS"
    rationale = f"協調融資銀行評価（主幹事銀行負担比率: {main_bank_share:.1%}、{mode_label}）: " + (
        "負担分担の均等化が必要です。下記をご確認ください。"
        if blockers
        else "負担分担は許容範囲内です。"
    )

    feedback = CriticFeedback(
        persona=_PERSONA,
        status=status,
        fatal_blockers=blockers,
        priority=_PRIORITY,
        rationale=rationale,
        simulated_argument=simulate_persona_argument(
            _PROMPT,
            _PERSONA,
            status,
            blockers,
            rationale,
            settings=settings,
            state=state,
        ),
    )

    _log.info(
        "critic.sub_bank",
        status=status,
        main_bank_share=main_bank_share,
        mode=mode,
        blockers=len(blockers),
    )

    return {"critic_feedbacks": [feedback.model_dump()]}
