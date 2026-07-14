"""Trajectory record model + preference framing (Feature 3).

A :class:`TrajectoryRecord` is one captured agent trajectory for a single
negotiation: the inputs the strategist saw, the candidate strategies it
proposed, and the banker's decision over them. It is the unit of the data
flywheel — a stream of these records is the offline training corpus for
supervised fine-tuning, preference optimisation (DPO/ORPO), and a revision-note
reward model.

Design (mirrors the audit model):
- Frozen pydantic model, ``extra="forbid"`` (write-once, stray fields fail loud).
- A deterministic SHA-256 ``content_hash`` over canonical JSON, so a stored
  record's integrity is checkable and duplicates are detectable. Same
  canonicalisation contract as the audit ledger (sorted keys, ensure_ascii=
  False, compact separators, the hash field excluded from its own input).
- Pure / deterministic / stdlib + pydantic only. No storage here (the store
  seam lives in ``app.backend.trajectory.store``); no network, no LLM.

Preference framing: :meth:`TrajectoryRecord.preference_pair` derives the
``(chosen, rejected, critique)`` triple every preference-optimisation pipeline
needs — the approved strategy is *chosen*, the other proposed strategies are
*rejected*, and the banker's revision note is the *critique*. A record with no
approved strategy (revise / reject) yields no chosen option, which is itself a
label (the banker rejected the whole slate).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TrajectoryDecision",
    "PreferencePair",
    "NodeSnapshot",
    "TrajectoryRecord",
    "canonical_json",
    "compute_content_hash",
]


class TrajectoryDecision(StrEnum):
    """The banker's decision over a proposed slate (the preference label)."""

    #: Banker accepted one strategy -> it is the chosen option.
    APPROVE = "approve"
    #: Banker sent the slate back for revision (no chosen option this round).
    REVISE = "revise"
    #: Banker rejected the slate outright (no chosen option).
    REJECT = "reject"


@dataclass(frozen=True)
class PreferencePair:
    """A preference triple derived from a trajectory for offline training.

    Attributes:
        chosen: The approved strategy dict, or None when the banker did not
            approve any (revise / reject).
        rejected: The non-chosen proposed strategy dicts (the alternatives).
        critique: The banker's revision note (the natural-language reason),
            usable as the label for a revision-note reward model. May be "".
    """

    chosen: dict[str, Any] | None
    rejected: list[dict[str, Any]]
    critique: str = ""

    @property
    def has_chosen(self) -> bool:
        """True when there is an approved (chosen) strategy."""
        return self.chosen is not None


