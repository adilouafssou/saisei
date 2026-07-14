"""Deterministic claim-grounding verifier for qualitative LLM output.

Feature 0 (claim-grounding & hallucination control), phase 1 — the **analogue of
``numeric_preservation`` for claims**. It is the deterministic gate that must
exist before any generated qualitative text (a critic rationale, the feasibility
advisory, the creditor-meeting briefing, the plan prose) can ever reach a
banker.

Why this exists
---------------
The numeric core is already protected: figures are computed deterministically and
``numeric_preservation`` rejects any polish whose numbers don't match the source.
But the **qualitative** LLM output currently ships unverified. Labelling it
"advisory-only" does not neutralise the risk: the briefing is read by the banker
*before* the real creditor meeting, so a hallucinated rationale can materially
steer a regulated decision.

The architectural commitment (Feature 0): every qualitative claim the banker sees
is either (a) grounded in a deterministic figure, (b) grounded in a specific
retrieved source passage, or (c) clearly rendered as *unverified model
commentary*. The system must **abstain (omit) rather than assert** when it cannot
ground a claim — mirroring the numeric stance exactly: generate, then verify
against ground truth before it can do harm.

What this module does (phase 1: the claim-citation verifier)
------------------------------------------------------------
Given a generated qualitative text plus its **evidence packet** (the deterministic
signals it was allowed to reason over + the retrieved source passages), this is a
deterministic post-processor that, sentence by sentence:

- extracts the citation tag(s) each sentence carries (``[<id>]`` markers, the same
  convention the feasibility prompt already asks the LLM to emit, e.g.
  ``[past_keikakusho]`` or ``[ews]``);
- checks each cited id actually **resolves** against the evidence packet (the
  signal key exists, or the source label is a real retrieved document);
- **keeps** sentences whose citations all resolve (grounded);
- **strips or flags** sentences that assert without a resolving citation, exactly
  as the numeric gate drops a bad polish.

It returns the cleaned text plus a provenance map (per sentence: grounded, the
resolved citations, and the reason it was kept/stripped/flagged). Pure and
deterministic: same inputs -> same result, no network, no LLM, stdlib only
(``re`` / ``dataclasses``). It mirrors ``numeric_preservation`` so the same
"deterministic verifier gates LLM text" pattern now covers claims as well as
numbers.

Scope / honesty guard
---------------------
This does not make the LLM *correct*; it makes every claim **attributable or
visibly unverified**. A grounded claim can still be a poor judgement — which is
precisely why the human, not the model, remains the only decider. Out of scope
for this slice (later phases of Feature 0): faithfulness/entailment scoring,
UI provenance rendering, and grounding-by-construction prompt contracts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "EvidencePacket",
    "ClaimVerdict",
    "SentenceProvenance",
    "ClaimGroundingResult",
    "split_sentences",
    "extract_citations",
    "check_claims_grounded",
    "guard_grounded_text",
    "UNVERIFIED_MARKER",
]

#: Suffix appended to a retained-but-uncited sentence in *flag* mode, so the
#: banker can see it is model commentary rather than an attributable claim.
#: Mirrors the spec's "unverified model commentary" provenance label.
UNVERIFIED_MARKER = "【未検証 / unverified】"

#: Citation tag in generated text: ``[<id>]`` where ``<id>`` is a deterministic
#: signal key (e.g. ``ews``) or a retrieved source label (e.g.
#: ``past_keikakusho``). The id is non-greedy and may not contain ``[`` / ``]``.
_CITATION_PATTERN = re.compile(r"\[([^\[\]]+?)\]")

#: Sentence matcher for both Japanese (。！？) and Latin (.!?) scripts. A
#: sentence is a run of non-newline characters terminated by one (or more) of
#: those marks, or a trailing run with no terminator. Crucially this does NOT
#: require whitespace after the terminator: Japanese prose has none, so the
#: previous ``\s+`` rule collapsed an entire JP paragraph into one "sentence"
#: and let ungrounded claims ride along undetected. Newlines always break a
#: sentence. Kept deliberately simple and deterministic (no NLP dependency).
#:
#: A Latin ``.`` is only treated as a terminator when it is NOT a decimal
#: point -- i.e. not simultaneously preceded AND followed by a digit -- so a
#: figure like ``62.0`` is never split into ``62.`` + ``0`` (which would strand
#: the figure's ``[citation]`` on the wrong fragment and falsely flag it
#: unverified). The JP terminators (。！？) and Latin ``!``/``?`` always
#: terminate.
#: A trailing run of citation tags that follows the terminator (optionally
#: whitespace-separated) is absorbed INTO the sentence the terminator closed,
#: so a figure sentence whose ``[citation]`` is written AFTER the JP full stop
#: -- e.g. ``EWSスコアは 62.0 です。[ews_score]`` (the companion's templated
#: format) -- keeps its citation instead of stranding it on the next fragment
#: (which left the figure sentence citation-free and falsely flagged
#: unverified). Without this the JP terminator ``。`` ended the sentence one
#: character before the tag. ``check_claims_grounded`` then re-extracts the tag
#: from the whole sentence, so attachment here is what makes it resolve.
_SENTENCE_MATCH = re.compile(
    r"[^\n]*?(?:[。！？!?]|(?<!\d)\.|\.(?!\d))+(?:\s*\[[^\[\]]+\])*|[^\n]+"
)


# ---------------------------------------------------------------------------
# Evidence packet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidencePacket:
    """The ground truth a generated text was allowed to reason over.

    A citation in the generated text resolves iff its id is one of these keys.
    Both tiers are matched case-insensitively and are interchangeable for the
    purpose of *resolving* a citation — the provenance map records which tier a
    citation resolved against.

    Attributes:
        signal_keys: Deterministic signal ids the LLM may cite, e.g.
            ``{"ews", "working_capital_gap", "fsa_classification"}``. These are
            the audited figures/labels computed by the spine.
        source_labels: Retrieved-source ids the LLM may cite, e.g.
            ``{"past_keikakusho", "benchmark", "fsa_manual"}`` — the ``source``
            field of each ``RetrievalSnippet`` handed to the generator.
    """

    signal_keys: frozenset[str] = field(default_factory=frozenset)
    source_labels: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def build(
        cls,
        signal_keys: Iterable[str] = (),
        source_labels: Iterable[str] = (),
    ) -> EvidencePacket:
        """Construct a packet from any iterables, normalising ids to lower-case.

        Args:
            signal_keys: Deterministic signal ids available as ground truth.
            source_labels: Retrieved-source ids available as ground truth.

        Returns:
            A frozen :class:`EvidencePacket` with case-normalised id sets.
        """
        return cls(
            signal_keys=frozenset(_norm(k) for k in signal_keys if _norm(k)),
            source_labels=frozenset(_norm(s) for s in source_labels if _norm(s)),
        )

    def resolve(self, citation_id: str) -> str | None:
        """Return the tier a citation resolves to, or None if it does not.

        Args:
            citation_id: The raw id from a ``[<id>]`` tag.

        Returns:
            ``"signal"`` or ``"source"`` when the id is known ground truth,
            otherwise ``None``.
        """
        key = _norm(citation_id)
        if key in self.signal_keys:
            return "signal"
        if key in self.source_labels:
            return "source"
        return None


def _norm(value: str) -> str:
    """Normalise a citation/key id for case-insensitive comparison."""
    return value.strip().lower()


# ---------------------------------------------------------------------------
# Per-sentence verdict + provenance
# ---------------------------------------------------------------------------


class ClaimVerdict(StrEnum):
    """Outcome for a single sentence under the grounding check."""

    #: Sentence carries >= 1 citation and every citation resolves -> kept.
    GROUNDED = "grounded"
    #: Sentence asserts but carries no citation, or a citation that does not
    #: resolve -> stripped (strip mode) or marked unverified (flag mode).
    UNGROUNDED = "ungrounded"
    #: Sentence makes no factual assertion (e.g. a heading or empty fragment)
    #: -> kept verbatim; grounding does not apply.
    NON_CLAIM = "non_claim"


@dataclass(frozen=True)
class SentenceProvenance:
    """Provenance record for one sentence of the generated text.

    Attributes:
        text: The original sentence (trimmed), without any added marker.
        verdict: The grounding verdict for this sentence.
        citations: The raw citation ids found in the sentence (in order).
        resolved: The subset of ``citations`` that resolved, each as
            ``(id, tier)`` where tier is ``"signal"`` or ``"source"``.
        kept: Whether the sentence appears in the cleaned output.
    """

    text: str
    verdict: ClaimVerdict
    citations: list[str] = field(default_factory=list)
    resolved: list[tuple[str, str]] = field(default_factory=list)
    kept: bool = True


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimGroundingResult:
    """Outcome of a claim-grounding check over a qualitative text.

    Attributes:
        grounded: True iff no sentence was ungrounded (nothing had to be
            stripped or flagged) — the text is fully attributable as-is.
        cleaned_text: The text after applying the chosen mode (ungrounded
            sentences removed, or suffixed with :data:`UNVERIFIED_MARKER`).
        provenance: Per-sentence provenance, in original order.
        flagged: Whether *flag* mode (vs *strip*) was used.
    """

    grounded: bool
    cleaned_text: str
    provenance: list[SentenceProvenance] = field(default_factory=list)
    flagged: bool = False

    @property
    def ungrounded(self) -> list[SentenceProvenance]:
        """Sentences that failed the grounding check."""
        return [p for p in self.provenance if p.verdict is ClaimVerdict.UNGROUNDED]

    def reason(self) -> str:
        """Return a human-readable explanation (empty when fully grounded)."""
        bad = self.ungrounded
        if not bad:
            return ""
        verb = "flagged" if self.flagged else "stripped"
        previews = "; ".join(_preview(p.text) for p in bad)
        return f"{len(bad)} ungrounded claim(s) {verb}: {previews}"


def _preview(text: str, limit: int = 48) -> str:
    """Return a short single-line preview of a sentence for diagnostics."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences on JP/Latin terminators and newlines.

    Deterministic and intentionally simple (no NLP dependency): splits after
    each ``。！？.!?`` terminator (no trailing whitespace required, so Japanese
    prose splits correctly) and on newlines. Empty fragments are dropped.
    Markdown bullet/heading prefixes are preserved within the sentence so the
    cleaned output keeps its shape.

    Args:
        text: The generated qualitative text.

    Returns:
        Trimmed, non-empty sentence fragments in order.
    """
    if not text:
        return []
    parts: list[str] = []
    for line in text.split("\n"):
        parts.extend(_SENTENCE_MATCH.findall(line))
    return [p.strip() for p in parts if p and p.strip()]


def extract_citations(sentence: str) -> list[str]:
    """Extract the citation ids from a sentence, in order of appearance.

    Recognises ``[<id>]`` tags. A comma/space/、-separated id list inside one
    bracket (e.g. ``[ews, benchmark]``) is split into individual ids so each is
    resolved independently.

    Args:
        sentence: One sentence of generated text.

    Returns:
        The raw citation ids (untrimmed of case; resolution normalises).
    """
    ids: list[str] = []
    for match in _CITATION_PATTERN.finditer(sentence):
        inner = match.group(1)
        for piece in re.split(r"[,、;]\s*|\s{2,}", inner):
            piece = piece.strip()
            if piece:
                ids.append(piece)
    return ids


#: A sentence is treated as a factual *claim* (subject to grounding) when it
#: contains at least one "word" character in any script. Pure punctuation,
#: bullets, or separators are NON_CLAIM and pass through untouched.
_HAS_WORD = re.compile(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]")

#: Lines that are structurally non-claims even though they contain words:
#: Markdown headings (``#``), and bare bullet/list markers with no body are
#: still claims if they carry words — we only exclude pure-heading lines so the
#: document skeleton survives. A heading is a label, not an assertion of fact.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")


def _is_claim(sentence: str) -> bool:
    """Return whether a sentence asserts a fact (so grounding applies)."""
    if not _HAS_WORD.search(sentence):
        return False
    return not _HEADING.match(sentence)


# ---------------------------------------------------------------------------
# Core check + guard
# ---------------------------------------------------------------------------


def check_claims_grounded(
    text: str,
    evidence: EvidencePacket | Mapping[str, Iterable[str]],
    *,
    flag: bool = False,
) -> ClaimGroundingResult:
    """Verify every claim in ``text`` carries a resolving citation.

    Pure and deterministic. Each sentence is classified:

    - **NON_CLAIM** (no words, or a heading): kept verbatim.
    - **GROUNDED** (>= 1 citation, and *all* citations resolve against the
      evidence packet): kept verbatim.
    - **UNGROUNDED** (no citation, or any citation that does not resolve):
      removed (``flag=False``, default) or kept with the
      :data:`UNVERIFIED_MARKER` suffix (``flag=True``).

    Requiring *all* citations in a sentence to resolve is the conservative
    choice: a sentence that mixes a real source with a fabricated one is not
    trustworthy as fact, so it fails. This mirrors the numeric gate's all-or-
    nothing posture (one bad figure fails the whole polish).

    Args:
        text: The generated qualitative output to verify.
        evidence: The evidence packet, or a mapping with keys ``signal_keys``
            and/or ``source_labels`` mapping to id iterables.
        flag: When True, ungrounded claims are kept and marked unverified
            instead of being stripped (the UI-provenance posture). Default
            False strips them (the abstain-rather-than-assert posture).

    Returns:
        A :class:`ClaimGroundingResult` with the cleaned text and provenance.
    """
    packet = _coerce_packet(evidence)
    provenance: list[SentenceProvenance] = []
    kept_lines: list[str] = []

    for sentence in split_sentences(text):
        if not _is_claim(sentence):
            provenance.append(
                SentenceProvenance(text=sentence, verdict=ClaimVerdict.NON_CLAIM, kept=True)
            )
            kept_lines.append(sentence)
            continue

        citations = extract_citations(sentence)
        resolved: list[tuple[str, str]] = []
        all_resolve = bool(citations)
        for cid in citations:
            tier = packet.resolve(cid)
            if tier is None:
                all_resolve = False
            else:
                resolved.append((cid, tier))

        if all_resolve:
            provenance.append(
                SentenceProvenance(
                    text=sentence,
                    verdict=ClaimVerdict.GROUNDED,
                    citations=citations,
                    resolved=resolved,
                    kept=True,
                )
            )
            kept_lines.append(sentence)
            continue

        # Ungrounded: strip (default) or flag.
        if flag:
            kept_lines.append(f"{sentence} {UNVERIFIED_MARKER}")
        provenance.append(
            SentenceProvenance(
                text=sentence,
                verdict=ClaimVerdict.UNGROUNDED,
                citations=citations,
                resolved=resolved,
                kept=flag,
            )
        )

    cleaned_text = " ".join(kept_lines).strip()
    grounded = not any(p.verdict is ClaimVerdict.UNGROUNDED for p in provenance)
    return ClaimGroundingResult(
        grounded=grounded,
        cleaned_text=cleaned_text,
        provenance=provenance,
        flagged=flag,
    )


def guard_grounded_text(
    text: str,
    evidence: EvidencePacket | Mapping[str, Iterable[str]],
    *,
    flag: bool = False,
) -> tuple[str, ClaimGroundingResult]:
    """Return attributable text (ungrounded claims removed/flagged) + the result.

    This is the fail-safe gate for any qualitative LLM output before it reaches a
    banker: an advisory rationale or briefing must never present an
    unattributable assertion as fact. The cleaned text — abstaining on what it
    cannot ground — is always safe to surface, mirroring the best-effort
    contract of ``guard_polished_text`` (the gate never breaks the workflow and
    can never silently pass an ungrounded claim).

    Args:
        text: The generated qualitative candidate.
        evidence: The evidence packet (or an equivalent mapping).
        flag: Keep-and-mark instead of strip (see :func:`check_claims_grounded`).

    Returns:
        A tuple of (text_to_use, result). ``text_to_use`` is the cleaned text
        (which equals the input when it is already fully grounded).
    """
    result = check_claims_grounded(text, evidence, flag=flag)
    return result.cleaned_text, result


def _coerce_packet(
    evidence: EvidencePacket | Mapping[str, Iterable[str]],
) -> EvidencePacket:
    """Accept either an :class:`EvidencePacket` or a plain mapping."""
    if isinstance(evidence, EvidencePacket):
        return evidence
    return EvidencePacket.build(
        signal_keys=evidence.get("signal_keys", ()),
        source_labels=evidence.get("source_labels", ()),
    )
