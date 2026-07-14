"""Credit Guarantee Corporation (Shinyo Hosho Kyokai) critic node.

Persona: Credit Guarantee Corp (信用保証協会 / Shinyo Hosho Kyokai).
Priority: P0 — Compliance (highest priority).

DETERMINISTIC FAIL gate condition (rule-based, no LLM):
The plan must provide a credible 3-5 year path to profitability and Normal
(正常先) FSA classification, aligned with FSA guidelines.

Deterministic checks:
1. Recovery horizon: total expected annual uplift must be sufficient to
   eliminate the current ordinary-profit deficit within 5 years.
   - Latest keijo_rieki (monthly) × 12 = annual ordinary profit.
   - If annual_keijo_rieki < 0: deficit = abs(annual_keijo_rieki).
   - Total annual uplift from strategies must cover deficit within 5 years.
   - Condition: total_uplift >= deficit / 5 (i.e., covers 1/5 of deficit per year).
   - If annual_keijo_rieki >= 0: no deficit → this gate passes automatically.

2. FSA classification path: current classification must not be HATAN_KENENSAKI
   with zero strategies (no plan = no path to Normal).

3. EWS trajectory: EWS score must be below the doubtful threshold (70) OR
   the plan must include at least 2 strategies (showing a credible multi-lever
   approach).
"""

from __future__ import annotations

from typing import Any

from app.backend.nodes.critics._persona import simulate_persona_argument
from app.backend.state import CriticFeedback, SaiseiState
from app.shared.constants import EWS_DOUBTFUL as _EWS_DOUBTFUL
from app.shared.constants import MIN_RECOVERY_HORIZON_YEARS as _RECOVERY_HORIZON_YEARS
from app.shared.logging import get_logger
from app.shared.models.classification import FsaClass
from app.shared.settings import Settings

__all__ = ["guarantor_critic_node"]

_log = get_logger(__name__)

_PERSONA = "guarantor"
_PRIORITY = "P0"
_PROMPT = "critic_guarantor"

#: Minimum strategies for a doubtful borrower.
_MIN_STRATEGIES_DOUBTFUL: int = 2


def guarantor_critic_node(state: SaiseiState, settings: Settings | None = None) -> dict[str, Any]:
    """Evaluate the Keikakusho from the guarantor's compliance perspective.

    DETERMINISTIC FAIL gate:
    - No 3-5yr path to profitability / Normal classification.
    - EWS >= 70 with fewer than 2 strategies.

    Args:
        state: Current graph state (reads shisanhyo, proposed_strategies,
               fsa_classification, ews_score).

    Returns:
        Partial state update appending one :class:`CriticFeedback` dict.
    """
    blockers: list[str] = []
    strategies = state.proposed_strategies

    # ------------------------------------------------------------------
    # Gate 1: Recovery horizon (3-5 year path to profitability)
    # ------------------------------------------------------------------
    if state.shisanhyo:
        latest = state.shisanhyo[-1]
        annual_keijo_rieki = latest.keijo_rieki * 12
        total_annual_uplift = sum(int(s.expected_keijo_uplift) for s in strategies)

        if annual_keijo_rieki < 0:
            annual_deficit = abs(annual_keijo_rieki)
            # Required: uplift covers at least 1/N of deficit per year,
            # where N = MIN_RECOVERY_HORIZON_YEARS (FSA guideline: 3-5 years).
            required_annual_uplift = annual_deficit / _RECOVERY_HORIZON_YEARS
            if total_annual_uplift < required_annual_uplift:
                years_to_recovery = (
                    annual_deficit / total_annual_uplift
                    if total_annual_uplift > 0
                    else float("inf")
                )
                blockers.append(
                    f"recovery_horizon_exceeded: 現在の年間経常利益赤字（{annual_deficit:,}円）を"
                    f"{_RECOVERY_HORIZON_YEARS}年以内に解消するには、"
                    f"年間{required_annual_uplift:,.0f}円以上の改善が必要です。"
                    f"現在の計画では{total_annual_uplift:,}円（推定回収期間: "
                    f"{'∞' if total_annual_uplift == 0 else f'{years_to_recovery:.1f}'}年）。"
                    "追加施策を検討してください。"
                )

    # ------------------------------------------------------------------
    # Gate 2: FSA classification path (no plan = no path to Normal)
    # ------------------------------------------------------------------
    # Triggered for 破綻懸念先 (In Danger of Bankruptcy) with no strategies.
    # 実質破綻先 / 破綻先 are routed to the workout node, not here.
    if state.fsa_classification is FsaClass.HATAN_KENENSAKI and not strategies:
        blockers.append(
            "no_recovery_plan: 破綻懸念先（In Danger of Bankruptcy）にもかかわらず"
            "改善施策がありません。正常先（Normal）への回帰計画が必要です。"
        )

    # ------------------------------------------------------------------
    # Gate 3: EWS trajectory (doubtful borrower needs multi-lever plan)
    # ------------------------------------------------------------------
    ews = state.ews_score or 0.0
    if ews >= _EWS_DOUBTFUL and len(strategies) < _MIN_STRATEGIES_DOUBTFUL:
        blockers.append(
            f"insufficient_strategies: EWSスコア（{ews:.1f}）が"
            f"破綻懸念先閾値（{_EWS_DOUBTFUL}）以上です。"
            f"最低{_MIN_STRATEGIES_DOUBTFUL}つの改善施策が必要ですが、"
            f"現在{len(strategies)}つしかありません。"
        )

    status = "FAIL" if blockers else "PASS"
    rationale = "信用保証協会評価（コンプライアンス最優先）: " + (
        "正常先への回帰計画に不備があります。下記をご確認ください。"
        if blockers
        else "3-5年以内の正常先回帰計画を確認しました。"
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
        "critic.guarantor",
        status=status,
        ews_score=ews,
        blockers=len(blockers),
    )

    return {"critic_feedbacks": [feedback.model_dump()]}
