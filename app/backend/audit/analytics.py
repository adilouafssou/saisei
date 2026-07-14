"""Cross-thread audit-ledger analytics (audit-ledger hardening).

The ledger already exposes two READ surfaces: per-thread ``read`` (one borrower's
ordered events + chain verdict) and cross-thread ``query`` (raw events across all
borrowers matching :class:`~app.backend.audit.sink.AuditQuery`). What a
supervisor / second-line / regulator actually opens a dashboard to ask is one
level up from raw rows:

* how much activity, of what kinds, over what window?
* who acted, and how often? (per-banker attribution)
* which borrowers carry the most decision activity?
* of the human decisions, how many were approve / revise / reject?
* what is the GOVERNANCE posture -- any redactions, any active legal holds?

This module computes those aggregations DETERMINISTICALLY from a list of events,
so the same offline / Postgres ``query`` result feeds the same summary. It is the
aggregation layer on top of the existing query surface -- it adds NO new storage
and NO mutation path (the ledger stays append-only); it only summarises events a
caller already has the right to read.

Design / safety posture
-----------------------
* **Pure + deterministic.** :func:`summarise` takes ``list[AuditEvent]`` and
  returns a frozen :class:`AuditAnalytics`. Same events -> same summary, no
  network, no clock, no LLM. The HTTP layer fetches the events via the sink and
  hands them here, exactly like the raw query endpoint.
* **Counts only, never figures.** Analytics counts EVENTS and categorises them;
  it never re-derives, sums, or reinterprets any monetary figure in a payload.
  It informs oversight; it has no vote in any verdict, route, or number.
* **Offline-safe.** With no audit backend the sink returns ``[]`` and this
  returns an all-zero summary -- byte-stable, no special-casing.
* **Redaction-aware.** Decision counts read only the ``decision`` discriminator
  (never a redactable free-text note), so masking a payload field never changes
  an aggregate. Redaction events are themselves counted (governance posture).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.backend.audit.admin import is_on_legal_hold
from app.backend.audit.audit_log import AuditEvent, AuditEventType

__all__ = [
    "AuditAnalytics",
    "summarise",
]

#: The human-decision verdicts surfaced as their own breakdown. These mirror the
#: HITL resume contract (approve / revise / reject); any other / missing value
#: is bucketed under ``"unknown"`` so the breakdown always sums to the
#: human_decision total.
_DECISION_KEYS: tuple[str, ...] = ("approve", "revise", "reject")


@dataclass(frozen=True)
class AuditAnalytics:
    """A deterministic, book-level summary of a set of audit events.

    Every count is over the events PASSED IN (already filtered by the caller's
    :class:`~app.backend.audit.sink.AuditQuery`), so the same summary describes
    "the whole ledger" or "one banker in March" depending on the query.

    Attributes:
        total_events: Number of events summarised.
        by_event_type: Count per :class:`AuditEventType` value (only non-zero
            kinds appear).
        by_actor: Count per actor id (e.g. 'system', a banker id).
        by_borrower: Count per 7-digit ``tdb_code`` (empty codes excluded).
        decisions: Count per human decision verdict (approve / revise / reject /
            unknown); keys with a zero count are omitted. Sums to the
            ``human_decision`` count in ``by_event_type``.
        distinct_actors: Number of distinct actor ids seen.
        distinct_borrowers: Number of distinct non-empty ``tdb_code``s seen.
        distinct_threads: Number of distinct ``thread_id``s seen.
        active_legal_holds: Number of threads currently on legal hold (an
            unreleased hold) among the summarised events -- a governance signal.
        redaction_events: Number of redaction directives recorded -- a
            governance signal (how much has been masked).
        earliest: ISO 8601 ``created_at`` of the oldest event, or '' if none.
        latest: ISO 8601 ``created_at`` of the newest event, or '' if none.
    """

    total_events: int = 0
    by_event_type: dict[str, int] = field(default_factory=dict)
    by_actor: dict[str, int] = field(default_factory=dict)
    by_borrower: dict[str, int] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)
    distinct_actors: int = 0
    distinct_borrowers: int = 0
    distinct_threads: int = 0
    active_legal_holds: int = 0
    redaction_events: int = 0
    earliest: str = ""
    latest: str = ""


def _decision_of(event: AuditEvent) -> str:
    """Return the normalised decision verdict for a human_decision event.

    Reads only the ``decision`` discriminator from the payload (never a
    redactable free-text note), normalises case/whitespace, and buckets any
    unrecognised / missing value under ``"unknown"`` so the breakdown always
    totals the human_decision count.
    """
    raw = str(event.payload.get("decision", "") or "").strip().lower()
    return raw if raw in _DECISION_KEYS else "unknown"


def summarise(events: list[AuditEvent]) -> AuditAnalytics:
    """Compute a deterministic book-level summary of ``events``.

    Pure aggregation: counts and categorises the events, computes the time span,
    distinct cardinalities, the human-decision breakdown, and the governance
    signals (active legal holds, redaction count). It never reinterprets a
    monetary figure -- it only counts events.

    Legal-hold status is computed PER THREAD using the same
    :func:`~app.backend.audit.admin.is_on_legal_hold` rule the retention sweep
    uses (holds minus releases), so the active-hold count is consistent with the
    rest of the ledger. NOTE: it reflects only the hold/release events PRESENT in
    ``events``; a query that filters them out will report 0 active holds for the
    slice, which is the correct answer for that slice.

    Args:
        events: The events to summarise (e.g. from ``AuditSink.query``), in any
            order.

    Returns:
        The :class:`AuditAnalytics` summary (all-zero for an empty list).
    """
    if not events:
        return AuditAnalytics()

    by_event_type: Counter[str] = Counter()
    by_actor: Counter[str] = Counter()
    by_borrower: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    threads: set[str] = set()
    events_by_thread: dict[str, list[AuditEvent]] = {}
    timestamps: list[str] = []
    redaction_events = 0

    for event in events:
        by_event_type[event.event_type.value] += 1
        by_actor[event.actor] += 1
        if event.tdb_code:
            by_borrower[event.tdb_code] += 1
        threads.add(event.thread_id)
        events_by_thread.setdefault(event.thread_id, []).append(event)
        if event.created_at:
            timestamps.append(event.created_at)
        if event.event_type is AuditEventType.HUMAN_DECISION:
            decisions[_decision_of(event)] += 1
        elif event.event_type is AuditEventType.REDACTION:
            redaction_events += 1

    active_legal_holds = sum(
        1 for thread_events in events_by_thread.values() if is_on_legal_hold(thread_events)
    )

    return AuditAnalytics(
        total_events=len(events),
        by_event_type=dict(by_event_type),
        by_actor=dict(by_actor),
        by_borrower=dict(by_borrower),
        decisions=dict(decisions),
        distinct_actors=len(by_actor),
        distinct_borrowers=len(by_borrower),
        distinct_threads=len(threads),
        active_legal_holds=active_legal_holds,
        redaction_events=redaction_events,
        earliest=min(timestamps) if timestamps else "",
        latest=max(timestamps) if timestamps else "",
    )
