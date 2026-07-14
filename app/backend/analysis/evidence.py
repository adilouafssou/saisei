"""Evidence-packet construction for the claim-grounding verifier (Feature 0).

Phase 1 of Feature 0 (grounding-by-construction) needs a single, deterministic
definition of *what a generated qualitative text is allowed to cite*: the
pre-computed deterministic signals plus the retrieved source passages. This
module builds that :class:`EvidencePacket` from a ``SaiseiState`` (and, where
applicable, the retrieved snippets handed to a generator), so every grounding
call site uses the SAME ground-truth definition rather than ad-hoc sets.

It is pure, deterministic, offline, and reads-only: it never mutates state and
never calls an LLM. The set of available signal keys is derived solely from
which deterministic fields the spine has populated, so a citation can only
resolve to a figure/label that actually exists for this borrower.

The canonical signal-key vocabulary (the only ids a prompt should instruct the
model to cite) is exported as :data:`SIGNAL_KEYS` so the prompt contract and the
verifier stay in lockstip.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.backend.analysis.claim_grounding import EvidencePacket
from app.backend.state import SaiseiState

__all__ = [
    "SIGNAL_KEYS",
    "available_signal_keys",
    "build_evidence_packet",
]

#: The canonical deterministic-signal citation vocabulary. A prompt may only ask
#: the model to cite these ids (plus retrieved source labels); the verifier only
#: resolves a signal citation when its key is in this set AND populated on state.
#: Keep this list in sync with the ``# signal:`` markers in the persona/
#: feasibility prompts.
SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "ews",
        "fsa_classification",
        "working_capital_gap",
        "tdb_score",
        "net_worth",
        "special_attention",
        "hosho_kaijo_score",
        "expected_uplift",
        "feasibility_score",
        "burden_table",
        "settlement_metrics",
        "boj_rate",
    }
)


def available_signal_keys(state: SaiseiState) -> frozenset[str]:
    """Return the deterministic signal ids actually populated for this borrower.

    A citation may only resolve to a figure that exists. This inspects the state
    and returns the subset of :data:`SIGNAL_KEYS` whose backing field is present
    (non-None / non-empty), so the verifier rejects a citation to, say, ``ews``
    when EWS was never computed.

    Args:
        state: The current graph state.

    Returns:
        The frozenset of available signal keys.
    """
    keys: set[str] = set()
    if state.ews_score is not None:
        keys.add("ews")
    if state.fsa_classification is not None:
        keys.add("fsa_classification")
    if state.working_capital_gap is not None:
        keys.add("working_capital_gap")
    if state.tdb_score is not None:
        keys.add("tdb_score")
    if state.net_worth is not None:
        keys.add("net_worth")
    if state.special_attention is not None:
        keys.add("special_attention")
    if state.hosho_kaijo_score is not None:
        keys.add("hosho_kaijo_score")
    if state.proposed_strategies:
        keys.add("expected_uplift")
    if state.feasibility_notes:
        keys.add("feasibility_score")
    if state.critic_feedbacks:
        keys.add("burden_table")
    if state.settlement_metrics is not None:
        keys.add("settlement_metrics")
    if state.boj_rate_curve:
        keys.add("boj_rate")
    return frozenset(keys)


def build_evidence_packet(
    state: SaiseiState,
    source_labels: Iterable[str] = (),
) -> EvidencePacket:
    """Build the evidence packet a generated text is allowed to cite.

    Combines the deterministic signals populated on ``state`` with the labels of
    any retrieved source passages handed to the generator. This is the single
    ground-truth definition used at every grounding call site so prompt contract
    and verifier agree.

    Args:
        state: The current graph state (source of deterministic signal keys).
        source_labels: Labels of retrieved snippets available to cite (e.g. the
            ``source`` field of each ``RetrievalSnippet``). Empty offline.

    Returns:
        A frozen :class:`EvidencePacket`.
    """
    return EvidencePacket.build(
        signal_keys=available_signal_keys(state),
        source_labels=source_labels,
    )
