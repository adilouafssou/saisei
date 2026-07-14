"""Feasibility critic node (MR #2 — re-posed deterministic floor + citation-grounded advisory).

Persona: Turnaround consultant (事業再生コンサルタント).
Role: ADVISORY ONLY. Asks "can THIS firm actually execute each proposed
strategy?" and annotates strategies with an operational-feasibility note.

DESIGN CONTRACT (the one rule that governs everything):
- This node NEVER gates PASS/FAIL, NEVER feeds routing, and NEVER alters any
  figure used by a downstream gate or the burden-sharing table. It only emits
  advisory ``feasibility_notes`` and the deterministic ``reconciliation_required``
  / ``reconciliation_details`` fields.
- The ``achievability`` band and ``achievability_score`` are DETERMINISTIC,
  rule-based proxies computed from four auditable signals (see formula below).
  Reproducible and auditable to the yen.
- The optional LLM ``advisory`` text mirrors the ``polish_keikakusho`` contract:
  best-effort, with a deterministic offline fallback (empty string) so
  ``make verify`` stays green in the no-network CI sandbox.
- Citation grounding: when an LLM advisory is produced, a deterministic
  post-check helper flags whether the advisory is supported by >= 1 retrieved
  snippet (``advisory_grounded: bool``). Metadata only — never feeds any gate.
- Reconciliation: a PURE DETERMINISTIC PREDICATE compares the floor band against
  an LLM-derived band. If they disagree by >= RECONCILIATION_BAND_DISTANCE, the
  node sets ``reconciliation_required=True`` and populates
  ``reconciliation_details``. The graph then routes to hitl_negotiation BEFORE
  the critic fan-out. The LLM can ONLY raise the question; it NEVER decides
  direction, verdict, or figure.

DETERMINISTIC FEASIBILITY FLOOR FORMULA (MR #2):
-------------------------------------------------
Four signals, all sourced from SaiseiState fields already present:

  1. uplift_ratio   = expected_annual_uplift / annual_sales
                      (strategy ambition relative to firm size)
  2. wc_stress      = max(0, -working_capital_gap) / annual_sales
                      (working-capital deficit as a fraction of sales;
                       0 when gap >= 0, i.e. no deficit)
  3. rate_stress    = latest_policy_rate_bps / 10_000
                      (BOJ rate as a decimal; 60 bps -> 0.006)
  4. settle_stress  = max(0, receivable_days - payable_days) / 90
                      (cash-conversion-cycle stress; 90 days = reference)

Composite score (0-100, higher = more achievable):

  raw = 100
        - FEASIBILITY_WEIGHT_UPLIFT   * uplift_ratio   * 100
        - FEASIBILITY_WEIGHT_WC       * wc_stress      * 100
        - FEASIBILITY_WEIGHT_RATE     * rate_stress    * 100
        - FEASIBILITY_WEIGHT_SETTLE   * settle_stress  * 100

  (FEASIBILITY_WEIGHT_UPLIFT is multiplied by FEASIBILITY_INDUSTRY_UPLIFT_FACTOR
   for capital-intensive industries: 製造業, 建設業, 運輸業.)

Clamped to [0, 100] and rounded to 2 decimal places.

Band thresholds (score-based):
  score >= FEASIBILITY_HIGH_FLOOR   -> 'high'
  score >= FEASIBILITY_MEDIUM_FLOOR -> 'medium'
  score <  FEASIBILITY_MEDIUM_FLOOR -> 'low'

Monotonicity properties (auditable):
  - Larger working-capital deficit (more negative gap) -> lower score.
  - Higher BOJ rate stress -> lower score.
  - Larger uplift-to-sales ratio -> lower score.
  - Longer cash-conversion cycle -> lower score.
  - Capital-intensive industry -> lower score (higher uplift weight).

Wiring: ``strategist -> feasibility_critic -> {hitl_negotiation | fan-out}``.
It runs once, before the parallel critic fan-out, and does not block it when
reconciliation_required is False.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.backend.analysis.evidence import build_evidence_packet
from app.backend.analysis.grounding_pipeline import ground_qualitative_text
from app.backend.analysis.uplift_grounding import assess_uplift
from app.backend.nodes.critics.realism import assess_realism
from app.backend.prompts.registry import get_prompt_or_empty
from app.backend.secrets import resolve_secret
from app.backend.state import (
    FeasibilityNote,
    ReconciliationDetail,
    SaiseiState,
    Strategy,
)
from app.backend.tools.boj_macro import RatePoint, SettlementMetrics
from app.backend.tools.retrieval import (
    RetrievalProvider,
    RetrievalSnippet,
    get_retrieval_provider,
)
from app.shared.constants import (
    FEASIBILITY_HIGH_FLOOR,
    FEASIBILITY_INDUSTRY_UPLIFT_FACTOR,
    FEASIBILITY_MEDIUM_FLOOR,
    FEASIBILITY_WEIGHT_RATE,
    FEASIBILITY_WEIGHT_SETTLE,
    FEASIBILITY_WEIGHT_UPLIFT,
    FEASIBILITY_WEIGHT_WC,
    MAX_RECONCILIATION_TRIGGERS,
    MONTHS_PER_YEAR,
    RECONCILIATION_BAND_DISTANCE,
)
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "feasibility_critic_node",
    "assess_feasibility",
    "is_advisory_grounded",
    "band_ordinal",
    "llm_band_from_score",
]

_log = get_logger(__name__)

#: Logical name of the feasibility persona prompt in the prompt registry.
_PROMPT_NAME = "feasibility_critic"

# ---------------------------------------------------------------------------
# Industry classification for uplift-weight adjustment.
# Capital-intensive industries carry higher execution risk for a given uplift
# ratio (long lead times, capital requirements, supplier dependencies).
# ---------------------------------------------------------------------------

#: Keywords that identify capital-intensive industries (matched as substrings).
_CAPITAL_INTENSIVE_KEYWORDS: tuple[str, ...] = ("製造", "建設", "運輸", "物流", "鉱業")


def _is_capital_intensive(industry: str) -> bool:
    """Return True when the industry string matches a capital-intensive sector.

    Args:
        industry: Industry label from CompanyProfile (e.g. '金属部品製造業').

    Returns:
        True when any capital-intensive keyword is found in the label.
    """
    return any(kw in industry for kw in _CAPITAL_INTENSIVE_KEYWORDS)


# ---------------------------------------------------------------------------
# Band ordinal mapping (used by the deterministic reconciliation predicate).
# ---------------------------------------------------------------------------

#: Ordinal values for achievability bands (higher = more achievable).
_BAND_ORDINALS: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def band_ordinal(band: str) -> int:
    """Return the ordinal (0-2) for an achievability band.

    Args:
        band: 'high' | 'medium' | 'low'.

    Returns:
        Ordinal integer (2=high, 1=medium, 0=low). Unknown bands return 1.
    """
    return _BAND_ORDINALS.get(band, 1)


def llm_band_from_score(score: float) -> str:
    """Map a 0-100 score to an achievability band (same thresholds as the floor).

    Used to convert an LLM-supplied numeric feasibility signal onto the same
    band scale as the deterministic floor so the reconciliation predicate can
    compare them.

    Args:
        score: Feasibility score in [0, 100].

    Returns:
        'high' | 'medium' | 'low'.
    """
    if score >= FEASIBILITY_HIGH_FLOOR:
        return "high"
    if score >= FEASIBILITY_MEDIUM_FLOOR:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Deterministic multi-factor feasibility floor (MR #2).
# ---------------------------------------------------------------------------


def _uplift_weight(industry: str) -> float:
    """Return the uplift-ratio weight, adjusted for industry type.

    Args:
        industry: Industry label from CompanyProfile.

    Returns:
        FEASIBILITY_WEIGHT_UPLIFT * FEASIBILITY_INDUSTRY_UPLIFT_FACTOR for
        capital-intensive industries; FEASIBILITY_WEIGHT_UPLIFT otherwise.
    """
    if _is_capital_intensive(industry):
        return FEASIBILITY_WEIGHT_UPLIFT * FEASIBILITY_INDUSTRY_UPLIFT_FACTOR
    return FEASIBILITY_WEIGHT_UPLIFT


def _rate_stress_from_curve(rate_curve: list[RatePoint]) -> float:
    """Extract the latest BOJ policy rate as a decimal (bps / 10_000).

    Args:
        rate_curve: BOJ policy-rate curve (latest point used).

    Returns:
        Rate stress as a decimal (e.g. 60 bps -> 0.006). 0.0 when curve empty.
    """
    if not rate_curve:
        return 0.0
    return rate_curve[-1].policy_rate_bps / 10_000.0


def _settle_stress_from_metrics(metrics: SettlementMetrics | None) -> float:
    """Compute cash-conversion-cycle stress from settlement metrics.

    Args:
        metrics: Settlement / liquidity metrics (DSO/DPO). None -> 0.0.

    Returns:
        Stress in [0, inf): max(0, receivable_days - payable_days) / 90.
        0.0 when metrics is None or payable_days >= receivable_days.
    """
    if metrics is None:
        return 0.0
    cycle = max(0, metrics.receivable_days - metrics.payable_days)
    return cycle / 90.0


def assess_feasibility(
    strategy: Strategy,
    monthly_sales: int,
    working_capital_gap: int | None = None,
    rate_curve: list[RatePoint] | None = None,
    settlement_metrics: SettlementMetrics | None = None,
    industry: str = "",
) -> FeasibilityNote:
    """Compute a deterministic feasibility note for one strategy (MR #2 formula).

    Pure function: no LLM, no I/O. The ``advisory`` field is left empty here and
    populated separately (best-effort) by the node when an LLM is configured.

    Formula (all terms auditable — see module docstring for full derivation):

        uplift_ratio  = |expected_annual_uplift| / annual_sales
        wc_stress     = max(0, -working_capital_gap) / annual_sales
        rate_stress   = latest_policy_rate_bps / 10_000
        settle_stress = max(0, receivable_days - payable_days) / 90

        w_uplift = FEASIBILITY_WEIGHT_UPLIFT * (FEASIBILITY_INDUSTRY_UPLIFT_FACTOR
                   if capital-intensive industry else 1.0)

        score = clamp(100
                      - w_uplift      * uplift_ratio  * 100
                      - WEIGHT_WC     * wc_stress     * 100
                      - WEIGHT_RATE   * rate_stress   * 100
                      - WEIGHT_SETTLE * settle_stress * 100,
                      0, 100)

        band: score >= FEASIBILITY_HIGH_FLOOR   -> 'high'
              score >= FEASIBILITY_MEDIUM_FLOOR -> 'medium'
              else                              -> 'low'

    Monotonicity (auditable):
        - Larger WC deficit -> lower score (higher wc_stress).
        - Higher BOJ rate -> lower score (higher rate_stress).
        - Larger uplift-to-sales ratio -> lower score.
        - Longer cash-conversion cycle -> lower score.
        - Capital-intensive industry -> lower score (higher w_uplift).

    Args:
        strategy: The proposed strategy to assess.
        monthly_sales: Latest monthly sales (売上), JPY. Annualised internally.
        working_capital_gap: Shikin Kuri gap (JPY; negative = deficit). None -> 0.
        rate_curve: BOJ policy-rate curve. None or empty -> rate_stress = 0.
        settlement_metrics: Settlement / liquidity metrics (DSO/DPO). None -> 0.
        industry: Industry label from CompanyProfile. Empty -> no adjustment.

    Returns:
        A :class:`FeasibilityNote` with an empty ``advisory`` and
        ``advisory_grounded=False``.
    """
    annual_sales = max(int(monthly_sales) * MONTHS_PER_YEAR, 1)
    uplift = abs(int(strategy.expected_keijo_uplift))
    gap = working_capital_gap if working_capital_gap is not None else 0

    # --- Four deterministic signals ---
    uplift_ratio = uplift / annual_sales
    wc_stress = max(0, -gap) / annual_sales
    rate_stress = _rate_stress_from_curve(rate_curve or [])
    settle_stress = _settle_stress_from_metrics(settlement_metrics)

    # --- Industry-adjusted uplift weight ---
    w_uplift = _uplift_weight(industry)

    # --- Composite score ---
    raw = (
        100.0
        - w_uplift * uplift_ratio * 100.0
        - FEASIBILITY_WEIGHT_WC * wc_stress * 100.0
        - FEASIBILITY_WEIGHT_RATE * rate_stress * 100.0
        - FEASIBILITY_WEIGHT_SETTLE * settle_stress * 100.0
    )
    score = round(max(0.0, min(100.0, raw)), 2)

    # --- Band ---
    if score >= FEASIBILITY_HIGH_FLOOR:
        band = "high"
    elif score >= FEASIBILITY_MEDIUM_FLOOR:
        band = "medium"
    else:
        band = "low"

    # --- Rationale (deterministic, auditable) ---
    rationale = (
        f"期待経常利益改善（{int(strategy.expected_keijo_uplift):,}円/年）は"
        f"年間売上（{annual_sales:,}円）の{uplift_ratio:.1%}に相当します。"
        f"資金繰りストレス={wc_stress:.3f}、"
        f"金利ストレス={rate_stress:.4f}、"
        f"決済サイクルストレス={settle_stress:.3f}。"
        f"総合スコア={score:.1f}→実現可能性「{band}」。"
    )

    return FeasibilityNote(
        strategy_title=strategy.title,
        achievability=band,
        achievability_score=score,
        rationale=rationale,
        advisory="",
        advisory_grounded=False,
    )


# ---------------------------------------------------------------------------
# Citation-grounding post-check (MR #2).
# ---------------------------------------------------------------------------


def is_advisory_grounded(advisory: str, snippets: list[RetrievalSnippet]) -> bool:
    """Deterministic post-check: is the advisory supported by >= 1 snippet?

    Uses a simple token/source overlap heuristic: the advisory is considered
    grounded when at least one snippet's source label or a significant token
    from its text appears in the advisory text. This is intentionally simple
    and conservative — it is metadata only and never feeds any gate or route.

    Algorithm:
        For each snippet:
          1. If the snippet's source label (e.g. 'past_keikakusho') appears in
             the advisory -> grounded.
          2. If any token from the snippet text with length >= 4 characters
             appears in the advisory -> grounded.
        If no snippet matches -> not grounded.

    Args:
        advisory: The LLM-produced advisory text.
        snippets: Retrieved precedent snippets used to ground the advisory.

    Returns:
        True when the advisory is supported by >= 1 snippet; False otherwise.
    """
    if not advisory or not snippets:
        return False
    advisory_lower = advisory.lower()
    for snippet in snippets:
        # Check source label overlap.
        if snippet.source and snippet.source.lower() in advisory_lower:
            return True
        # Check token overlap (tokens >= 4 chars from snippet text).
        tokens = [t for t in snippet.text.split() if len(t) >= 4]
        if any(token.lower() in advisory_lower for token in tokens):
            return True
    return False


# ---------------------------------------------------------------------------
# LLM advisory pass (best-effort, citation-grounded).
# ---------------------------------------------------------------------------


def _llm_configured(settings: Settings) -> bool:
    """Return whether an LLM is configured for the advisory pass.

    The API key is read through the secret seam (consistent with
    ``kaizen_generation`` and ``embeddings``), so a ``@env:`` / ``@file:`` /
    ``@/path`` reference resolves to its real value before the truthiness check
    — otherwise a referenced key would look configured here but be sent as the
    literal reference string, 401, and silently disable both the advisory AND
    the LLM-vs-floor reconciliation gate.
    """
    return bool(resolve_secret(settings.llm_api_key) and settings.llm_model)


def _load_prompt() -> str:
    """Load the static persona prompt from the registry; empty if unavailable."""
    return get_prompt_or_empty(_PROMPT_NAME)


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

    MR #2: The prompt now explicitly instructs the model to ground its advisory
    in the provided precedents and cite them by source label. The grounding
    check is performed deterministically by the caller after this returns.

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
        precedent_block = "\n".join(f"- [{s.source}] {s.text}" for s in snippets)
        user_content += (
            "\n参考事例（過去計画・ベンチマーク・金融庁指針 / advisory precedents）:\n"
            f"{precedent_block}\n"
            "\n【重要】上記の参考事例を根拠として助言を作成し、"
            "使用した事例のソース（例: [past_keikakusho]）を必ず引用してください。"
        )
    payload = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
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
        return _call_llm(settings, system_prompt, note, strategy, monthly_sales, snippets)
    except Exception as exc:  # noqa: BLE001 - advisory is best-effort
        _log.warning("feasibility.advisory_failed", error=str(exc))
        return ""


def _retrieval_query(strategy: Strategy, company_name: str) -> str:
    """Build the RAG query for a strategy (advisory precedent retrieval)."""
    return f"{company_name} {strategy.title} {strategy.rationale}".strip()


# ---------------------------------------------------------------------------
# LLM feasibility signal extraction (for reconciliation predicate).
# ---------------------------------------------------------------------------


def _clamp_score(value: float) -> float:
    """Clamp a raw numeric score into the inclusive [0, 100] range."""
    return max(0.0, min(100.0, value))


def _parse_signal_array(raw: str, expected: int) -> list[float] | None:
    """Parse a batched LLM response into exactly ``expected`` clamped scores.

    Defensive, multi-strategy parsing so a single malformed response degrades to
    "no reconciliation" (None) rather than misaligning scores with strategies:

    1. Try to locate and ``json.loads`` the first ``[...]`` array in ``raw``
       (tolerates code fences / leading prose). Each element is coerced to a
       float and clamped.
    2. Fall back to line-wise extraction: take the first integer on each line.

    A result is accepted ONLY when it contains exactly ``expected`` scores; any
    other length (over, under, or zero) returns None so the caller treats it as
    a failed signal and leaves ``reconciliation_required`` False.

    Args:
        raw: The raw LLM message content.
        expected: The number of strategies (required result length).

    Returns:
        A list of ``expected`` clamped scores, or None on any mismatch/failure.
    """
    # --- Attempt 1: JSON array anywhere in the response. ---
    array_match = re.search(r"\[.*?\]", raw, re.DOTALL)
    if array_match:
        try:
            parsed = json.loads(array_match.group())
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            scores: list[float] = []
            for item in parsed:
                try:
                    scores.append(_clamp_score(float(item)))
                except (TypeError, ValueError):
                    scores = []
                    break
            if len(scores) == expected:
                return scores

    # --- Attempt 2: first integer on each non-empty line. ---
    line_scores: list[float] = []
    for line in raw.splitlines():
        match = re.search(r"-?\d+(?:\.\d+)?", line)
        if match:
            line_scores.append(_clamp_score(float(match.group())))
    if len(line_scores) == expected:
        return line_scores

    return None


def _call_llm_feasibility_signals(
    settings: Settings,
    notes: list[FeasibilityNote],
    strategies: list[Strategy],
    monthly_sales: int,
) -> list[float] | None:
    """Request feasibility scores (0-100) for ALL strategies in ONE LLM call.

    This is a SEPARATE, minimal call used ONLY for the reconciliation predicate.
    Batching all strategies into a single request keeps the critical-path LLM
    cost at N+1 (one advisory call per strategy plus this one) instead of 2N.

    The LLM supplies numbers; the routing decision is a pure function of
    (deterministic_band, llm_band, RECONCILIATION_BAND_DISTANCE). The LLM
    NEVER decides direction, verdict, or figure.

    Returns a list of scores aligned 1:1 with ``strategies`` (and ``notes``), or
    None on any failure / length mismatch (offline fallback -> reconciliation
    stays False).

    Args:
        settings: Application settings (LLM endpoint / key / model).
        notes: Deterministic feasibility notes (parallel to strategies).
        strategies: The proposed strategies being assessed.
        monthly_sales: Latest monthly sales (JPY).

    Returns:
        A list of len(strategies) clamped scores in [0, 100], or None on failure.
    """
    if not strategies:
        return []

    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    strategy_block = "\n".join(
        (
            f"{i + 1}. 戦略タイトル: {strategy.title}\n"
            f"   根拠: {strategy.rationale}\n"
            f"   期待経常利益改善: {int(strategy.expected_keijo_uplift):,}円/年\n"
            f"   決定論的スコア（参考）: {note.achievability_score}"
        )
        for i, (note, strategy) in enumerate(zip(notes, strategies, strict=True))
    )
    user_content = (
        "以下の各戦略の実現可能性を0〜100の整数で評価してください（100=非常に高い）。\n"
        f"最新月次売上: {int(monthly_sales):,}円\n\n"
        f"{strategy_block}\n\n"
        "【出力形式】上記の順番と同じ順序で、スコアの整数配列のみをJSONで返してください。"
        f"要素数は必ず{len(strategies)}個としてください（例: [72, 41, 88]）。"
    )
    payload = {
        "model": settings.llm_model,
        "temperature": 0.0,
        "max_tokens": 64,
        "messages": [
            {
                "role": "system",
                "content": (
                    "あなたは事業再生コンサルタントです。"
                    "各戦略の実現可能性を0〜100の整数で評価し、"
                    "整数配列のJSONのみを返答してください。"
                ),
            },
            {"role": "user", "content": user_content},
        ],
    }
    headers = {"Authorization": f"Bearer {resolve_secret(settings.llm_api_key)}"}
    try:
        response = httpx.post(
            url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds
        )
        response.raise_for_status()
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()
        return _parse_signal_array(raw, len(strategies))
    except Exception as exc:  # noqa: BLE001 - best-effort
        _log.warning("feasibility.llm_signal_failed", error=str(exc))
    return None


# ---------------------------------------------------------------------------
# Deterministic reconciliation predicate (MR #2).
# ---------------------------------------------------------------------------


def _compute_reconciliation(
    notes: list[FeasibilityNote],
    settings: Settings,
    monthly_sales: int,
    strategies: list[Strategy],
) -> tuple[bool, list[dict[str, Any]]]:
    """Compute the deterministic LLM-vs-floor reconciliation predicate.

    MR #2: For each strategy, requests an LLM feasibility score (best-effort)
    and compares it against the deterministic floor band. If the band distance
    >= RECONCILIATION_BAND_DISTANCE for any strategy, sets reconciliation_required.

    MR #3: Adds a deterministic per-run ceiling (MAX_RECONCILIATION_TRIGGERS) on
    how many disagreements drive routing. ALL qualifying disagreements are recorded
    in reconciliation_details for full audit transparency. The top-N by
    band_distance (descending) are marked routed=True; ties are broken by
    strategy_title ascending for byte-stable deterministic output. This prevents
    a pathological LLM from carpet-bombing the banker with review triggers
    (alert-fatigue / authority-leakage risk) while ensuring the LLM's strongest
    signals always reach the human. 'More power, never more authority.'

    INVARIANT: reconciliation_required is True if and only if at least one
    disagreement qualifies (single-disagreement routing is unchanged from MR #2).
    The ceiling governs how details are prioritised/marked, not whether a single
    disagreement still routes.

    The LLM supplies a number; the routing decision is a PURE DETERMINISTIC
    FUNCTION of (deterministic_band, llm_band, RECONCILIATION_BAND_DISTANCE).
    The LLM NEVER decides direction, verdict, or figure.

    Returns (reconciliation_required, reconciliation_details_as_dicts).
    Offline fallback: returns (False, []) when no LLM is configured.

    Args:
        notes: Deterministic feasibility notes (one per strategy).
        settings: Application settings.
        monthly_sales: Latest monthly sales (JPY).
        strategies: Proposed strategies (parallel to notes).

    Returns:
        Tuple of (reconciliation_required: bool, details: list[dict]).
    """
    if not _llm_configured(settings):
        return False, []

    # Single batched LLM call for ALL strategies (N+1 critical-path cost, not 2N).
    # On any failure / length mismatch this returns None and reconciliation
    # degrades to False (the same safe outcome as the offline path).
    llm_scores = _call_llm_feasibility_signals(settings, notes, strategies, monthly_sales)
    if llm_scores is None:
        return False, []

    # --- Collect ALL qualifying disagreements (full audit trail) ---
    # We record every disagreement regardless of the ceiling so the banker
    # always has the complete picture. The ceiling only governs routed=True.
    qualifying: list[ReconciliationDetail] = []

    for note, strategy, llm_score in zip(notes, strategies, llm_scores, strict=True):
        llm_band = llm_band_from_score(llm_score)
        det_band = note.achievability
        distance = abs(band_ordinal(det_band) - band_ordinal(llm_band))

        if distance >= RECONCILIATION_BAND_DISTANCE:
            # routed=False initially; we set True for top-N below.
            detail = ReconciliationDetail(
                strategy_title=strategy.title,
                deterministic_band=det_band,
                deterministic_score=note.achievability_score,
                llm_band=llm_band,
                llm_score=llm_score,
                band_distance=distance,
                routed=False,
            )
            qualifying.append(detail)

    if not qualifying:
        # No disagreements at all — emit summary and return.
        _log.info(
            "feasibility.reconciliation_summary",
            total_disagreements=0,
            routed_triggers=0,
            strategies=[s.title for s in strategies],
        )
        return False, []

    # --- MR #3: Ranked selection — top-N by band_distance, ties by title asc ---
    # Sort descending by band_distance, then ascending by strategy_title for
    # deterministic tie-breaking (byte-stable output).
    ranked = sorted(
        qualifying,
        key=lambda d: (-d.band_distance, d.strategy_title),
    )

    # Mark the top-N as routed=True (ceiling enforcement).
    routed_titles: set[str] = {d.strategy_title for d in ranked[:MAX_RECONCILIATION_TRIGGERS]}

    # Rebuild details with routed flag set correctly, preserving ranked order
    # (strongest signals first) for the HITL payload.
    details: list[dict[str, Any]] = []
    for detail in ranked:
        updated = detail.model_copy(update={"routed": detail.strategy_title in routed_titles})
        details.append(updated.model_dump())
        if updated.routed:
            _log.info(
                "feasibility.reconciliation_triggered",
                strategy=detail.strategy_title,
                deterministic_band=detail.deterministic_band,
                llm_band=detail.llm_band,
                distance=detail.band_distance,
                routed=True,
            )
        else:
            _log.info(
                "feasibility.reconciliation_ceiling_suppressed",
                strategy=detail.strategy_title,
                deterministic_band=detail.deterministic_band,
                llm_band=detail.llm_band,
                distance=detail.band_distance,
                routed=False,
                ceiling=MAX_RECONCILIATION_TRIGGERS,
            )

    routed_count = len(routed_titles)

    # --- MR #3: Observability summary hook ---
    # Emit a structured log summarising the reconciliation run so the ceiling
    # is monitored. A rising total_disagreements / routed_triggers ratio signals
    # the floor formula or LLM prompt needs tuning.
    _log.info(
        "feasibility.reconciliation_summary",
        total_disagreements=len(qualifying),
        routed_triggers=routed_count,
        strategies=[d["strategy_title"] for d in details],
    )

    # reconciliation_required is True iff at least one disagreement qualifies
    # (invariant: single-disagreement routing unchanged from MR #2).
    return True, details


# ---------------------------------------------------------------------------
# Main node.
# ---------------------------------------------------------------------------


def feasibility_critic_node(
    state: SaiseiState,
    settings: Settings | None = None,
    retrieval: RetrievalProvider | None = None,
) -> dict[str, Any]:
    """Annotate each proposed strategy with an advisory feasibility note.

    MR #2 changes:
    - Deterministic floor uses the multi-factor formula (uplift_ratio,
      wc_stress, rate_stress, settle_stress, industry adjustment).
    - Advisory is citation-grounded: the prompt instructs the LLM to cite
      retrieved precedents; a deterministic post-check sets advisory_grounded.
    - Reconciliation predicate: compares floor band against LLM-derived band;
      sets reconciliation_required and reconciliation_details when they disagree
      by >= RECONCILIATION_BAND_DISTANCE. Offline-safe (no-op when no LLM).

    ADVISORY ONLY: returns ``feasibility_notes``, ``reconciliation_required``,
    and ``reconciliation_details`` only; touches no gate field.

    Args:
        state: Current graph state (reads proposed_strategies, shisanhyo,
               working_capital_gap, boj_rate_curve, settlement_metrics,
               company_profile).
        settings: Optional settings override (defaults to cached settings).
        retrieval: Optional retrieval provider (defaults to the configured one;
            the mock provider returns no precedents offline).

    Returns:
        Partial state update with ``feasibility_notes``,
        ``reconciliation_required``, and ``reconciliation_details``.
    """
    settings = settings or get_settings()
    strategies = state.proposed_strategies

    if not strategies:
        _log.info("feasibility.no_strategies")
        return {
            "feasibility_notes": [],
            "reconciliation_required": False,
            "reconciliation_details": [],
        }

    monthly_sales = int(state.shisanhyo[-1].uriage) if state.shisanhyo else 0
    industry = state.company_profile.industry if state.company_profile else ""

    use_llm = _llm_configured(settings)
    system_prompt = _load_prompt() if use_llm else ""
    # Retrieval only matters when we will actually phrase an advisory note.
    retrieval = retrieval or (get_retrieval_provider(settings) if use_llm else None)
    company_name = state.company_profile.name if state.company_profile else state.tdb_code

    notes: list[FeasibilityNote] = []
    for strategy in strategies:
        note = assess_feasibility(
            strategy,
            monthly_sales,
            working_capital_gap=state.working_capital_gap,
            rate_curve=list(state.boj_rate_curve),
            settlement_metrics=state.settlement_metrics,
            industry=industry,
        )
        # Depth step 4: annotate the note with the deterministic credibility of
        # this strategy's claimed annual uplift against the firm's OWN figures
        # (margin-recovery + cost-reduction + WC-financing headroom), so an
        # over-claimed uplift is visible to the banker BEFORE the recovery curve
        # is trusted. Pure arithmetic over shisanhyo + working_capital_gap;
        # ADVISORY ONLY -- it rides the feasibility-note channel, which the spine
        # proves never feeds a gate, route, or figure. Skipped (fields stay empty)
        # when there is no history to assess.
        if state.shisanhyo:
            credibility = assess_uplift(
                state.shisanhyo,
                int(strategy.expected_keijo_uplift),
                working_capital_gap=state.working_capital_gap,
            )
            # Depth step 4 part 3: deterministic REALISM read -- do the two
            # independent deterministic signals (execution-risk band vs uplift
            # magnitude band) AGREE? A 'high' achievability strategy whose uplift
            # is 'implausible' is internally contradictory (easy to do, but the
            # payoff is fiction) -- exactly the "is this realistic?" question a
            # turnaround consultant asks. Pure function of two already-computed
            # bands; no new LLM, no new constant. ADVISORY ONLY.
            realism_flag, realism_note = assess_realism(note.achievability, credibility.band)
            note = note.model_copy(
                update={
                    "uplift_credibility": credibility.band,
                    "uplift_credibility_ratio": credibility.ratio,
                    "uplift_credibility_reason": credibility.reason,
                    "realism_flag": realism_flag,
                    "realism_note": realism_note,
                }
            )
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
                # Feature 0: gate the advisory through the claim-grounding
                # pipeline before it can reach the banker. Any sentence whose
                # citation does not resolve to a deterministic signal or a
                # retrieved precedent (or that the evidence does not entail) is
                # stripped (calibrated abstention). The cleaned text is what we
                # store; an empty result means nothing was attributable.
                packet = build_evidence_packet(state, source_labels=[s.source for s in snippets])
                evidence_texts = {s.source: s.text for s in snippets}
                grounded_advisory = ground_qualitative_text(
                    advisory,
                    packet,
                    evidence_texts=evidence_texts,
                    settings=settings,
                )
                clean = grounded_advisory.text
                # Feature 0 phase 4 (provenance in the UI): also run the pipeline
                # in FLAG mode to capture a complete per-sentence provenance map
                # — every claim retained and labelled grounded / unverified — so
                # the banker can SEE which claims are attributable and to what.
                # The stored ``advisory`` stays the strip-mode (abstaining) text;
                # this second pass only produces display metadata and never
                # changes the surfaced text, a gate, a route, or a figure.
                flagged = ground_qualitative_text(
                    advisory,
                    packet,
                    evidence_texts=evidence_texts,
                    flag=True,
                    settings=settings,
                )
                provenance = [
                    {
                        "text": entry.text,
                        "status": entry.status,
                        "citations": list(entry.citations),
                    }
                    for entry in flagged.provenance
                ]
                note = note.model_copy(
                    update={
                        "advisory": clean,
                        "advisory_grounded": bool(clean) and grounded_advisory.fully_grounded,
                        "advisory_provenance": provenance,
                    }
                )
                _log.info(
                    "feasibility.advisory_grounded",
                    strategy=strategy.title,
                    grounded=grounded_advisory.fully_grounded,
                    stripped=advisory != clean,
                    claims=len(provenance),
                )
        notes.append(note)

    # --- Deterministic reconciliation predicate (MR #2) ---
    reconciliation_required, reconciliation_details = _compute_reconciliation(
        notes, settings, monthly_sales, strategies
    )

    notes_dicts = [n.model_dump() for n in notes]

    _log.info(
        "feasibility.assessed",
        strategies=len(strategies),
        llm=use_llm,
        bands=[n["achievability"] for n in notes_dicts],
        reconciliation_required=reconciliation_required,
    )
    return {
        "feasibility_notes": notes_dicts,
        "reconciliation_required": reconciliation_required,
        "reconciliation_details": reconciliation_details,
    }
