"""Faithfulness / groundedness scoring for retained claims (Feature 0 phase 3).

The phase-2 citation verifier (:mod:`app.backend.analysis.claim_grounding`)
guarantees every retained claim *carries a citation that resolves* to real
ground truth. It does NOT guarantee the claim is actually *entailed by* that
evidence — a sentence can cite ``[ews]`` and still assert something the EWS
figure does not support. Phase 3 catches what citations miss: it scores whether
each retained claim is genuinely supported by its cited evidence and demotes
low-faithfulness claims to *unverified* rather than presenting them as analysis.

Design (mirrors the rest of the stack)
--------------------------------------
- **Offline-first, deterministic fallback.** With no LLM configured, a pure
  lexical-overlap entailment proxy scores each claim against the concatenated
  text of its cited evidence. No network; ``make verify`` stays green and the
  result is byte-stable.
- **Optional LLM-as-judge.** When an LLM is configured, a strictly-scoped judge
  answers ONLY "is claim X supported by evidence Y?" (a Ragas-style faithfulness
  question), never an open-ended generation. Best-effort: any failure falls back
  to the deterministic proxy, so the judge can never break the workflow.
- **Advisory boundary preserved.** This demotes claims (marks them unverified);
  it never edits a figure, gate, route, or the deterministic verdict.

The judge is given the claim plus a map of ``citation_id -> evidence_text`` so it
scores against the SAME ground truth the citation verifier resolved against.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from app.backend.secrets import resolve_secret
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "FaithfulnessResult",
    "ClaimFaithfulness",
    "DEFAULT_FAITHFULNESS_FLOOR",
    "lexical_overlap_score",
    "score_claim_faithfulness",
    "score_claims",
]

_log = get_logger(__name__)

#: Default minimum faithfulness score [0, 1] for a claim to remain "verified".
#: Below this, a claim is demoted to unverified. Conservative but not punitive;
#: tune via the eval harness as the corpus grows.
DEFAULT_FAITHFULNESS_FLOOR = 0.30

#: Latin/numeric word tokeniser for the deterministic proxy: runs >= 2 chars.
#: CJK has no word spacing, so Latin tokenisation alone makes any two distinct
#: Japanese sentences score 0 overlap; CJK is handled separately via character
#: bigrams (see ``_tokens``). Citation tags are stripped before tokenising.
_TOKEN = re.compile(r"[0-9A-Za-z]{2,}")

#: Single CJK ideograph / kana code points. Consecutive CJK characters are
#: turned into overlapping character bigrams so two Japanese sentences that
#: share wording (but not exact form) get a meaningful, deterministic overlap.
_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]")

#: High-frequency Latin function words carry no entailment signal but, being
#: shared by almost any two sentences, badly inflate token-recall (e.g. "the",
#: "is"). Dropping them keeps the proxy honest: an unsupported claim that merely
#: shares stopwords with its evidence scores ~0, not above the floor. Japanese
#: particles are not enumerated here — the CJK bigram scheme already dilutes their
#: effect, and a hand-rolled JP stoplist would be brittle.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "as",
        "and",
        "or",
        "but",
        "if",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "an",
        "so",
        "will",
        "would",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "not",
        "no",
    }
)

#: Strip ``[<id>]`` citation tags before scoring so the tag text itself does not
#: inflate the overlap.
_CITATION_TAG = re.compile(r"\[[^\[\]]+\]")


def _cjk_bigrams(text: str) -> set[str]:
    """Overlapping character bigrams over each run of CJK characters.

    A single isolated CJK character also contributes itself as a unigram so a
    one-character claim is not unscoreable.
    """
    grams: set[str] = set()
    for run in re.findall(rf"{_CJK_CHAR.pattern}+", text):
        if len(run) == 1:
            grams.add(run)
            continue
        for i in range(len(run) - 1):
            grams.add(run[i : i + 2])
    return grams


def _tokens(text: str) -> set[str]:
    """Lower-cased token set (Latin words + CJK bigrams), citation tags removed.

    Latin stopwords are dropped so shared function words cannot inflate the
    overlap; CJK character bigrams are added so Japanese claims are scoreable.
    """
    stripped = _CITATION_TAG.sub(" ", text)
    tokens = {
        m.group(0).lower()
        for m in _TOKEN.finditer(stripped)
        if m.group(0).lower() not in _STOPWORDS
    }
    tokens |= _cjk_bigrams(stripped)
    return tokens


def lexical_overlap_score(claim: str, evidence: str) -> float:
    """Deterministic entailment proxy: token recall of the claim in the evidence.

    Returns the fraction of the claim's content tokens that also appear in the
    evidence text — a simple, transparent, offline stand-in for an entailment
    model. Higher = more of the claim is lexically supported by its evidence.

    This is intentionally a *recall* of claim tokens (not Jaccard): a short claim
    fully covered by a long evidence passage should score high. Empty claim or
    empty evidence scores 0.0.

    Args:
        claim: The claim sentence (citation tags are ignored).
        evidence: The concatenated text of the claim's cited evidence.

    Returns:
        A score in [0.0, 1.0].
    """
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return 0.0
    evidence_tokens = _tokens(evidence)
    if not evidence_tokens:
        return 0.0
    covered = len(claim_tokens & evidence_tokens)
    return covered / len(claim_tokens)


@dataclass(frozen=True)
class ClaimFaithfulness:
    """Faithfulness outcome for a single claim.

    Attributes:
        claim: The claim sentence scored.
        score: Faithfulness score in [0, 1].
        faithful: Whether ``score >= floor`` (kept verified) vs demoted.
        method: ``"llm"`` or ``"lexical"`` — which scorer produced ``score``.
    """

    claim: str
    score: float
    faithful: bool
    method: str = "lexical"


@dataclass(frozen=True)
class FaithfulnessResult:
    """Aggregate faithfulness outcome over a set of claims.

    Attributes:
        claims: Per-claim faithfulness records, in input order.
        floor: The faithfulness floor applied.
    """

    claims: list[ClaimFaithfulness] = field(default_factory=list)
    floor: float = DEFAULT_FAITHFULNESS_FLOOR

    @property
    def all_faithful(self) -> bool:
        """True iff every claim met the floor (nothing demoted)."""
        return all(c.faithful for c in self.claims)

    @property
    def demoted(self) -> list[ClaimFaithfulness]:
        """Claims that fell below the floor and were demoted to unverified."""
        return [c for c in self.claims if not c.faithful]


def _llm_configured(settings: Settings) -> bool:
    """Return whether an LLM-as-judge is configured.

    The key is read through the secret seam (consistent with kaizen_generation /
    embeddings / feasibility), so a ``@env:`` / ``@file:`` / ``@/path`` reference
    resolves before the truthiness check. Otherwise a referenced key would look
    configured here but be sent as the literal reference string, 401, and
    silently demote every claim to the weaker lexical proxy — quietly weakening
    the faithfulness safety guarantee on exactly the deployments that use the
    secret seam.
    """
    return bool(resolve_secret(settings.llm_api_key) and settings.llm_model)


def _judge_llm(settings: Settings, claim: str, evidence: str) -> float | None:
    """Ask the constrained LLM judge: is ``claim`` supported by ``evidence``?

    The judge is strictly scoped to a faithfulness question and must return a
    single number in [0, 1]. Best-effort: returns None on any transport/shape
    error so the caller falls back to the deterministic proxy.
    """
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "temperature": 0.0,
        "max_tokens": 8,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict faithfulness judge. Given a CLAIM and its "
                    "EVIDENCE, output ONLY a number between 0 and 1: the degree "
                    "to which the evidence supports (entails) the claim. "
                    "1 = fully supported, 0 = unsupported or contradicted. "
                    "Do not explain. Output the number only."
                ),
            },
            {
                "role": "user",
                "content": f"CLAIM:\n{claim}\n\nEVIDENCE:\n{evidence}",
            },
        ],
    }
    headers = {"Authorization": f"Bearer {resolve_secret(settings.llm_api_key)}"}
    try:
        response = httpx.post(
            url, json=payload, headers=headers, timeout=settings.llm_timeout_seconds
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"-?\d+(?:\.\d+)?", str(raw))
        if not match:
            return None
        return max(0.0, min(1.0, float(match.group())))
    except Exception as exc:  # noqa: BLE001 - judge is best-effort
        _log.warning("faithfulness.judge_failed", error=str(exc))
        return None


def score_claim_faithfulness(
    claim: str,
    evidence: str,
    *,
    floor: float = DEFAULT_FAITHFULNESS_FLOOR,
    settings: Settings | None = None,
) -> ClaimFaithfulness:
    """Score one claim against its evidence (LLM judge if configured, else proxy).

    Args:
        claim: The claim sentence.
        evidence: Concatenated text of the claim's cited evidence.
        floor: Minimum score to remain verified.
        settings: Optional settings override.

    Returns:
        A :class:`ClaimFaithfulness` record.
    """
    settings = settings or get_settings()
    score: float | None = None
    method = "lexical"
    if _llm_configured(settings):
        score = _judge_llm(settings, claim, evidence)
        if score is not None:
            method = "llm"
    if score is None:
        score = lexical_overlap_score(claim, evidence)
        method = "lexical"
    return ClaimFaithfulness(
        claim=claim,
        score=round(score, 4),
        faithful=score >= floor,
        method=method,
    )


def score_claims(
    claim_evidence: Mapping[str, str],
    *,
    floor: float = DEFAULT_FAITHFULNESS_FLOOR,
    settings: Settings | None = None,
) -> FaithfulnessResult:
    """Score a batch of (claim -> evidence) pairs.

    Args:
        claim_evidence: Ordered mapping of claim sentence -> its evidence text.
        floor: Minimum score to remain verified.
        settings: Optional settings override.

    Returns:
        A :class:`FaithfulnessResult` over all claims.
    """
    settings = settings or get_settings()
    records = [
        score_claim_faithfulness(claim, evidence, floor=floor, settings=settings)
        for claim, evidence in claim_evidence.items()
    ]
    return FaithfulnessResult(claims=records, floor=floor)