class NodeSnapshot(BaseModel):
    """One node's output digest in the captured per-node trajectory (Feature 3.1).

    A compact, deterministic snapshot of a single graph node's contribution,
    reconstructed from the accumulated state at HITL time (see
    ``recorder.build_node_trajectory``). Frozen + ``extra="forbid"`` like the
    record itself; the ``output`` dict is JSON-canonical so the same node output
    always hashes identically.

    Attributes:
        node: The graph node name (e.g. ``"ews"``, ``"classifier"``).
        output: A compact, JSON-serialisable digest of that node's output (not
            the whole state) — enough to reconstruct the agentic path for
            offline training without storing every field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    output: dict[str, Any] = Field(default_factory=dict)


#: Fields excluded from the canonical hash input (only the hash itself).
_HASH_EXCLUDED: frozenset[str] = frozenset({"content_hash"})


class TrajectoryRecord(BaseModel):
    """One captured agent trajectory for a single negotiation.

    Frozen and ``extra="forbid"`` (consistent with the project's domain models):
    a trajectory is written once and never mutated.

    Attributes:
        trajectory_id: UUID4 string, generated at write time.
        thread_id: The run's thread_id (groups one borrower's rounds in order).
        tdb_code: 7-digit borrower code (denormalised for query).
        hojin_bango: 13-digit corporate number (denormalised for query).
        created_at: ISO 8601 UTC timestamp, set at write time.
        actor: The banker id who made the decision (or a placeholder).
        decision: The banker's decision (see :class:`TrajectoryDecision`).
        revision_note: The banker's free-text note (critique). May be "".
        data_version: Hash over the borrower inputs the trajectory was built on.
        input_summary: Compact, deterministic snapshot of the inputs the
            strategist saw (fsa_classification, ews_score, working_capital_gap,
            revision_count) — enough to condition a model without storing the
            whole state object.
        proposed_strategies: The candidate strategy dicts the strategist output.
        approved_strategy: The chosen strategy dict, or None (revise / reject).
        keikakusho_draft: The final plan text when one was written, else "".
        node_trajectory: Feature 3.1 — the ordered per-node output digests
            (``NodeSnapshot`` list) reconstructed from the accumulated state, so
            the record carries the full agentic path, not just the negotiation
            summary. Empty (byte-identical record) when not captured.
        interrupt_payload: Feature 3.1 — the raw HITL interrupt payload the
            banker actually saw at decision time. Empty when not captured.
        content_hash: SHA-256 of this record's canonical JSON (excluding itself).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trajectory_id: str
    thread_id: str
    tdb_code: str
    hojin_bango: str = ""
    created_at: str
    actor: str = "system"
    decision: TrajectoryDecision
    revision_note: str = ""
    data_version: str = ""
    input_summary: dict[str, Any] = Field(default_factory=dict)
    proposed_strategies: list[dict[str, Any]] = Field(default_factory=list)
    approved_strategy: dict[str, Any] | None = None
    keikakusho_draft: str = ""
    # Feature 3.1: the full per-node trajectory + the raw interrupt payload.
    # Additive and optional: both default empty so a record built without them
    # hashes byte-identically to a pre-Feature-3.1 record. Both are covered by
    # content_hash (no hash exclusion added), so they are tamper-evident too.
    node_trajectory: list[NodeSnapshot] = Field(default_factory=list)
    interrupt_payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""

    def with_content_hash(self) -> TrajectoryRecord:
        """Return a copy with ``content_hash`` set to this record's canonical hash."""
        return self.model_copy(update={"content_hash": compute_content_hash(self)})

    def hash_is_valid(self) -> bool:
        """Return whether ``content_hash`` matches a recomputation (integrity)."""
        return bool(self.content_hash) and self.content_hash == compute_content_hash(self)

    def preference_pair(self) -> PreferencePair:
        """Derive the (chosen, rejected, critique) preference triple.

        The approved strategy is *chosen*; every other proposed strategy is
        *rejected*; the revision note is the *critique*. Identity of the chosen
        option is matched on the strategy ``title`` (the stable key), so the
        rejected set excludes the approved one even though both come from
        ``proposed_strategies``.

        Returns:
            A :class:`PreferencePair`. When there is no approved strategy
            (revise / reject), ``chosen`` is None and every proposed strategy is
            in ``rejected``.
        """
        chosen = self.approved_strategy
        chosen_title = chosen.get("title") if chosen else None
        rejected = [
            s
            for s in self.proposed_strategies
            if chosen_title is None or s.get("title") != chosen_title
        ]
        return PreferencePair(chosen=chosen, rejected=rejected, critique=self.revision_note)


def _hashable_mapping(record: TrajectoryRecord) -> dict[str, Any]:
    """Return the record as a plain dict with the hash field removed."""
    data = record.model_dump(mode="json")
    for key in _HASH_EXCLUDED:
        data.pop(key, None)
    return data


def canonical_json(record: TrajectoryRecord) -> str:
    """Serialise a record to canonical JSON for hashing (deterministic).

    Sorted keys, ``ensure_ascii=False`` (CJK stable, not escaped), compact
    separators; excludes ``content_hash``. Same contract as the audit ledger so
    the two stores hash identically.

    Args:
        record: The record to canonicalise.

    Returns:
        The canonical JSON string.
    """
    return json.dumps(
        _hashable_mapping(record),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compute_content_hash(record: TrajectoryRecord) -> str:
    """Return the SHA-256 hex digest of a record's canonical JSON.

    Args:
        record: The record to hash (its existing ``content_hash`` is ignored).

    Returns:
        The 64-char lowercase hex SHA-256 digest.
    """
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
