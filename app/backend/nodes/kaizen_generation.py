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

from app.backend.analysis.numeric_preservation import guard_polished_text
from app.backend.analysis.pnl_recovery import RecoveryProjection, project_recovery
from app.backend.secrets import resolve_secret
from app.backend.state import (
    CRITIC_FEEDBACKS_CLEAR,
    FEASIBILITY_NOTES_CLEAR,
    SaiseiState,
    Strategy,
)
from app.shared.constants import EWS_SUBSTANDARD, WORKING_CAPITAL_FINANCING_RATE
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


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a model attribute or a mapping key.

    The guarantee-release conditions reach the plan writer as a live
    ``HoshoKaijoConditions`` model in-graph, but a checkpointer-rehydrated run
    may present them as a plain dict. This reads uniformly from either shape so
    the rendered section never assumes a live object.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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
    """Return whether an LLM is configured for the polish pass.

    The API key is read through the secret seam, so it may be a literal or a
    ``@env:`` / ``@file:`` / ``@/path`` reference (and, in production, a
    Vault-backed value) without changing the offline-fallback contract.
    """
    return bool(resolve_secret(settings.llm_api_key) and settings.llm_model)


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
    headers = {"Authorization": f"Bearer {resolve_secret(settings.llm_api_key)}"}

    response = httpx.post(url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds)
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
    # Deterministic numeric-preservation gate (Feature 1, LangSmith eval): a
    # readability pass must NEVER add, drop, or alter a figure in a regulated
    # credit document. If the polish changed any yen value, discard it and keep
    # the deterministic draft (fail-safe), logging the discrepancy for eval.
    text, preservation = guard_polished_text(draft, polished)
    if not preservation.preserved:
        _log.warning(
            "polish.numbers_not_preserved",
            model=settings.llm_model,
            reason=preservation.reason(),
        )
        return text
    _log.info("polish.applied", model=settings.llm_model, chars=len(text))
    return text


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
        # Recurring benefit of closing the gap is a FLOW: the annual financing
        # cost saved, not the full gap STOCK. Using abs(gap) directly made this
        # single strategy dominate the others and distort the sub-bank pro-rata
        # share (largest-strategy / total). Use the carrying cost at the assumed
        # financing rate so the uplift is comparable to the other levers.
        wc_uplift = int(round(abs(working_capital_gap) * WORKING_CAPITAL_FINANCING_RATE))
        strategies.append(
            Strategy(
                title="資金繰り改善（Working-capital / Shikin Kuri）",
                rationale=(
                    "Shorten receivable days and negotiate extended payable terms "
                    "to close the working-capital deficit widened by BOJ rate hikes "
                    "and T+1/T+2 settlement pressure."
                ),
                expected_keijo_uplift=wc_uplift,
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


def _recovery_section_lines(recovery: RecoveryProjection | None) -> list[str]:
    """Render the Feature 5 recovery projection as Markdown section lines.

    Produces the "## 4. 損益計画" section: a per-month table of the phased
    uplift, projected 経常利益, and recomputed EWS, plus the recovery verdict.
    Returns ``[]`` when there is no projection to render (so the existing draft
    is byte-identical when recovery is not supplied).
    """
    if recovery is None or not recovery.months:
        return []

    if recovery.recovery_month_index is not None:
        verdict = (
            f"- 正常化見込（Projected normalisation）: "
            f"{recovery.recovery_month_index}ヶ月目（EWS < {int(EWS_SUBSTANDARD)}）"
        )
    else:
        verdict = (
            f"- 正常化見込（Projected normalisation）: "
            f"{len(recovery.months)}ヶ月以内には未達（EWS ≥ {int(EWS_SUBSTANDARD)}）"
        )

    lines = [
        "## 4. 損益計画（Recovery projection）",
        "",
        f"- 期待経常利益改善（Annual uplift）: {format_jpy(int(recovery.annual_uplift))} / 年",
        f"- 月次換算（Full monthly uplift）: "
        f"{format_jpy(int(recovery.full_monthly_uplift))} / 月"
        f"（{recovery.ramp_months}ヶ月で段階導入）",
        verdict,
        "",
        "| 月 (Month) | 月次改善額 (Uplift) | 経常利益 (Keijo Rieki) | EWS |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for m in recovery.months:
        lines.append(
            f"| {m.month_index} | {format_jpy(int(m.monthly_uplift))} | "
            f"{format_jpy(int(m.keijo_rieki))} | {m.ews_score:.2f} |"
        )
    lines.append("")
    return lines


def _assessment_basis_lines(
    ews_score: float | None,
    ews_breakdown: list[dict[str, Any]] | None,
    classification_reason: str,
) -> list[str]:
    """Render the Feature 7 assessment-basis (explainability) section lines.

    Produces a "## 1-2. 診断根拠" addendum exposing WHY the borrower landed in
    its band: the EWS score, the per-signal contribution table (which sums to the
    score by construction), and the deterministic classification threshold
    reason. This is the audit trail an FSA examiner reads in the submitted
    document — the live-UI explainability, made part of the regulated artifact.

    Returns ``[]`` when there is nothing to explain (no breakdown AND no reason),
    so the draft is byte-identical to the pre-Feature-7 output when the basis is
    not supplied. Every figure is a deterministic source value, so the
    numeric-preservation gate over the LLM polish protects them like any other.
    """
    breakdown = ews_breakdown or []
    if not breakdown and not classification_reason:
        return []

    score_str = f"{float(ews_score):.2f}" if ews_score is not None else "—"
    lines = [
        "## 1-2. 診断根拠（Assessment basis）",
        "",
        f"- EWSスコア（Early Warning Signal）: {score_str} / 100",
    ]
    if classification_reason:
        lines.append(f"- 区分根拠（Classification basis）: {classification_reason}")
    if breakdown:
        lines.extend(
            [
                "",
                "| シグナル (Signal) | 測定値 (Measure) | 寄与点 (Points) | 最大 (Weight) |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for s in breakdown:
            raw = float(s.get("raw", 0.0)) * 100.0
            points = float(s.get("points", 0.0))
            weight = float(s.get("weight", 0.0))
            label = str(s.get("label_ja", s.get("key", "")))
            lines.append(f"| {label} | {raw:.1f}% | {points:.1f} | {weight:.0f} |")
    lines.append("")
    return lines


def _hosho_section_lines(
    hosho_score: float | None,
    hosho_eligible: bool | None,
    hosho_conditions: Any | None,
) -> list[str]:
    """Render the guarantee-release (経営者保証解除) basis as Markdown lines.

    Feature 7 parity (with the EWS assessment basis): puts the deterministic
    保証解除 breakdown into the regulated document the banker submits — the
    overall score + eligibility, a per-pillar contribution table (法人個人分離 /
    財務基盤 / 情報開示), and the ordered, actionable "what must change to
    release the personal guarantee" directives.

    Returns ``[]`` when there are no conditions to render, so the draft is
    byte-identical to the pre-this-change output when the basis is not supplied.
    Every figure is a deterministic source value, protected by the
    numeric-preservation polish gate like any other. Accepts a model or a
    checkpointer-rehydrated dict (via ``_attr``).
    """
    if not hosho_conditions:
        return []

    score_str = f"{float(hosho_score):.2f}" if hosho_score is not None else "—"
    eligible_str = "該当（Eligible）" if hosho_eligible else "未達（Not yet eligible）"
    pillars = [
        ("bunri", "法人個人分離（Asset separation）", 40),
        ("zaimu", "財務基盤（Financial base）", 35),
        ("kaiji", "情報開示（Disclosure）", 25),
    ]
    lines = [
        "## 5. 経営者保証解除（Guarantee-release basis）",
        "",
        f"- 保証解除スコア（Hosho Kaijo score）: {score_str} / 100（{eligible_str}）",
        "",
        "| 条件 (Condition) | 達成 (Met) | 得点 (Points) | 最大 (Weight) |",
        "| --- | :---: | ---: | ---: |",
    ]
    for key, label, weight in pillars:
        met = "✓" if bool(_attr(hosho_conditions, f"{key}_met", False)) else "—"
        pts = float(_attr(hosho_conditions, f"{key}_score", 0.0) or 0.0)
        lines.append(f"| {label} | {met} | {pts:.1f} | {weight} |")

    directives = [str(d) for d in (_attr(hosho_conditions, "ordered_directives", []) or [])]
    if directives:
        lines.extend(["", "### 解除に向けた課題（Required changes）", ""])
        lines.extend(f"- {d}" for d in directives)
    lines.append("")
    return lines


def _precedent_section_lines(
    feasibility_notes: list[dict[str, Any]] | None,
) -> list[str]:
    """Render the advisory precedent-citations appendix (Feature 4, partial).

    Surfaces, per strategy, the precedent sources (past plans / benchmarks / FSA
    passages) that GROUNDED the feasibility critic's advisory note — the RAG
    precedents made visible as citations in the regulated document, so a banker
    or examiner can see what each operational-feasibility opinion rests on.

    ADVISORY ONLY — the design contract that governs everything here:
      * It cites only ALREADY-GROUNDED provenance the feasibility critic produced
        (claims the claim-grounding pipeline marked ``grounded``). It retrieves
        nothing, calls no LLM, and adds no figure.
      * It is explicitly labelled advisory so it can never be mistaken for the
        deterministic basis (sections 1-2 / 5), which is the regulated spine.
      * Offline / no-LLM runs have empty advisory provenance, so this returns
        ``[]`` and the draft stays byte-identical to the pre-this-change output.

    Each strategy lists its distinct cited sources in first-seen order (stable,
    deterministic, de-duplicated). Strategies with no grounded citation are
    skipped; when NONE has one, the whole section is omitted.

    Args:
        feasibility_notes: The advisory ``feasibility_notes`` dicts from state
            (each may carry ``advisory_provenance`` = ``[{text, status,
            citations}]``). May be ``None`` / empty.

    Returns:
        Markdown section lines, or ``[]`` when there is nothing to cite.
    """
    notes = feasibility_notes or []

    # Collect, per strategy, the distinct source labels from GROUNDED claims.
    per_strategy: list[tuple[str, list[str]]] = []
    for note in notes:
        title = str(_attr(note, "strategy_title", "") or "")
        seen: list[str] = []
        for claim in _attr(note, "advisory_provenance", []) or []:
            # Only grounded claims carry an attributable precedent.
            if str(_attr(claim, "status", "")) != "grounded":
                continue
            for source in _attr(claim, "citations", []) or []:
                label = str(source).strip()
                if label and label not in seen:
                    seen.append(label)
        if seen:
            per_strategy.append((title, seen))

    if not per_strategy:
        return []

    lines = [
        "## 6. 参考事例（Advisory precedents — 参考情報）",
        "",
        "下記は実現可能性に関する助言の根拠となった参考事例です。 "
        "本欄は参考情報（advisory）であり、債務者区分や各指標の算出には使用されていません。 "
        "(Precedents that grounded the feasibility advisory. Advisory only — "
        "never used for the FSA classification or any figure.)",
        "",
    ]
    for title, sources in per_strategy:
        lines.append(f"### {title}")
        lines.append("")
        for source in sources:
            lines.append(f"- {source}")
        lines.append("")
    return lines


def render_keikakusho(
    company_name: str,
    hojin_bango: str,
    fsa_kanji: str,
    latest: TrialBalance,
    strategy: Strategy,
    working_capital_gap: int | None,
    recovery: RecoveryProjection | None = None,
    *,
    ews_score: float | None = None,
    ews_breakdown: list[dict[str, Any]] | None = None,
    classification_reason: str = "",
    hosho_score: float | None = None,
    hosho_eligible: bool | None = None,
    hosho_conditions: Any | None = None,
    feasibility_notes: list[dict[str, Any]] | None = None,
) -> str:
    """Render the Keikakusho draft as Markdown.

    Args:
        company_name: Debtor company name.
        hojin_bango: 13-digit Corporate Number.
        fsa_kanji: FSA classification in kanji.
        latest: Most recent monthly trial balance.
        strategy: The approved turnaround strategy.
        working_capital_gap: Shikin Kuri gap in yen (negative = deficit).
        recovery: Optional Feature 5 recovery projection. When provided (and it
            has months), a "## 4. 損益計画" section is appended. When ``None`` the
            output is byte-identical to the pre-Feature-5 draft.
        ews_score: Optional EWS score for the Feature 7 assessment-basis section.
        ews_breakdown: Optional per-signal EWS contribution dicts. When provided
            (or a classification_reason is), a "## 1-2. 診断根拠" section is
            appended after section 1. When omitted the output is byte-identical
            to the pre-Feature-7 draft.
        classification_reason: Optional deterministic classification threshold
            reason for the assessment-basis section.
        feasibility_notes: Optional advisory ``feasibility_notes`` dicts. When at
            least one carries a GROUNDED advisory citation, a "## 6. 参考事例"
            advisory precedent-citations appendix is appended. When omitted (or
            none is grounded, e.g. offline) the output is byte-identical.

    Returns:
        The Markdown Keikakusho draft.
    """
    gap_line = format_jpy(working_capital_gap) if working_capital_gap is not None else "—"
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
            *_assessment_basis_lines(ews_score, ews_breakdown, classification_reason),
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
            *_recovery_section_lines(recovery),
            *_hosho_section_lines(hosho_score, hosho_eligible, hosho_conditions),
            *_precedent_section_lines(feasibility_notes),
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
        return {"errors": [*state.errors, "Cannot write Keikakusho without an approved strategy."]}

    strategy: Strategy = state.approved_strategy
    profile = state.company_profile
    # Feature 5: project the recovery curve from the approved uplift. Pure
    # deterministic arithmetic over the verified history; appended as section 4.
    recovery = project_recovery(state.shisanhyo, int(strategy.expected_keijo_uplift))
    draft = render_keikakusho(
        company_name=profile.name if profile else state.tdb_code,
        hojin_bango=state.hojin_bango,
        fsa_kanji=state.fsa_classification.kanji if state.fsa_classification else "—",
        latest=state.shisanhyo[-1],
        strategy=strategy,
        working_capital_gap=state.working_capital_gap,
        recovery=recovery,
        # Feature 7: include the assessment basis (EWS breakdown + classification
        # reason) so the explainability reaches the submitted regulated document,
        # not just the live UI. Deterministic source figures; the polish gate
        # protects them like any other.
        ews_score=state.ews_score,
        ews_breakdown=state.ews_breakdown,
        classification_reason=state.classification_reason,
        # Feature 7 parity: carry the guarantee-release basis into the document.
        hosho_score=state.hosho_kaijo_score,
        hosho_eligible=state.hosho_kaijo_eligible,
        hosho_conditions=state.hosho_kaijo_conditions,
        # Feature 4 (partial): cite the advisory RAG precedents that grounded the
        # feasibility notes as a clearly-labelled advisory appendix. Advisory
        # only; empty (byte-identical draft) offline or when nothing is grounded.
        feasibility_notes=state.feasibility_notes,
    )
    draft = polish_keikakusho(draft)
    _log.info("plan_writer.drafted", strategy=strategy.title, chars=len(draft))
    return {"keikakusho_draft": draft}
