"""Audit-event model + canonical hashing (Feature 7, spec §4 / §12 step 1).

The immutable audit ledger records three kinds of events (classification,
guarantee-release, human decision). Each :class:`AuditEvent` is frozen and stores
a deterministic SHA-256 ``content_hash`` over its canonical JSON, plus the
``prev_hash`` of the previous event for the same ``thread_id`` — forming a
tamper-evident hash chain. Any retro-edit of a stored event breaks the chain and
is detectable (see ``sink.verify_chain``, added in a later step).

This module is pure and deterministic: same inputs -> same hash, no network, no
LLM, stdlib + pydantic only. It defines NO storage; the sink abstraction lives in
``app.backend.audit.sink``.

Canonicalisation contract (must stay stable forever, or historical hashes break):
- JSON with sorted keys, ``ensure_ascii=False``, separators ``(",", ":")``.
- The ``content_hash`` field is EXCLUDED from its own hash input.
- All other fields (including ``prev_hash``) are included, so the chain is
  bound into each event's identity.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AuditEventType",
    "AuditEvent",
    "canonical_json",
    "compute_content_hash",
]


class AuditEventType(StrEnum):
    """The kinds of events the audit ledger records (extensible)."""

    #: Emitted by the classifier node after a deterministic FSA classification.
    CLASSIFICATION = "classification"
    #: Emitted by the keieisha_hosho node after a guarantee-release assessment.
    GUARANTEE_RELEASE = "guarantee_release"
    #: Emitted on the HITL resume path when a banker decides.
    HUMAN_DECISION = "human_decision"
    #: Emitted by the origination node at the 稟議 gate: records the
    #: deterministic, advisory credit recommendation (APPROVE / DECLINE), the
    #: provisional facility ceiling, and its grounding status. ADVISORY ONLY —
    #: the recommendation never records the credit decision itself; the
    #: UNDER_REVIEW → APPROVED / DECLINED transition is HITL-gated and recorded
    #: as a HUMAN_DECISION. This is the who-was-recommended-what record, pinned
    #: to the data + thresholds version at recommendation time.
    ORIGINATION_DECISION = "origination_decision"
    #: Emitted when the banker asks the advisory companion a question. Records
    #: the question + the answer's grounding status (advisory, read-only): a
    #: case-shaping conversation must leave a trail, like any other event a
    #: regulator cares about. The companion itself never decides; this is the
    #: who-asked-what record, pinned to the data version at ask time.
    COMPANION_QUERY = "companion_query"
    #: Administrative: a redaction directive. Recorded as its OWN immutable,
    #: hash-chained, signed event (never an edit/delete of the target) naming the
    #: target event_id, the payload keys to mask, the actor, and the reason. The
    #: original row is untouched; reads mask the named keys at view time. This is
    #: how redaction stays compatible with an append-only ledger: the ACT of
    #: redacting is itself part of the permanent trail.
    REDACTION = "redaction"
    #: Administrative: a legal hold placed on a thread (e.g. for litigation /
    #: examination). While an unreleased hold exists, the retention sweep must
    #: skip the thread's events. Append-only marker; released by a matching
    #: LEGAL_HOLD_RELEASE event.
    LEGAL_HOLD = "legal_hold"
    #: Administrative: release of a previously placed legal hold on a thread.
    LEGAL_HOLD_RELEASE = "legal_hold_release"


#: Fields excluded from the canonical hash input. ``content_hash`` is excluded
#: because it cannot hash itself; ``signature`` is excluded because it is
#: computed OVER the content_hash AFTER sealing, so it must not feed back into
#: the hash (and so adding signing leaves every historical content_hash
#: byte-identical — unsigned legacy events verify exactly as before).
_HASH_EXCLUDED: frozenset[str] = frozenset({"content_hash", "signature"})


class AuditEvent(BaseModel):
    """One immutable, hash-chained audit-ledger record.

    Frozen and ``extra="forbid"`` (consistent with the project's domain models):
    an event is written once and never mutated; stray fields fail loudly.

    Attributes:
        event_id: UUID4 string, generated at write time.
        thread_id: The run's thread_id (groups one borrower's events in order).
        tdb_code: 7-digit borrower code (denormalised for query).
        hojin_bango: 13-digit corporate number (denormalised for query).
        event_type: One of :class:`AuditEventType`.
        created_at: ISO 8601 UTC timestamp, set at write time.
        actor: "system" for deterministic nodes; the banker id for decisions.
        payload: Event-kind-specific, canonical-serialisable contents (spec §3).
        data_version: Hash over the borrower inputs the event was computed from.
        thresholds_version: Hash over the relevant constants in force.
        content_hash: SHA-256 of this event's canonical JSON (excluding itself).
        prev_hash: ``content_hash`` of the previous event for this thread_id,
            or "" for the genesis event.
        signature: Optional detached cryptographic signature over
            ``content_hash`` (hex). Empty when no signing key is configured
            (the offline default). Excluded from the content hash, so signing an
            event never changes its identity and unsigned events stay valid.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    thread_id: str
    tdb_code: str
    hojin_bango: str = ""
    event_type: AuditEventType
    created_at: str
    actor: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    data_version: str = ""
    thresholds_version: str = ""
    content_hash: str = ""
    prev_hash: str = ""
    signature: str = ""

    def with_content_hash(self) -> AuditEvent:
        """Return a copy with ``content_hash`` set to this event's canonical hash.

        Pure: computes the hash over every field except ``content_hash`` itself
        (see :func:`compute_content_hash`) and returns a new frozen instance.
        """
        digest = compute_content_hash(self)
        return self.model_copy(update={"content_hash": digest})

    def hash_is_valid(self) -> bool:
        """Return whether ``content_hash`` matches a recomputation (tamper check)."""
        return bool(self.content_hash) and self.content_hash == compute_content_hash(self)


def _hashable_mapping(event: AuditEvent) -> dict[str, Any]:
    """Return the event as a plain dict with the hash field(s) removed.

    ``event_type`` (a StrEnum) serialises to its string value via
    ``model_dump(mode="json")`` so the canonical form is stable and JSON-native.
    """
    data = event.model_dump(mode="json")
    for key in _HASH_EXCLUDED:
        data.pop(key, None)
    return data


def canonical_json(event: AuditEvent) -> str:
    """Serialise an event to canonical JSON for hashing (deterministic).

    Sorted keys, ``ensure_ascii=False`` (so CJK is stable, not escaped), and
    compact separators. Excludes ``content_hash``. Two events that differ only in
    dict insertion order produce byte-identical output.

    Args:
        event: The event to canonicalise.

    Returns:
        The canonical JSON string.
    """
    return json.dumps(
        _hashable_mapping(event),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compute_content_hash(event: AuditEvent) -> str:
    """Return the SHA-256 hex digest of an event's canonical JSON.

    Args:
        event: The event to hash (its existing ``content_hash`` is ignored).

    Returns:
        The 64-char lowercase hex SHA-256 digest.
    """
    return hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()
