"""Saisei companion — an advisory, read-only co-pilot for the banker.

The summonable companion (“再生の精 / Saisei spirit” in the UI) answers free-form
banker questions about the *current* case: explain a figure, explain why a
classification landed where it did, or compare this borrower to a similar past
one. It is the interactive surface over machinery the stack already has — the
deterministic ``SaiseiState`` figures and the advisory two-tier RAG seam.

Why this is safe (the same invariant the rest of Saisei enforces)
----------------------------------------------------------------
Free-form Q&A is exactly where an LLM is most tempted to assert ungrounded
numbers, so the companion is built to be *provably incapable* of two things:

1. **Casting a hidden vote.** It is strictly READ-ONLY. It receives a snapshot
   of the finalized state and returns text; it never returns a state delta, and
   nothing here writes a gate, route, figure, or verdict. The banker remains the
   only decider — the companion explicitly defers the decision.
2. **Lying with a number.** Every qualitative sentence it emits is routed through
   the existing :mod:`app.backend.analysis.claim_grounding` gate (flag mode)
   against an :class:`~app.backend.analysis.claim_grounding.EvidencePacket` built
   from the deterministic signal keys in state plus the source labels of any
   retrieved precedent. An unattributable assertion is therefore visibly marked
   【未検証 / unverified】rather than presented as fact.

Retrieval reuses the **existing** seam (:func:`app.backend.tools.retrieval.
get_retrieval_provider`) — no new vector store, offline-green by default (the
mock provider returns no precedent, so the companion simply answers from the
deterministic figures).

Determinism
-----------
v1 composes its answer deterministically (intent routing + templated, cited
sentences over the state figures and retrieved snippets) with no LLM dependency,
so ``make verify`` stays offline and the answer is replayable. A later phase may
let an LLM compose prose *on top of the same evidence packet* without changing
the grounding gate — the gate already covers any qualitative text, LLM or not.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.backend.analysis.claim_grounding import (
    ClaimGroundingResult,
    EvidencePacket,
    check_claims_grounded,
)
from app.backend.tools.retrieval import (
    RetrievalProvider,
    RetrievalSnippet,
    get_retrieval_provider,
)
from app.shared.logging import get_logger
from app.shared.models.classification import FsaClass
from app.shared.models.money import format_jpy

__all__ = [
    "ChatIntent",
    "CompanionAnswer",
    "answer_question",
    "build_evidence_packet",
    "classify_intent",
]

_log = get_logger(__name__)

#: Max precedent snippets to retrieve / cite for a single answer. Kept small so
#: the answer stays a focused advisory, not a dump.
_TOP_K = 3


class ChatIntent(StrEnum):
    """The deterministic intent a banker question is routed to."""

    #: "Compare this case to a similar past one" — precedent retrieval.
    COMPARE = "compare"
    #: "Why is this classified … / explain the assessment" — deterministic basis.
    EXPLAIN = "explain"
    #: "What is the EWS / working-capital gap / …" — read a figure from state.
    FIGURE = "figure"
    #: Anything else — a grounded summary of the case + an offer to go deeper.
    SUMMARY = "summary"


#: Marker prefix the composer attaches to a deliberately non-factual *framing*
#: line (a lead-in, an honest abstention, or the advisory deferral). These lines
#: carry words but assert nothing about the case, so they must NOT be routed
#: through the claim-grounding gate (which would otherwise split a bilingual
#: framing line on its 。/. terminators into word-bearing, citation-free
#: fragments and flag them 【未検証 / unverified】, falsely flipping the answer to
#: ungrounded). The marker is internal only and is stripped before the line is
#: surfaced, so it never reaches the banker.
_FRAMING_TAG = "\x00framing\x00"


def _framing(line: str) -> str:
    """Tag ``line`` as a non-factual framing line (internal marker)."""
    return f"{_FRAMING_TAG}{line}"


def _is_framing(line: str) -> bool:
    """Return whether ``line`` was tagged as framing by :func:`_framing`."""
    return line.startswith(_FRAMING_TAG)


def _strip_framing(line: str) -> str:
    """Remove the internal framing marker from a line for display."""
    return line[len(_FRAMING_TAG) :] if _is_framing(line) else line


@dataclass(frozen=True)
class CompanionAnswer:
    """A grounded answer from the companion (advisory, read-only).

    Attributes:
        intent: The intent the question was routed to.
        text: The grounded answer text. Ungrounded sentences are marked
            【未検証 / unverified】 (flag mode) rather than stripped, so the banker
            sees every claim and its attribution status.
        grounded: True iff every claim resolved against the evidence packet.
        citations: The distinct evidence ids the answer cited, in order.
        precedents: The retrieved precedent snippets surfaced (may be empty).
    """

    intent: ChatIntent
    text: str
    grounded: bool
    citations: list[str] = field(default_factory=list)
    precedents: list[RetrievalSnippet] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Intent routing (deterministic, language-tolerant)
# ---------------------------------------------------------------------------

#: Keyword sets per intent, JA + EN. Matched case-insensitively as substrings
#: so the router is robust to natural phrasing without an NLP dependency.
_COMPARE_KEYS = (
    "類似",
    "似た",
    "比較",
    "事例",
    "先例",
    "過去",
    "similar",
    "compare",
    "precedent",
    "past case",
    "like this",
)
_EXPLAIN_KEYS = (
    "なぜ",
    "どうして",
    "理由",
    "根拠",
    "説明",
    "区分",
    "why",
    "explain",
    "reason",
    "how come",
    "basis",
    "rationale",
)
_FIGURE_KEYS = (
    "ews",
    "スコア",
    "資金繰り",
    "ギャップ",
    "数値",
    "保証",
    "係数",
    "score",
    "gap",
    "figure",
    "number",
    "working capital",
    "guarantee",
)


def classify_intent(question: str) -> ChatIntent:
    """Route a banker question to a :class:`ChatIntent` deterministically.

    Compare is checked first (it is the most specific, highest-value ask), then
    explain, then a bare figure lookup; everything else is a grounded summary.

    Args:
        question: The raw banker question.

    Returns:
        The matched :class:`ChatIntent`.
    """
    q = question.strip().lower()
    if any(k in q for k in _COMPARE_KEYS):
        return ChatIntent.COMPARE
    if any(k in q for k in _EXPLAIN_KEYS):
        return ChatIntent.EXPLAIN
    if any(k in q for k in _FIGURE_KEYS):
        return ChatIntent.FIGURE
    return ChatIntent.SUMMARY


# ---------------------------------------------------------------------------
# Evidence packet from state
# ---------------------------------------------------------------------------


def _state_get(state: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read a key from the snapshot mapping (rehydration-safe, dict-shaped)."""
    return state.get(key, default)


