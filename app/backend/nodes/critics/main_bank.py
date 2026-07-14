"""Main Bank critic node.

Persona: Risk-Averse Lead Bank (主幹事銀行 / Shukan-ji Ginko).
Priority: P1 — Accountability.

DETERMINISTIC FAIL gate conditions (rule-based, no LLM):

1. Owner executive compensation (Yakuin Hoshu / 役員報酬) has NOT been
   explicitly committed to be cut.
   Gate: ``state.yakuin_hoshu_cut`` must be ``True``.
   This flag is set by the banker/HITL during the interrupt step and is
   surfaced in the interrupt payload.  It defaults to ``False`` so the gate
   is live from the first revision cycle.

   Previous proxy (pre-fix): the gate checked for the presence of an SG&A
   rationalisation strategy, but ``propose_strategies`` ALWAYS appends that
   strategy, making the gate dead code.  The explicit flag makes the gate
   meaningful: a banker must affirmatively confirm the exec-comp cut.

2. No personal asset disposal commitment in the plan when a working-capital
   deficit exists.
   Gate: if ``state.working_capital_gap < 0``, then
   ``state.personal_asset_disposal`` must be ``True``.
   This flag is set by the banker/HITL and defaults to ``False``.

   Previous proxy (pre-fix): the gate checked for the presence of a
   working-capital strategy, but ``propose_strategies`` ALWAYS appends that
   strategy when a deficit exists, making the gate dead code.

Both conditions must be satisfied for PASS.
"""

from __future__ import annotations

from typing import Any

from app.backend.nodes.critics._persona import simulate_persona_argument
from app.backend.state import CriticFeedback, SaiseiState
from app.shared.logging import get_logger
from app.shared.settings import Settings

__all__ = ["main_bank_critic_node"]

_log = get_logger(__name__)

_PERSONA = "main_bank"
_PRIORITY = "P1"
_PROMPT = "critic_main_bank"


def main_bank_critic_node(state: SaiseiState, settings: Settings | None = None) -> dict[str, Any]:
    """Evaluate the Keikakusho from the lead bank's perspective.

    DETERMINISTIC FAIL gate:
    - ``yakuin_hoshu_cut`` is False → FAIL (blocker: yakuin_hoshu_not_cut).
    - Working-capital deficit AND ``personal_asset_disposal`` is False →
      FAIL (blocker: no_asset_disposal).

    Args:
        state: Current graph state (reads yakuin_hoshu_cut,
               personal_asset_disposal, working_capital_gap).

    Returns:
        Partial state update appending one :class:`CriticFeedback` dict.
    """
    blockers: list[str] = []

    # Gate 1: Exec-comp cut commitment (explicit flag).
    # NOTE: each blocker keeps a stable machine `code:` prefix — it is
    # load-bearing (lead_arranger.is_banker_only_blocker matches on it, the
    # revision directive carries it, and the critic tests assert on it). The
    # human sentence after the colon is banker-facing and must NOT leak Python
    # field names or HITL/implementation jargon; the UI strips the code prefix
    # before display.
    if not state.yakuin_hoshu_cut:
        blockers.append(
            "yakuin_hoshu_not_cut: 役員報酬の削減コミットメントが未確認です。"
            "主幹事銀行は、経営者による役員報酬の削減を前提として支援を検討します。"
            "下記「コミットメント確認」で「役員報酬削減」を承認してください。 "
            "(Executive compensation reduction has not been committed.)"
        )

    # Gate 2: Personal asset disposal commitment (explicit flag, deficit only).
    deficit = state.working_capital_gap is not None and state.working_capital_gap < 0
    if deficit and not state.personal_asset_disposal:
        blockers.append(
            "no_asset_disposal: 資金繰りに不足があるにもかかわらず、個人資産の処分"
            "コミットメントが未確認です。下記「コミットメント確認」で"
            "「個人資産処分」を承認してください。 "
            "(Personal asset disposal has not been committed despite a working-capital shortfall.)"
        )

    status = "FAIL" if blockers else "PASS"
    rationale = "主幹事銀行評価: " + (
        "支援の前提となるコミットメントが未だ確認できていません。下記の事項をご確認ください。"
        if blockers
        else "役員報酬の削減および個人資産の処分コミットメントを確認しました。"
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
        "critic.main_bank",
        status=status,
        blockers=len(blockers),
        yakuin_hoshu_cut=state.yakuin_hoshu_cut,
        personal_asset_disposal=state.personal_asset_disposal,
    )

    return {"critic_feedbacks": [feedback.model_dump()]}
