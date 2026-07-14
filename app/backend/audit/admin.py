"""Retention, redaction, and legal-hold admin actions (audit-ledger hardening).

The ledger is append-only and tamper-proof (DB trigger blocks UPDATE/DELETE;
events are hash-chained and optionally Ed25519-signed). Retention, redaction,
and legal-hold seem to conflict with that -- they imply removing or masking
data. The resolution that PRESERVES the append-only guarantee is:

* **Redaction is an append, not an edit.** Masking a value never touches the
  original row. Instead a new :data:`AuditEventType.REDACTION` event is appended
  naming the TARGET ``event_id``, the payload KEYS to mask, the acting admin,
  and the reason. The original event (and its hash/signature) is untouched, so
  the chain still verifies; reads mask the named keys AT VIEW TIME via
  :func:`apply_redactions`. The ACT of redacting is thus itself a permanent,
  attributable, signed record -- exactly what a regulator expects.
* **Legal hold is an append-only marker.** :func:`place_legal_hold` /
  :func:`release_legal_hold` append :data:`AuditEventType.LEGAL_HOLD` /
  ``LEGAL_HOLD_RELEASE`` events for a thread. A thread is *on hold* when it has
  more holds than releases (:func:`is_on_legal_hold`).
* **Retention is PLANNING here, not physical deletion.** :func:`plan_retention`
  returns which threads are eligible to purge given a cutoff date, ALWAYS
  excluding threads on legal hold. The privileged PHYSICAL purge (which would
  require temporarily relaxing the append-only trigger under a dedicated DB
  role) is a deployment-owned operational action, deliberately not performed by
  this offline core -- see ``NEXT_STEPS.md`` / ``DATA_ARCHITECTURE.md``.

Every write here flows through :func:`~app.backend.audit.record.record_event`,
so redaction and hold actions are themselves hash-chained and signed like any
other event, and are offline no-ops under the ``NullAuditSink`` default.

All functions are read/append only -- there is NO update or delete path, so the
append-only contract is structurally preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.record import record_event
from app.backend.audit.sink import AuditSink
from app.shared.settings import Settings

__all__ = [
    "REDACTED_PLACEHOLDER",
    "RetentionPlan",
    "record_redaction",
    "apply_redactions",
    "place_legal_hold",
    "release_legal_hold",
    "is_on_legal_hold",
    "plan_retention",
]

#: The value a masked payload field is replaced with at view time.
REDACTED_PLACEHOLDER = "\u3010REDACTED\u3011"


def _thread_state(state_thread_id: str, tdb_code: str = "") -> Any:
    """Build a minimal state-like object carrying the identity record_event reads.

    Admin actions are not tied to a live ``SaiseiState``; ``record_event`` only
    needs ``tdb_code`` / ``hojin_bango`` (for the row) and the thread_id (passed
    explicitly), so a tiny shim suffices and keeps the version-hash computation
    well-defined (empty inputs -> a stable hash).
    """

    class _S:
        tdb_code = ""
        hojin_bango = ""
        shisanhyo: list[Any] = []
        tdb_score = None
        working_capital_gap = None
        net_worth = None
        is_insolvent = None

    shim = _S()
    shim.tdb_code = tdb_code
    return shim


def record_redaction(
    thread_id: str,
    target_event_id: str,
    redact_keys: list[str],
    *,
    reason: str,
    actor: str,
    tdb_code: str = "",
    settings: Settings | None = None,
    sink: AuditSink | None = None,
) -> None:
    """Append a redaction directive for ``target_event_id`` (append-only).

    Records a :data:`AuditEventType.REDACTION` event naming the target event, the
    payload keys to mask, the acting admin, and the reason. The target row is
    NEVER edited or deleted; :func:`apply_redactions` masks the named keys when
    the thread is read. Best-effort / offline-safe via ``record_event``.

    Args:
        thread_id: The thread the target event belongs to (keeps the redaction
            in the same hash chain as what it redacts).
        target_event_id: The ``event_id`` whose payload keys are to be masked.
        redact_keys: The payload keys to mask at view time.
        reason: Why the redaction was made (recorded for the examiner).
        actor: The administrator performing the redaction.
        tdb_code: Optional borrower code for the redaction row.
        settings: Optional settings override.
        sink: Optional sink override.
    """
    record_event(
        AuditEventType.REDACTION,
        state=_thread_state(thread_id, tdb_code),
        payload={
            "target_event_id": target_event_id,
            "redact_keys": list(redact_keys),
            "reason": reason,
        },
        actor=actor,
        thread_id=thread_id,
        settings=settings,
        sink=sink,
    )


def apply_redactions(events: list[AuditEvent]) -> list[AuditEvent]:
    """Return ``events`` with view-time masking applied per recorded redactions.

    Scans the list for :data:`AuditEventType.REDACTION` events and, for each,
    masks the named ``redact_keys`` in the payload of the targeted event
    (replacing each value with :data:`REDACTED_PLACEHOLDER`). The returned events
    are COPIES -- the stored rows are never mutated, so the on-disk chain and
    signatures are untouched. The redaction events themselves are passed through
    unchanged so the examiner still sees who redacted what and why.

    Note: a masked event's recomputed content_hash will no longer match the
    stored hash (that is the whole point -- the displayed payload differs from
    the sealed one). Chain/signature verification must therefore run on the RAW
    events (``sink.read``), and masking is applied only for DISPLAY. Callers that
    both verify and display should verify first, then mask.

    Args:
        events: Raw events in write order (e.g. from ``AuditSink.read``).

    Returns:
        A new list with redacted payloads masked (input list not mutated).
    """
    redactions: dict[str, set[str]] = {}
    for event in events:
        if event.event_type is AuditEventType.REDACTION:
            target = str(event.payload.get("target_event_id", "") or "")
            keys = event.payload.get("redact_keys", []) or []
            if target:
                redactions.setdefault(target, set()).update(str(k) for k in keys)

    if not redactions:
        return list(events)

    masked: list[AuditEvent] = []
    for event in events:
        keys = redactions.get(event.event_id)
        if not keys:
            masked.append(event)
            continue
        new_payload = dict(event.payload)
        for key in keys:
            if key in new_payload:
                new_payload[key] = REDACTED_PLACEHOLDER
        masked.append(event.model_copy(update={"payload": new_payload}))
    return masked


def place_legal_hold(
    thread_id: str,
    *,
    reason: str,
    actor: str,
    tdb_code: str = "",
    settings: Settings | None = None,
    sink: AuditSink | None = None,
) -> None:
    """Append a legal-hold marker for ``thread_id`` (append-only).

    While an unreleased hold exists, :func:`plan_retention` excludes the thread.
    """
    record_event(
        AuditEventType.LEGAL_HOLD,
        state=_thread_state(thread_id, tdb_code),
        payload={"reason": reason},
        actor=actor,
        thread_id=thread_id,
        settings=settings,
        sink=sink,
    )


def release_legal_hold(
    thread_id: str,
    *,
    reason: str,
    actor: str,
    tdb_code: str = "",
    settings: Settings | None = None,
    sink: AuditSink | None = None,
) -> None:
    """Append a legal-hold-release marker for ``thread_id`` (append-only)."""
    record_event(
        AuditEventType.LEGAL_HOLD_RELEASE,
        state=_thread_state(thread_id, tdb_code),
        payload={"reason": reason},
        actor=actor,
        thread_id=thread_id,
        settings=settings,
        sink=sink,
    )


def is_on_legal_hold(events: list[AuditEvent]) -> bool:
    """Return whether a thread is currently on legal hold.

    A thread is on hold when it has strictly more LEGAL_HOLD events than
    LEGAL_HOLD_RELEASE events (holds and releases pair up; an unmatched hold
    means an active hold). Order-independent and idempotent-safe.

    Args:
        events: The thread's events (e.g. from ``AuditSink.read``).

    Returns:
        True iff an unreleased hold is in force.
    """
    holds = sum(1 for e in events if e.event_type is AuditEventType.LEGAL_HOLD)
    releases = sum(1 for e in events if e.event_type is AuditEventType.LEGAL_HOLD_RELEASE)
    return holds > releases


@dataclass(frozen=True)
class RetentionPlan:
    """Result of planning a retention sweep (PLANNING only -- nothing deleted).

    Attributes:
        cutoff: The ISO 8601 cutoff; a thread is purge-eligible only if ALL its
            events are at or before this timestamp.
        purgeable: Thread ids eligible to purge (old enough, not on hold).
        retained_recent: Thread ids kept because they have an event after cutoff.
        retained_on_hold: Thread ids kept because they are on legal hold (these
            are excluded even if old -- legal hold always wins).
    """

    cutoff: str
    purgeable: list[str] = field(default_factory=list)
    retained_recent: list[str] = field(default_factory=list)
    retained_on_hold: list[str] = field(default_factory=list)


def plan_retention(threads: dict[str, list[AuditEvent]], cutoff: str) -> RetentionPlan:
    """Plan which threads are eligible to purge at ``cutoff`` (NOTHING deleted).

    A thread is purgeable iff (a) it is NOT on legal hold and (b) every one of
    its events has ``created_at <= cutoff`` (so no recent activity is lost).
    Legal hold ALWAYS wins: a held thread is retained even if entirely old.

    This is deliberately PURE PLANNING. It performs no deletion -- the physical
    purge is a privileged, deployment-owned operational step (it must relax the
    append-only trigger under a dedicated role and is logged separately). This
    function gives an operator the exact, hold-respecting list to act on.

    Args:
        threads: Mapping of thread_id -> its events (e.g. read per thread).
        cutoff: ISO 8601 cutoff timestamp (inclusive). ISO 8601 sorts lexically.

    Returns:
        A :class:`RetentionPlan`.
    """
    purgeable: list[str] = []
    retained_recent: list[str] = []
    retained_on_hold: list[str] = []
    for thread_id, events in threads.items():
        if is_on_legal_hold(events):
            retained_on_hold.append(thread_id)
            continue
        if events and all(e.created_at <= cutoff for e in events):
            purgeable.append(thread_id)
        else:
            retained_recent.append(thread_id)
    return RetentionPlan(
        cutoff=cutoff,
        purgeable=sorted(purgeable),
        retained_recent=sorted(retained_recent),
        retained_on_hold=sorted(retained_on_hold),
    )
