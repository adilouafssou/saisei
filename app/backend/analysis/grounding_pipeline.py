"""End-to-end claim-grounding pipeline (Feature 0, the layered defense).

This is the single entry point every qualitative-LLM call site uses to make its
output safe to surface to a banker. It chains the Feature 0 phases in order of
leverage:

  phase 2  citation verifier   — strip/flag any sentence whose citation does not
                                 resolve against the evidence packet;
  phase 3  faithfulness gate   — of the citation-grounded sentences, demote any
                                 the cited evidence does not actually entail;
  phase 4  provenance          — return a per-sentence provenance map for the UI;
  phase 5  calibrated abstain  — the default mode OMITS unbacked claims rather
                                 than asserting them.

Offline contract (the one rule still governs): with no LLM configured the input
text is empty (advisory generators return ``""``), so the whole pipeline is a
no-op returning ``("", grounded-empty-result)``. It never edits a figure, gate,
route, or the deterministic verdict — it only cleans/annotates qualitative prose.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.backend.analysis.claim_grounding import (
    UNVERIFIED_MARKER,
    ClaimGroundingResult,
    ClaimVerdict,
    EvidencePacket,
    check_claims_grounded,
)
from app.backend.analysis.faithfulness import (
    DEFAULT_FAITHFULNESS_FLOOR,
    FaithfulnessResult,
    score_claims,
)
from app.shared.settings import Settings, get_settings

__all__ = [
    "GroundedText",
    "ProvenanceEntry",
    "ground_qualitative_text",
]


@dataclass(frozen=True)
class ProvenanceEntry:
    """UI-facing provenance for one surfaced sentence (phase 4).

    Attributes:
        text: The sentence as surfaced (may carry :data:`UNVERIFIED_MARKER`).
        status: ``"grounded"`` | ``"unverified"`` | ``"non_claim"``.
        citations: The resolved citation ids backing the sentence (if any).
    """

    text: str
    status: str
    citations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GroundedText:
    """Result of running the full grounding pipeline over a qualitative text.

    Attributes:
        text: The cleaned text, safe to surface to the banker.
        fully_grounded: True iff nothing was stripped/flagged/demoted.
        provenance: Per-surfaced-sentence provenance for the UI.
        citation_result: The raw phase-2 result (for diagnostics/tests).
        faithfulness: The phase-3 result (empty when nothing to score).
    """

    text: str
    fully_grounded: bool
    provenance: list[ProvenanceEntry] = field(default_factory=list)
    citation_result: ClaimGroundingResult | None = None
    faithfulness: FaithfulnessResult | None = None


def _evidence_text_for(
    citations: list[str],
    evidence_texts: Mapping[str, str],
) -> str:
    """Concatenate the evidence text backing a claim's resolved citations."""
    parts = [evidence_texts[c] for c in citations if c in evidence_texts]
    return "\n".join(p for p in parts if p)


def ground_qualitative_text(
    text: str,
    evidence: EvidencePacket | Mapping[str, Iterable[str]],
    *,
    evidence_texts: Mapping[str, str] | None = None,
    flag: bool = False,
    faithfulness_floor: float = DEFAULT_FAITHFULNESS_FLOOR,
    settings: Settings | None = None,
) -> GroundedText:
    """Run the full Feature 0 grounding pipeline over a qualitative text.

    Args:
        text: The generated qualitative output (empty offline -> no-op).
        evidence: The evidence packet (ids the text may cite).
        evidence_texts: Optional map of ``citation_id -> evidence text`` used by
            the phase-3 faithfulness gate to check entailment. When omitted,
            phase 3 is skipped (citation grounding only).
        flag: When True, ungrounded/unfaithful claims are kept and marked
            :data:`UNVERIFIED_MARKER` (UI-provenance posture) instead of being
            stripped (the default abstain posture).
        faithfulness_floor: Minimum entailment score to keep a claim verified.
        settings: Optional settings override (defaults to cached settings).

    Returns:
        A :class:`GroundedText` with the cleaned text and provenance.
    """
    settings = settings or get_settings()

    # Trivial / offline no-op.
    if not text or not text.strip():
        return GroundedText(text="", fully_grounded=True)

    # --- Phase 2: citation grounding. ---
    citation_result = check_claims_grounded(text, evidence, flag=flag)

    # --- Phase 3: faithfulness over the citation-grounded claims. ---
    faith: FaithfulnessResult | None = None
    demoted: set[str] = set()
    if evidence_texts:
        to_score: dict[str, str] = {}
        for prov in citation_result.provenance:
            if prov.verdict is ClaimVerdict.GROUNDED:
                cited = [cid for cid, _tier in prov.resolved]
                to_score[prov.text] = _evidence_text_for(cited, evidence_texts)
        if to_score:
            faith = score_claims(to_score, floor=faithfulness_floor, settings=settings)
            demoted = {c.claim for c in faith.demoted}

    # --- Assemble surfaced text + provenance (phases 4/5). ---
    surfaced: list[str] = []
    provenance: list[ProvenanceEntry] = []
    for prov in citation_result.provenance:
        if prov.verdict is ClaimVerdict.NON_CLAIM:
            surfaced.append(prov.text)
            provenance.append(ProvenanceEntry(text=prov.text, status="non_claim"))
            continue

        cited = [cid for cid, _tier in prov.resolved]
        is_demoted = prov.text in demoted
        grounded = prov.verdict is ClaimVerdict.GROUNDED and not is_demoted

        if grounded:
            surfaced.append(prov.text)
            provenance.append(ProvenanceEntry(text=prov.text, status="grounded", citations=cited))
            continue

        # Ungrounded (phase 2) or demoted (phase 3): abstain or flag.
        if flag:
            marked = f"{prov.text} {UNVERIFIED_MARKER}"
            surfaced.append(marked)
            provenance.append(ProvenanceEntry(text=marked, status="unverified", citations=cited))
        # strip mode: omit entirely (calibrated abstention, phase 5).

    cleaned = " ".join(surfaced).strip()
    fully_grounded = citation_result.grounded and not demoted
    return GroundedText(
        text=cleaned,
        fully_grounded=fully_grounded,
        provenance=provenance,
        citation_result=citation_result,
        faithfulness=faith,
    )