#: The deterministic signal keys the companion may cite, mapped to a short JA/EN
#: label used when composing a sentence about that figure. Only keys actually
#: present (non-None) in the snapshot become part of the evidence packet, so a
#: sentence can never cite a figure the case does not have.
_SIGNAL_LABELS: dict[str, str] = {
    "ews_score": "EWSスコア (EWS score)",
    "fsa_classification": "債務者区分 (FSA classification)",
    "working_capital_gap": "資金繰りギャップ (working-capital gap)",
    "hosho_kaijo_score": "経営者保証解除スコア (guarantee-release score)",
    "classification_reason": "区分の根拠 (classification basis)",
}


def build_evidence_packet(
    state: Mapping[str, Any], precedents: list[RetrievalSnippet]
) -> EvidencePacket:
    """Build the grounding evidence packet for the current case.

    The packet is the ground truth the answer is allowed to assert: the
    deterministic signal keys actually present in the snapshot, plus the
    ``source`` label of every retrieved precedent snippet. A citation in the
    answer resolves iff its id is one of these.

    Args:
        state: The finalized graph state snapshot (dict-shaped).
        precedents: Retrieved precedent snippets (their source labels become
            citable source ids).

    Returns:
        A frozen :class:`EvidencePacket`.
    """
    signal_keys = [key for key in _SIGNAL_LABELS if _state_get(state, key) is not None]
    source_labels = [snip.source for snip in precedents if snip.source]
    return EvidencePacket.build(signal_keys=signal_keys, source_labels=source_labels)


