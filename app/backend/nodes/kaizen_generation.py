"""Kaizen generation node.

Merges the strategist (propose_strategies, strategist_node), plan writer
(render_keikakusho, plan_writer_node), and LLM polish (polish_keikakusho)
into a single blueprint file.

Public functions preserved for test compatibility:
- ``propose_strategies``: pure function, testable in isolation.
- ``strategist_node``: propose turnaround strategies.
- ``render_keikakusho``: pure function, testable in isolation.
- ``plan_writer_node``: write the Keikakusho draft.
- ``polish_keikakusho``: optional LLM polish pass (safe no-op without LLM).

The rendered Keikakusho output is byte-identical to the original so that
test_polish.py and test_graph_flow.py pass unchanged.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.backend.state import (
    CRITIC_FEEDBACKS_CLEAR,
    FEASIBILITY_NOTES_CLEAR,
    SaiseiState,
    Strategy,
)
from app.shared.logging import get_logger
from app.shared.models.accounting import TrialBalance
from app.shared.models.money import format_jpy
from app.shared.settings import Settings, get_settings

__all__ = [
    "propose_strategies",
    "strategist_node",
    "render_keikakusho",
    "plan_writer_node",
    "polish_keikakusho",
]

_log = get_logger(__name__)

#: Annualisation factor (monthly -> yearly).
_MONTHS = 12

# ---------------------------------------------------------------------------
# LLM polish (extracted from shared/graph/polish.py)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a Japanese regional-bank credit officer. Improve the readability "
    "and tone of the following Keiei Kaizen Keikakusho (経営改善計画書) draft. "
    "Preserve ALL monetary figures, section headings, and the FSA classification "
    "exactly. Do not invent numbers. Keep the Markdown structure. Respond with "
    "the improved Markdown only."
)


def _llm_configured(settings: Settings) -> bool:
    """Return whether an LLM is configured for the polish pass."""
    return bool(settings.llm_api_key and settings.llm_model)


def _call_llm(settings: Settings, draft: str) -> str:
    """Polish the draft via an OpenAI-compatible Chat Completions endpoint."""
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": draft},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    response = httpx.post(
        url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds
    )
    response.raise_for_status()
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected LLM response shape") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty LLM response content")
    return content


def polish_keikakusho(draft: str, settings: Settings | None = None) -> str:
    """Return an LLM-polished Keikakusho, or the original draft as a fallback.

    Best-effort: any failure (no config, transport error, bad response) returns
    the deterministic draft so the workflow never breaks on the polish step.

    Args:
        draft: The deterministic Keikakusho Markdown (source of truth).
        settings: Optional settings override (defaults to the cached settings).

    Returns:
        The polished draft when possible, otherwise ``draft`` unchanged.
    """
    settings = settings or get_settings()
    if not _llm_configured(settings):
        _log.info("polish.skipped", reason="llm_not_configured")
        return draft
    try:
        polished = _call_llm(settings, draft)
    except Exception as exc:  # noqa: BLE001 - polish is best-effort
        _log.warning("polish.failed", error=str(exc))
        return draft
    _log.info("polish.applied", model=settings.llm_model, chars=len(polished))
    return polished


# ---------------------------------------------------------------------------
# Strategist
# ---------------------------------------------------------------------------


def propose_strategies(latest: TrialBalance, working_capital_gap: int | None) -> list[Strategy]:
    """Build grounded turnaround strategies from the latest monthly figures.

    Args:
        latest: Most recent monthly trial balance.
        working_capital_gap: Shikin Kuri gap in yen (negative = deficit).

    Returns:
        Ordered list of candidate strategies.
    """
    sales = int(latest.uriage)
    cogs = int(latest.uriage_genka)
    sga = int(latest.hanbaihi)

    # Price pass-through: a 3% price increase flows straight to ordinary profit.
    price_uplift = int(round(sales * 0.03 * _MONTHS))

    # Cost reduction: trim 2% of COGS via procurement / yield improvement.
    cost_uplift = int(round(cogs * 0.02 * _MONTHS))

    # SG&A rationalisation: 5% of overhead.
    sga_uplift = int(round(sga * 0.05 * _MONTHS))

    strategies = [
        Strategy(
            title="価格転嫁の実行（Price pass-through）",
            rationale=(
                "Renegotiate unit prices with key customers to recover input-cost "
                "inflation (genka koutou). A 3% price increase restores margin "
                "eroded by failed kakaku tenka."
            ),
            expected_keijo_uplift=price_uplift,
        ),
        Strategy(
            title="原価低減（COGS reduction）",
            rationale=(
                "Diversify suppliers and improve yield to cut COGS by ~2%, "
                "directly lifting gross profit (uriage sourieki)."
            ),
            expected_keijo_uplift=cost_uplift,
        ),
        Strategy(
            title="販売費・一般管理費の見直し（SG&A rationalisation）",
            rationale=(
                "Rationalise overhead by ~5% to protect ordinary profit while "
                "price and cost measures take effect."
            ),
            expected_keijo_uplift=sga_uplift,
        ),
    ]

    if working_capital_gap is not None and working_capital_gap < 0:
        strategies.append(
            Strategy(
                title="資金繰り改善（Working-capital / Shikin Kuri）",
                rationale=(
                    "Shorten receivable days and negotiate extended payable terms "
                    "to close the working-capital deficit widened by BOJ rate hikes "
                    "and T+1/T+2 settlement pressure."
                ),
                expected_keijo_uplift=abs(working_capital_gap),
            )
        )

    return strategies


def strategist_node(state: SaiseiState) -> dict[str, Any]:
    """Propose turnaround strategies for human negotiation.

    Args:
        state: Current graph state (requires Shisanhyo).

    Returns:
        Partial state update with ``proposed_strategies`` and reset critic state.
    """
    if not state.shisanhyo:
        _log.warning("strategist.no_shisanhyo")
        return {"errors": [*state.errors, "Cannot propose strategies without Shisanhyo."]}

    strategies = propose_strategies(state.shisanhyo[-1], state.working_capital_gap)

    if state.revision_note:
        _log.info("strategist.revising", revision_note=state.revision_note)
    if state.revision_directive:
        _log.info("strategist.critic_revision", directive=state.revision_directive[:80])

    _log.info("strategist.proposed", count=len(strategies))
    # Reset critic state for a fresh round of critic evaluation.
    # IMPORTANT: returning CRITIC_FEEDBACKS_CLEAR (the sentinel object) triggers
    # the custom reducer to replace (not append to) the accumulated list, so
    # stale verdicts from earlier revision rounds do not bleed into lead_arranger.
    return {
        "proposed_strategies": strategies,
        "critic_feedbacks": CRITIC_FEEDBACKS_CLEAR,
        "feasibility_notes": FEASIBILITY_NOTES_CLEAR,
        "negotiation_status": "pending",
        "revision_directive": None,
    }


# ---------------------------------------------------------------------------
# Plan writer
# ---------------------------------------------------------------------------


def render_keikakusho(
    company_name: str,
    hojin_bango: str,
    fsa_kanji: str,
    latest: TrialBalance,
    strategy: Strategy,
    working_capital_gap: int | None,
) -> str:
    """Render the Keikakusho draft as Markdown.

    Args:
        company_name: Debtor company name.
        hojin_bango: 13-digit Corporate Number.
        fsa_kanji: FSA classification in kanji.
        latest: Most recent monthly trial balance.
        strategy: The approved turnaround strategy.
        working_capital_gap: Shikin Kuri gap in yen (negative = deficit).

    Returns:
        The Markdown Keikakusho draft.
    """
    gap_line = (
        format_jpy(working_capital_gap) if working_capital_gap is not None else "—"
    )
    return "\n".join(
        [
            "# 経営改善計画書（Keiei Kaizen Keikakusho）",
            "",
            f"- 企業名（Company）: {company_name}",
            f"- 法人番号（Hojin Bango）: {hojin_bango}",
            f"- 債務者区分（FSA classification）: {fsa_kanji}",
            "",
            "## 1. 現状分析（Current position）",
            "",
            f"- 売上（Uriage）: {format_jpy(int(latest.uriage))}",
            f"- 売上原価（Uriage Genka）: {format_jpy(int(latest.uriage_genka))}",
            f"- 販売費（Hanbaihi）: {format_jpy(int(latest.hanbaihi))}",
            f"- 経常利益（Keijo Rieki）: {format_jpy(latest.keijo_rieki)}",
            f"- 資金繰りギャップ（Working-capital gap）: {gap_line}",
            "",
            "## 2. 改善施策（Turnaround strategy）",
            "",
            f"### {strategy.title}",
            "",
            strategy.rationale,
            "",
            f"- 期待される経常利益改善（Expected Keijo Rieki uplift）: "
            f"{format_jpy(int(strategy.expected_keijo_uplift))} / 年",
            "",
            "## 3. 実行計画（Action plan）",
            "",
            "1. 施策の実行体制を構築し、担当者と期限を設定する。",
            "2. 月次で進捗をモニタリングし、経常利益と資金繰りを検証する。",
            "3. 銀行と四半期ごとにレビューを実施する。",
            "",
        ]
    )


def plan_writer_node(state: SaiseiState) -> dict[str, Any]:
    """Write the Keikakusho draft from the approved strategy.

    Args:
        state: Current graph state (requires an approved strategy and Shisanhyo).

    Returns:
        Partial state update with ``keikakusho_draft``.
    """
    if state.approved_strategy is None or not state.shisanhyo:
        _log.warning("plan_writer.missing_inputs")
        return {
            "errors": [*state.errors, "Cannot write Keikakusho without an approved strategy."]
        }

    strategy: Strategy = state.approved_strategy
    profile = state.company_profile
    draft = render_keikakusho(
        company_name=profile.name if profile else state.tdb_code,
        hojin_bango=state.hojin_bango,
        fsa_kanji=state.fsa_classification.kanji if state.fsa_classification else "—",
        latest=state.shisanhyo[-1],
        strategy=strategy,
        working_capital_gap=state.working_capital_gap,
    )
    draft = polish_keikakusho(draft)
    _log.info("plan_writer.drafted", strategy=strategy.title, chars=len(draft))
    return {"keikakusho_draft": draft}
