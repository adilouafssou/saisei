"""Feasibility critic node (PART 4 — upstream operational pre-screen).

Persona: Turnaround consultant (事業再生コンサルタント).
Role: ADVISORY ONLY. Asks "can THIS firm actually execute each proposed
strategy?" and annotates strategies with an operational-feasibility note.

DESIGN CONTRACT (the one rule that governs everything):
- This node NEVER gates PASS/FAIL, NEVER feeds routing, and NEVER alters any
  figure used by a downstream gate or the burden-sharing table. It only emits
  advisory ``feasibility_notes``.
- The ``achievability`` band and ``achievability_score`` are DETERMINISTIC,
  rule-based proxies computed from the strategy's expected uplift relative to
  the firm's monthly sales. Reproducible and auditable.
- The optional LLM ``advisory`` text mirrors the ``polish_keikakusho`` contract:
  best-effort, with a deterministic offline fallback (empty string) so
  ``make verify`` stays green in the no-network CI sandbox.

Wiring: ``strategist → feasibility_critic → {main_bank, sub_bank, guarantor}``.
It runs once, before the parallel critic fan-out, and does not block it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.backend.state import FeasibilityNote, SaiseiState, Strategy
from app.backend.tools.retrieval import (
    RetrievalProvider,
    RetrievalSnippet,
    get_retrieval_provider,
)
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = ["feasibility_critic_node", "assess_feasibility"]

_log = get_logger(__name__)

#: Static persona prompt (kept out of Python per project convention).
_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "feasibility_critic.md"

# ---------------------------------------------------------------------------
# Deterministic achievability proxy.
#
# A strategy whose expected ANNUAL uplift is small relative to the firm's
# ANNUAL sales is operationally easy to achieve (high feasibility); one that
# demands an uplift that is large relative to sales is a stretch (low
# feasibility). Bands are transparent thresholds on uplift / annual_sales.
# ---------------------------------------------------------------------------

#: uplift/annual_sales at or below this → 'high' achievability.
_HIGH_RATIO_CEILING: float = 0.05   # <= 5% of annual sales
#: uplift/annual_sales at or below this (but above high) → 'medium'.
_MEDIUM_RATIO_CEILING: float = 0.15  # <= 15% of annual sales
_MONTHS: int = 12


def _achievability_band(ratio: float) -> tuple[str, float]:
    """Map an uplift/annual-sales ratio to a (band, score) pair.

    Score is a deterministic 0-100 where higher = more achievable. It decreases
    monotonically with the ratio and is clamped to [0, 100].

    Args:
        ratio: expected_annual_uplift / annual_sales (>= 0).

    Returns:
        (band, score) where band is 'high' | 'medium' | 'low'.
    """
    # Linear: ratio 0 -> 100, ratio 0.20 -> 0. Clamped.
    score = round(max(0.0, min(100.0, 100.0 * (1.0 - ratio / 0.20))), 2)
    if ratio <= _HIGH_RATIO_CEILING:
        return "high", score
    if ratio <= _MEDIUM_RATIO_CEILING:
        return "medium", score
    return "low", score


def assess_feasibility(
    strategy: Strategy,
    monthly_sales: int,
) -> FeasibilityNote:
    """Compute a deterministic feasibility note for one strategy.

    Pure function: no LLM, no I/O. The ``advisory`` field is left empty here and
    populated separately (best-effort) by the node when an LLM is configured.

    Args:
        strategy: The proposed strategy to assess.
        monthly_sales: Latest monthly sales (売上), JPY. Annualised internally.

    Returns:
        A :class:`FeasibilityNote` with an empty ``advisory``.
    """
    annual_sales = max(int(monthly_sales) * _MONTHS, 1)
    uplift = int(strategy.expected_keijo_uplift)
    ratio = abs(uplift) / annual_sales
    band, score = _achievability_band(ratio)

    rationale = (
        f"期待経常利益改善（{uplift:,}円/年）は年間売上（{annual_sales:,}円）の"
        f"{ratio:.1%}に相当し、実現可能性は「{band}」と評価されます。"
    )

    return FeasibilityNote(
        strategy_title=strategy.title,
        achievability=band,
        achievability_score=score,
        rationale=rationale,
        advisory="",
    )


def _llm_configured(settings: Settings) -> bool:
    """Return whether an LLM is configured for the advisory pass."""
    return bool(settings.llm_api_key and settings.llm_model)


def _load_prompt() -> str:
    """Load the static persona prompt; empty string if unavailable."""
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        _log.warning("feasibility.prompt_missing", error=str(exc))
        return ""


def _call_llm(
    settings: Settings,
    system_prompt: str,
    note: FeasibilityNote,
    strategy: Strategy,
    monthly_sales: int,
    snippets: list[RetrievalSnippet],
) -> str:
    """Request an advisory feasibility paragraph via Chat Completions.

    Mirrors the ``polish_keikakusho`` client pattern. Raises on any transport
    or shape error; the caller swallows it for the offline fallback.

    ``snippets`` are advisory RAG precedents (past plans / benchmarks / FSA
    passages); they enrich the prompt context only and never affect the
    deterministic band/score.
    """
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    user_content = (
        f"戦略タイトル: {strategy.title}\n"
        f"根拠: {strategy.rationale}\n"
        f"期待経常利益改善: {int(strategy.expected_keijo_uplift):,}円/年\n"
        f"最新月次売上: {int(monthly_sales):,}円\n"
        f"決定論的実現可能性バンド: {note.achievability} "
        f"(スコア {note.achievability_score})\n"
    )
    if snippets:
        precedent_block = "\n".join(
            f"- [{s.source}] {s.text}" for s in snippets
        )
        user_content += (
            "\n参考事例（過去計画・ベンチマーク・金融庁指針 / advisory precedents）:\n"
            f"{precedent_block}\n"
        )
    payload = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
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
    return content.strip()


def _advisory_for(
    settings: Settings,
    system_prompt: str,
    note: FeasibilityNote,
    strategy: Strategy,
    monthly_sales: int,
    snippets: list[RetrievalSnippet],
) -> str:
    """Best-effort advisory text; empty string on any failure (offline fallback)."""
    if not system_prompt:
        return ""
    try:
        return _call_llm(
            settings, system_prompt, note, strategy, monthly_sales, snippets
        )
    except Exception as exc:  # noqa: BLE001 - advisory is best-effort
        _log.warning("feasibility.advisory_failed", error=str(exc))
        return ""


def _retrieval_query(strategy: Strategy, company_name: str) -> str:
    """Build the RAG query for a strategy (advisory precedent retrieval)."""
    return f"{company_name} {strategy.title} {strategy.rationale}".strip()


def feasibility_critic_node(
    state: SaiseiState,
    settings: Settings | None = None,
    retrieval: RetrievalProvider | None = None,
) -> dict[str, Any]:
    """Annotate each proposed strategy with an advisory feasibility note.

    Deterministic achievability bands/scores are always computed; the optional
    LLM advisory text is best-effort and empty when no LLM is configured. When
    an LLM is configured, advisory RAG precedents (past plans / benchmarks / FSA
    passages) are retrieved per strategy and added to the prompt context.

    ADVISORY ONLY: returns ``feasibility_notes`` only; touches no gate field.
    Retrieval never affects the deterministic band/score.

    Args:
        state: Current graph state (reads proposed_strategies, shisanhyo).
        settings: Optional settings override (defaults to cached settings).
        retrieval: Optional retrieval provider (defaults to the configured one;
            the mock provider returns no precedents offline).

    Returns:
        Partial state update with ``feasibility_notes`` (a list of note dicts).
    """
    settings = settings or get_settings()
    strategies = state.proposed_strategies

    if not strategies:
        _log.info("feasibility.no_strategies")
        return {"feasibility_notes": []}

    monthly_sales = int(state.shisanhyo[-1].uriage) if state.shisanhyo else 0

    use_llm = _llm_configured(settings)
    system_prompt = _load_prompt() if use_llm else ""
    # Retrieval only matters when we will actually phrase an advisory note.
    retrieval = retrieval or (get_retrieval_provider(settings) if use_llm else None)
    company_name = (
        state.company_profile.name if state.company_profile else state.tdb_code
    )

    notes: list[dict] = []
    for strategy in strategies:
        note = assess_feasibility(strategy, monthly_sales)
        if use_llm:
            snippets: list[RetrievalSnippet] = []
            if retrieval is not None:
                snippets = retrieval.search(
                    _retrieval_query(strategy, company_name),
                    settings.retrieval_top_k,
                )
            advisory = _advisory_for(
                settings, system_prompt, note, strategy, monthly_sales, snippets
            )
            if advisory:
                note = note.model_copy(update={"advisory": advisory})
        notes.append(note.model_dump())

    _log.info(
        "feasibility.assessed",
        strategies=len(strategies),
        llm=use_llm,
        bands=[n["achievability"] for n in notes],
    )
    return {"feasibility_notes": notes}