# ---------------------------------------------------------------------------
# Figure formatting helpers (read-only; format already-computed values)
# ---------------------------------------------------------------------------


def _fsa_kanji(value: Any) -> str:
    """Return the FSA classification kanji for an enum / str / None."""
    if not value:
        return "—"
    if isinstance(value, FsaClass):
        return value.kanji
    try:
        return FsaClass(str(value)).kanji
    except ValueError:
        return str(value)


def _figure_sentences(state: Mapping[str, Any]) -> list[str]:
    """Build cited, one-per-figure sentences for the figures present in state.

    Each sentence carries a ``[<signal_key>]`` citation so the grounding gate
    can resolve it. Only figures actually present are emitted, so nothing is
    asserted that the case does not have. Pure formatting of computed values.
    """
    lines: list[str] = []
    ews = _state_get(state, "ews_score")
    if ews is not None:
        lines.append(f"EWSスコアは {float(ews):.1f} です。[ews_score]")
    fsa = _state_get(state, "fsa_classification")
    if fsa is not None:
        lines.append(f"債務者区分は {_fsa_kanji(fsa)} です。[fsa_classification]")
    gap = _state_get(state, "working_capital_gap")
    if gap is not None:
        lines.append(f"資金繰りギャップは {format_jpy(int(gap))} です。[working_capital_gap]")
    hosho = _state_get(state, "hosho_kaijo_score")
    if hosho is not None:
        lines.append(f"経営者保証解除スコアは {float(hosho):.1f} です。[hosho_kaijo_score]")
    return lines


def _precedent_sentences(precedents: list[RetrievalSnippet]) -> list[str]:
    """Build cited sentences surfacing each retrieved precedent passage.

    Each sentence cites the snippet's ``source`` id so it resolves against the
    evidence packet (whose source labels were taken from these same snippets).
    The snippet *text* is UNTRUSTED (T3 ambient: a retrieved document the bank
    may have ingested from anywhere), so it is sanitised first — see
    :func:`_sanitise_untrusted` — so a precedent can never smuggle in a fake
    ``[citation]`` tag or sentence-splitting punctuation that would let it ride
    into the answer as a (falsely) grounded claim.
    """
    lines: list[str] = []
    for snip in precedents:
        text = _collapse(_sanitise_untrusted(snip.text))
        lines.append(f"参考事例: {text} [{snip.source}]")
    return lines


#: Characters that carry meaning to the grounding gate and must be stripped from
#: untrusted snippet text: the citation brackets ``[ ]`` (so a snippet cannot
#: forge a ``[ews_score]`` tag) and the sentence terminators (so a snippet
#: cannot split itself into a second, uncited — and therefore stripped, or
#: falsely-grounded — sentence inside the line we build around it).
_UNTRUSTED_STRIP = re.compile(r"[\[\]。！？.!?\n]")


def _sanitise_untrusted(text: str) -> str:
    """Neutralise grounding-control characters in untrusted snippet text.

    Retrieved precedent is T3-ambient untrusted data. Before it is composed into
    an answer sentence we strip the characters that mean something to the
    grounding gate (citation brackets + sentence terminators), so the snippet is
    inert prose that can only appear inside the single cited sentence we wrap
    around it — never as its own forged-grounded claim. Defence in depth: the
    evidence packet already only contains the snippet's real ``source`` label,
    so a forged tag would not resolve anyway; this removes the ambiguity at the
    source.
    """
    return _UNTRUSTED_STRIP.sub(" ", text)


def _collapse(text: str, limit: int = 220) -> str:
    """Collapse whitespace and clamp a precedent passage for a chat bubble."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


#: The closing line on every answer: the companion proposes, the banker decides.
#: It carries no citation by design, so the grounding gate treats it as a
#: non-claim (no factual assertion) and passes it through verbatim.
_DEFERRAL = (
    "これは助言です。最終的な判断は担当者にあります。 (Advisory only — the decision is yours.)"
)


# ---------------------------------------------------------------------------
# Optional LLM prose pass (B1: rephrase, never re-author) — opt-in, fail-safe
# ---------------------------------------------------------------------------
#
# The deterministic, cited answer is ALWAYS the source of truth. When (and only
# when) an LLM is configured, this pass rewrites that answer for readability
# while being instructed to preserve every ``[citation]`` tag and every figure
# verbatim. The rewrite is then re-run through the SAME claim-grounding gate
# against the SAME evidence packet: it is accepted only if it introduces no new
# ungrounded claim relative to the deterministic baseline. Any failure, missing
# config, or grounding regression falls back to the deterministic text — exactly
# the stance of ``polish_keikakusho`` (LLM for language, never for facts). This
# keeps make verify / CI offline and the answer fail-safe.

_REPHRASE_SYSTEM_PROMPT = (
    "You are a Japanese regional-bank credit officer's assistant. Rewrite the "
    "following advisory answer so it reads naturally and professionally for a "
    "banker. STRICT RULES: preserve EVERY bracketed citation tag (e.g. "
    "[ews_score], [past_keikakusho]) exactly and attached to the same claim; "
    "preserve EVERY number, currency figure, and the FSA classification exactly; "
    "do not add any new fact, figure, or claim that is not already present; keep "
    "the final advisory/deferral line. Respond with the rewritten answer only."
)


def _llm_configured(settings: Any) -> bool:
    """Return whether an LLM is configured (mirrors the Keikakusho polish gate).

    Reads the key through the shared secret seam (app.backend.llm.llm_configured)
    so a @env:/@file:/@/path reference resolves before the truthiness check.
    Otherwise a referenced key would look configured here but be sent as the
    literal reference string, 401, and silently disable the prose pass.
    """
    from app.backend.llm import llm_configured

    return llm_configured(settings)


def _call_rephrase_llm(settings: Any, text: str) -> str:
    """Rephrase ``text`` via an OpenAI-compatible Chat Completions endpoint.

    Mirrors ``kaizen_generation._call_llm``: same transport, low temperature,
    raises on an empty/odd response so the caller can fall back.
    """
    import httpx

    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _REPHRASE_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    from app.backend.llm import llm_auth_headers

    headers = llm_auth_headers(settings)
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


def _maybe_rephrase(
    deterministic_text: str,
    packet: EvidencePacket,
    baseline: ClaimGroundingResult,
    *,
    settings: Any | None,
) -> ClaimGroundingResult:
    """Optionally rewrite the answer via LLM, re-grounded; else the baseline.

    B1 “rephrase, never re-author”: returns a grounding result over the LLM
    rewrite ONLY when an LLM is configured AND the rewrite introduces no new
    ungrounded claim versus ``baseline`` (the deterministic answer's grounding
    result). Otherwise returns ``baseline`` unchanged. Fail-safe and offline:
    unconfigured / any error / grounding regression -> the deterministic text.

    Args:
        deterministic_text: The cited deterministic answer (source of truth).
        packet: The evidence packet the answer must remain grounded against.
        baseline: The grounding result of ``deterministic_text`` (flag mode).
        settings: Settings (or None to load cached); the LLM-config gate.

    Returns:
        A :class:`ClaimGroundingResult` — over the accepted rewrite, or baseline.
    """
    if settings is None:
        from app.shared.settings import get_settings

        settings = get_settings()
    if not _llm_configured(settings):
        return baseline
    try:
        rewritten = _call_rephrase_llm(settings, deterministic_text)
    except Exception as exc:  # noqa: BLE001 - rephrase is best-effort
        _log.warning("companion.rephrase_failed", error=str(exc))
        return baseline

    # Re-ground the rewrite against the SAME packet (flag mode). Accept it only
    # if it does not REGRESS grounding: the rewrite must not introduce more
    # ungrounded claims than the deterministic baseline already had. This is the
    # numeric-preservation stance generalised to claims — the LLM may improve
    # prose, never weaken attribution.
    candidate = check_claims_grounded(rewritten, packet, flag=True)
    if len(candidate.ungrounded) > len(baseline.ungrounded):
        _log.warning(
            "companion.rephrase_grounding_regressed",
            baseline_ungrounded=len(baseline.ungrounded),
            candidate_ungrounded=len(candidate.ungrounded),
        )
        return baseline
    _log.info("companion.rephrase_applied", chars=len(candidate.cleaned_text))
    return candidate


# ---------------------------------------------------------------------------
# Answer composition
# ---------------------------------------------------------------------------


def answer_question(
    question: str,
    state: Mapping[str, Any],
    *,
    provider: RetrievalProvider | None = None,
    settings: Any | None = None,
) -> CompanionAnswer:
    """Answer a banker question about the current case (advisory, read-only).

    Deterministic and grounded: routes the question to an intent, composes cited
    sentences over the deterministic figures in ``state`` and any retrieved
    precedent, then passes the whole text through the claim-grounding gate in
    *flag* mode so unattributable claims are marked 【未検証 / unverified】 rather
    than presented as fact. Returns text only — never a state delta — so it
    cannot move a gate, route, figure, or verdict.

    When an LLM is configured, an OPTIONAL prose pass rewrites the deterministic
    answer for readability and is re-grounded against the same evidence packet
    (B1 “rephrase, never re-author”); it is accepted only if it does not weaken
    grounding, and falls back to the deterministic text otherwise. Offline /
    unconfigured, the answer is byte-identical to the deterministic version.

    Args:
        question: The banker's free-form question.
        state: The finalized graph state snapshot (dict-shaped).
        provider: Optional retrieval provider override (defaults to the
            configured two-tier seam; offline that yields no precedent).
        settings: Optional settings override (defaults to cached settings); the
            LLM-prose gate reads it.

    Returns:
        A :class:`CompanionAnswer` with grounded text + provenance.
    """
    intent = classify_intent(question)
    provider = provider or get_retrieval_provider()

    # Retrieve precedent only for the intents that use it (compare / summary),
    # so a pure figure/explain answer does no network work. Best-effort: the
    # provider degrades to [] on any failure (offline default).
    precedents: list[RetrievalSnippet] = []
    if intent in (ChatIntent.COMPARE, ChatIntent.SUMMARY):
        try:
            precedents = list(provider.search(question, _TOP_K))
        except Exception as exc:  # noqa: BLE001 - retrieval is best-effort
            _log.warning("companion.retrieval_failed", error=str(exc))
            precedents = []

    packet = build_evidence_packet(state, precedents)
    # The deferral is framing (advisory, non-factual) and is always last.
    lines = [*_compose_lines(intent, state, precedents), _framing(_DEFERRAL)]

    # Split deliberate FRAMING lines (lead-ins / abstentions / deferral) from
    # CLAIM lines. Only claim lines are routed through the grounding gate: a
    # framing line carries words but asserts nothing about the case, and the
    # JP sentence-splitter would otherwise break a bilingual framing line into
    # word-bearing, citation-free fragments and flag them unverified -- falsely
    # flipping the answer to ungrounded. Framing lines are kept verbatim, in
    # their original positions (pre-claim lead-ins, post-claim deferral), so the
    # banker still sees the framing prose.
    pre: list[str] = []
    claim_lines: list[str] = []
    post: list[str] = []
    seen_claim = False
    for line in lines:
        if _is_framing(line):
            (post if seen_claim else pre).append(_strip_framing(line))
        else:
            seen_claim = True
            claim_lines.append(line)

    # Flag mode (not strip): the banker should SEE every claim and its status,
    # so an unattributable sentence is kept and visibly marked unverified.
    result = check_claims_grounded("\n".join(claim_lines), packet, flag=True)

    # Optional LLM prose pass (opt-in, fail-safe): rewrite the CLAIM text for
    # readability and re-ground against the same packet; accepted only if
    # grounding does not regress, else the deterministic result stands.
    result = _maybe_rephrase(result.cleaned_text, packet, result, settings=settings)

    # Reassemble: pre-claim framing, the grounded claim block, post-claim
    # framing (the deferral). Empty segments are skipped so spacing stays clean.
    segments = [*pre, result.cleaned_text, *post]
    final_text = " ".join(seg for seg in segments if seg).strip()

    # Strip the bracketed [citation] tags from the DISPLAY text. The tags are
    # the grounding gate's internal attribution markers, surfaced to the banker
    # structurally via ``citations`` (below) -- not as raw [tag] markup in the
    # prose. Stripping here also closes the last gap in the untrusted-precedent
    # hardening: even if a forged tag survived sanitisation, it can never appear
    # as a literal citation in the answer the banker reads. Whitespace left by a
    # removed tag is collapsed so the prose stays clean.
    final_text = re.sub(r"\s*\[[^\[\]]+\]", "", final_text)
    final_text = re.sub(r"\s{2,}", " ", final_text).strip()

    citations: list[str] = []
    for prov in result.provenance:
        for cid, _tier in prov.resolved:
            if cid not in citations:
                citations.append(cid)

    _log.info(
        "companion.answer",
        intent=str(intent),
        grounded=result.grounded,
        precedents=len(precedents),
        citations=len(citations),
    )
    return CompanionAnswer(
        intent=intent,
        text=final_text,
        grounded=result.grounded,
        citations=citations,
        precedents=precedents,
    )


def _compose_lines(
    intent: ChatIntent,
    state: Mapping[str, Any],
    precedents: list[RetrievalSnippet],
) -> list[str]:
    """Compose the cited answer lines for an intent (pre-grounding).

    Pure: builds templated, individually-cited sentences over the deterministic
    figures and the retrieved precedent. The grounding gate is applied by the
    caller; composing cited sentences here keeps every assertion attributable by
    construction.
    """
    figures = _figure_sentences(state)

    if intent is ChatIntent.FIGURE:
        return figures or [_framing(_NO_FIGURES)]

    if intent is ChatIntent.EXPLAIN:
        lines: list[str] = []
        reason = _state_get(state, "classification_reason")
        if reason:
            lines.append(f"{_collapse(str(reason))} [classification_reason]")
        # Anchor the explanation in the figures that drove the band.
        lines.extend(figures)
        return lines or [_framing(_NO_FIGURES)]

    if intent is ChatIntent.COMPARE:
        if not precedents:
            # Honest abstention: no precedent to compare against (the offline /
            # empty-corpus default). Better to say so than to invent a parallel.
            return [_framing(_NO_PRECEDENT), *figures]
        return [_framing(_COMPARE_LEAD), *_precedent_sentences(precedents), *figures]

    # SUMMARY: the figures, plus any precedent we happened to retrieve.
    summary = [_framing(_SUMMARY_LEAD), *figures]
    if precedents:
        summary.extend(_precedent_sentences(precedents))
    return summary


#: Lead-ins / abstentions. These are non-claims (no citation) and pass the gate
#: verbatim; they frame the cited sentences that follow.
_SUMMARY_LEAD = "本件の要点は以下のとおりです。 (Here is the case at a glance:)"
_COMPARE_LEAD = "類似する過去事例を提示します。 (Comparable past cases:)"
_NO_PRECEDENT = (
    "類似事例は見つかりませんでした。 (No comparable precedent was found in the corpus.)"
)
_NO_FIGURES = (
    "まだ診断が実行されていません。まず診断を実行してください。 "
    "(No assessment has run yet — run one first.)"
)
