"""Loan-facility lifecycle models for Saisei.

The loan (融資案件) is modelled as a first-class, **event-sourced** domain
entity. Current status is never stored as a mutable field; it is *derived* from
an ordered, append-only log of :class:`LoanEvent` records. This matches the
project's append-only, hash-chained audit ledger and its replayability
principle: any loan's state at any point is reconstructable by replaying its
events.

Monetary fields are strict integer yen (see :mod:`app.shared.models.money`):
principal can never silently become a float.

The set of statuses (:class:`LoanStatus`) is **closed**, in the same spirit as
:class:`app.shared.models.classification.FsaClass`. The lifecycle spans the
whole arc so both origination and the existing turnaround / workout work can
attach to it:

    APPLIED (申込) → UNDER_REVIEW (審査中) → APPROVED (承認) / DECLINED (謝絶)
    APPROVED → DISBURSED (実行) → PERFORMING (正常)
    PERFORMING → RESTRUCTURED (条件変更) → PERFORMING | WORKOUT (管理回収)
    PERFORMING → WORKOUT (管理回収)
    PERFORMING | RESTRUCTURED → CLOSED (完済)
    WORKOUT → CLOSED (完済) | WRITTEN_OFF (償却)

Design boundary (the product invariant, restated for this layer): the loan
ledger computes balances and statuses **deterministically** and is the source
of truth. The LLM never produces or alters a figure here, and never performs a
transition in a human's place — banker-authority transitions are gated upstream
(see :data:`HITL_GATED_TRANSITIONS` and ``docs/en/LOAN_LIFECYCLE.md``).

This module is the canonical location under ``app.shared.models.loan``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.shared.constants import (
    ORIGINATION_MAX_FACILITY_SALES_MULTIPLE,
    ORIGINATION_TDB_APPROVE_FLOOR,
    PROVISION_RATE_BANKRUPT,
    PROVISION_RATE_DOUBTFUL,
    PROVISION_RATE_NEEDS_ATTENTION,
    PROVISION_RATE_NORMAL,
    PROVISION_RATE_SPECIAL_ATTENTION,
)
from app.shared.models.classification import FsaClass
from app.shared.models.money import JPY, format_jpy

#: Base reserve ratio per FSA class (要管理先 sub-tier handled in
#: ``provision_rate_for`` via the special_attention flag on 要注意先).
_PROVISION_RATE: dict[FsaClass, float] = {
    FsaClass.SEIJOSAKI: PROVISION_RATE_NORMAL,
    FsaClass.YOCHUISAKI: PROVISION_RATE_NEEDS_ATTENTION,
    FsaClass.HATAN_KENENSAKI: PROVISION_RATE_DOUBTFUL,
    FsaClass.JISSHITSU_HATANSAKI: PROVISION_RATE_BANKRUPT,
    FsaClass.HATANSAKI: PROVISION_RATE_BANKRUPT,
}

__all__ = [
    "HITL_GATED_TRANSITIONS",
    "SERVICING_TRANSITIONS",
    "Loan",
    "LoanEvent",
    "LoanStatus",
    "OriginationDecision",
    "OriginationRecommendation",
    "current_status",
    "is_servicing_transition",
    "loan_events_reducer",
    "max_facility_amount",
    "outstanding_principal",
    "outstanding_principal_for_state",
    "proposed_origination_decision",
    "proposed_servicing_transition",
    "provision_amount",
    "provision_rate_for",
    "proposed_transition_for",
    "recommend_origination",
]


class LoanStatus(StrEnum):
    """Loan-facility lifecycle status (融資ステータス) — a closed set.

    Values are romanized identifiers; use ``.kanji`` / ``.english`` for display.

    Members (origination → servicing → distress → terminal):
        APPLIED:       申込 — Applied. A facility request exists, not yet reviewed.
        UNDER_REVIEW:  審査中 — Under Review. Underwriting / 稟議 in progress.
        APPROVED:      承認 — Approved, awaiting disbursement.
        DECLINED:      謝絶 — Declined (terminal).
        DISBURSED:     実行 — Disbursed / drawn down.
        PERFORMING:    正常 — Performing, on the agreed repayment schedule.
        RESTRUCTURED:  条件変更 — Restructured (リスケ); a 貸出条件緩和債権.
        WORKOUT:       管理回収 — Workout / managed recovery.
        CLOSED:        完済 — Fully repaid (terminal).
        WRITTEN_OFF:   償却 — Written off (terminal).

    Routing properties:
        ``is_terminal``: True for DECLINED, CLOSED, WRITTEN_OFF — no further
            transitions are legal.
        ``is_distressed``: True for RESTRUCTURED and WORKOUT — designed to map
            onto the existing FsaClass.requires_turnaround / requires_workout
            routing in a follow-up MR.
    """

    APPLIED = "applied"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    DECLINED = "declined"
    DISBURSED = "disbursed"
    PERFORMING = "performing"
    RESTRUCTURED = "restructured"
    WORKOUT = "workout"
    CLOSED = "closed"
    WRITTEN_OFF = "written_off"

    @property
    def kanji(self) -> str:
        """Return the Japanese label for this status."""
        return _KANJI[self]

    @property
    def english(self) -> str:
        """Return the English label for this status."""
        return _ENGLISH[self]

    @property
    def is_terminal(self) -> bool:
        """Whether this is a terminal status (no further transitions legal)."""
        return self in _TERMINAL

    @property
    def is_distressed(self) -> bool:
        """Whether this status denotes a distressed facility (条件変更 / 管理回収).

        Designed to map onto the existing FSA turnaround / workout routing in a
        follow-up MR.
        """
        return self in (LoanStatus.RESTRUCTURED, LoanStatus.WORKOUT)

    @property
    def allowed_transitions(self) -> frozenset[LoanStatus]:
        """Return the set of statuses this status may legally transition to."""
        return _ALLOWED_TRANSITIONS[self]

    def can_transition_to(self, target: LoanStatus) -> bool:
        """Whether a transition from this status to ``target`` is legal."""
        return target in _ALLOWED_TRANSITIONS[self]


_KANJI: dict[LoanStatus, str] = {
    LoanStatus.APPLIED: "申込",
    LoanStatus.UNDER_REVIEW: "審査中",
    LoanStatus.APPROVED: "承認",
    LoanStatus.DECLINED: "謝絶",
    LoanStatus.DISBURSED: "実行",
    LoanStatus.PERFORMING: "正常",
    LoanStatus.RESTRUCTURED: "条件変更",
    LoanStatus.WORKOUT: "管理回収",
    LoanStatus.CLOSED: "完済",
    LoanStatus.WRITTEN_OFF: "償却",
}

_ENGLISH: dict[LoanStatus, str] = {
    LoanStatus.APPLIED: "Applied",
    LoanStatus.UNDER_REVIEW: "Under Review",
    LoanStatus.APPROVED: "Approved",
    LoanStatus.DECLINED: "Declined",
    LoanStatus.DISBURSED: "Disbursed",
    LoanStatus.PERFORMING: "Performing",
    LoanStatus.RESTRUCTURED: "Restructured",
    LoanStatus.WORKOUT: "Workout",
    LoanStatus.CLOSED: "Closed",
    LoanStatus.WRITTEN_OFF: "Written Off",
}

#: Terminal statuses: once reached, no further transition is legal.
_TERMINAL: frozenset[LoanStatus] = frozenset(
    {LoanStatus.DECLINED, LoanStatus.CLOSED, LoanStatus.WRITTEN_OFF}
)

#: The single source of truth for the legal state machine. Each status maps to
#: the closed set of statuses it may legally transition to. Terminal statuses
#: map to the empty set.
_ALLOWED_TRANSITIONS: dict[LoanStatus, frozenset[LoanStatus]] = {
    LoanStatus.APPLIED: frozenset({LoanStatus.UNDER_REVIEW, LoanStatus.DECLINED}),
    LoanStatus.UNDER_REVIEW: frozenset({LoanStatus.APPROVED, LoanStatus.DECLINED}),
    LoanStatus.APPROVED: frozenset({LoanStatus.DISBURSED, LoanStatus.DECLINED}),
    LoanStatus.DECLINED: frozenset(),
    LoanStatus.DISBURSED: frozenset({LoanStatus.PERFORMING}),
    # PERFORMING / RESTRUCTURED carry a legal SELF-transition: a partial
    # repayment (一部入金 / 元本均等返済) advances the facility's outstanding
    # balance DOWN while its status is unchanged -- the facility stays performing
    # / restructured. The self-loop carries the repaid amount on the event
    # (``principal_repaid``); the status is the same. This is a non-distress
    # operational fact, so the pair is in SERVICING_TRANSITIONS, not the gated set.
    LoanStatus.PERFORMING: frozenset(
        {
            LoanStatus.PERFORMING,
            LoanStatus.RESTRUCTURED,
            LoanStatus.WORKOUT,
            LoanStatus.CLOSED,
        }
    ),
    LoanStatus.RESTRUCTURED: frozenset(
        {
            LoanStatus.RESTRUCTURED,
            LoanStatus.PERFORMING,
            LoanStatus.WORKOUT,
            LoanStatus.CLOSED,
        }
    ),
    LoanStatus.WORKOUT: frozenset({LoanStatus.CLOSED, LoanStatus.WRITTEN_OFF}),
    LoanStatus.CLOSED: frozenset(),
    LoanStatus.WRITTEN_OFF: frozenset(),
}

#: Transitions that REQUIRE a human (banker) sign-off before they may be
#: recorded. Human authority is non-negotiable: the system proposes, the banker
#: decides. These are enforced upstream (graph / API); this module documents
#: them as the canonical list. A pair ``(from, to)`` is HITL-gated.
HITL_GATED_TRANSITIONS: frozenset[tuple[LoanStatus, LoanStatus]] = frozenset(
    {
        (LoanStatus.UNDER_REVIEW, LoanStatus.APPROVED),
        (LoanStatus.UNDER_REVIEW, LoanStatus.DECLINED),
        (LoanStatus.APPROVED, LoanStatus.DECLINED),
        (LoanStatus.PERFORMING, LoanStatus.RESTRUCTURED),
        (LoanStatus.PERFORMING, LoanStatus.WORKOUT),
        (LoanStatus.RESTRUCTURED, LoanStatus.WORKOUT),
        (LoanStatus.WORKOUT, LoanStatus.WRITTEN_OFF),
    }
)

#: The closed set of SERVICING transitions: the non-distress, non-credit,
#: deterministic operational moves along the performing arc of a facility's
#: life. Disjoint from :data:`HITL_GATED_TRANSITIONS` by construction (asserted
#: in tests): a servicing transition is an operational fact (a facility entered
#: normal servicing; a facility was fully repaid), never a banker-authority
#: credit / distress judgement. The distress transitions out of PERFORMING /
#: RESTRUCTURED (条件変更 / 管理回収 / 償却) are deliberately NOT here -- they are
#: owned by the depth half and stay HITL-gated. A pair ``(from, to)`` is a
#: servicing transition.
SERVICING_TRANSITIONS: frozenset[tuple[LoanStatus, LoanStatus]] = frozenset(
    {
        (LoanStatus.DISBURSED, LoanStatus.PERFORMING),
        # Partial-repayment self-loops (一部入金): outstanding declines, status
        # unchanged. Non-distress operational facts, so part of the servicing set.
        (LoanStatus.PERFORMING, LoanStatus.PERFORMING),
        (LoanStatus.RESTRUCTURED, LoanStatus.RESTRUCTURED),
        (LoanStatus.PERFORMING, LoanStatus.CLOSED),
        (LoanStatus.RESTRUCTURED, LoanStatus.CLOSED),
    }
)


def is_servicing_transition(src: LoanStatus, dst: LoanStatus) -> bool:
    """Whether ``(src, dst)`` is a deterministic servicing transition.

    A servicing transition is a non-distress, non-credit operational move along
    the performing arc (see :data:`SERVICING_TRANSITIONS`). True iff the pair is
    in that closed set; in particular it is never a HITL-gated credit / distress
    transition.
    """
    return (src, dst) in SERVICING_TRANSITIONS


class LoanEvent(BaseModel):
    """A single, immutable lifecycle event for a loan facility.

    Events form an ordered, append-only log; the loan's current status is
    derived by replaying them (see :func:`current_status`). An event records a
    transition INTO ``status`` and is the audit-bearing record of who/when.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: LoanStatus = Field(description="The status the loan transitions into.")
    at: dt.datetime = Field(description="Timestamp of the transition (UTC).")
    actor: str = Field(description="Identity that recorded the transition (banker id / system).")
    note: str = Field(default="", description="Optional free-text note (audit).")
    principal_repaid: int = Field(
        default=0,
        ge=0,
        description=(
            "Principal repaid in THIS event (integer yen, >= 0). Non-zero only "
            "on a partial-repayment self-event (一部入金: PERFORMING -> PERFORMING "
            "or RESTRUCTURED -> RESTRUCTURED); 0 (the default) on every status "
            "transition. Summed across the log to derive the live outstanding "
            "principal (see :func:`outstanding_principal`)."
        ),
    )
    principal_disbursed: int = Field(
        default=0,
        ge=0,
        description=(
            "Original principal drawn down at disbursement (integer yen, >= 0). "
            "Non-zero only on the DISBURSED (実行) event, where it stamps the "
            "facility's principal baseline ONTO the append-only ledger so the "
            "live outstanding balance is recoverable from the log alone (no "
            "external lender-stakes snapshot needed). 0 (the default) on every "
            "other event."
        ),
    )


class Loan(BaseModel):
    """A loan facility (融資案件) as an event-sourced aggregate.

    The aggregate carries immutable identity and terms plus its ordered event
    log. Current status is NOT a stored field — it is derived from ``events``
    via :attr:`status`, so the record stays replayable and tamper-evident.

    The first event MUST be an ``APPLIED`` event (the lifecycle entry point);
    every subsequent event MUST be a legal transition from the prior status.
    Both invariants are enforced at validation time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    loan_id: str = Field(description="Stable facility identifier.")
    hojin_bango: str = Field(description="13-digit corporate number of the borrower.")
    principal: JPY = Field(description="Original principal / 元本 (integer yen).")
    originated_on: dt.date = Field(description="Application / origination date.")
    events: tuple[LoanEvent, ...] = Field(
        description="Ordered, append-only lifecycle event log (oldest first)."
    )

    def model_post_init(self, __context: object) -> None:
        """Validate the event log forms a legal lifecycle from APPLIED."""
        _validate_event_chain(self.events)
        # Cumulative repayments may never exceed the original principal: a
        # facility cannot repay more than it borrowed. (Per-event sign / status
        # placement is enforced in _validate_event_chain; this is the one check
        # that needs the principal, so it lives on the aggregate.)
        repaid = sum(e.principal_repaid for e in self.events)
        if repaid > int(self.principal):
            raise ValueError(
                "Cumulative principal_repaid "
                f"({repaid}) exceeds original principal ({int(self.principal)})."
            )

    @property
    def status(self) -> LoanStatus:
        """Current status, derived from the event log."""
        return current_status(self.events)

    @property
    def outstanding(self) -> int:
        """Live outstanding principal (残高) = original minus cumulative repaid."""
        return outstanding_principal(int(self.principal), self.events)

    @property
    def is_open(self) -> bool:
        """Whether the facility is still open (current status not terminal)."""
        return not self.status.is_terminal

    def summary(self) -> str:
        """Human-readable one-line summary with a ¥-formatted principal."""
        return (
            f"{self.loan_id} | {format_jpy(int(self.principal))} | "
            f"{self.status.kanji} ({self.status.english})"
        )


def _validate_event_chain(events: Sequence[LoanEvent]) -> None:
    """Raise ValueError unless ``events`` is a legal lifecycle from APPLIED.

    Rules:
        * the log must be non-empty;
        * the first event must be APPLIED;
        * each subsequent status must be a legal transition from the prior one.
    """
    if not events:
        raise ValueError("A loan must have at least one event (APPLIED).")
    if events[0].status is not LoanStatus.APPLIED:
        raise ValueError(f"First loan event must be APPLIED, got {events[0].status!r}.")
    prev: LoanStatus = events[0].status
    if events[0].principal_repaid:
        raise ValueError("The APPLIED event cannot carry a principal repayment.")
    if events[0].principal_disbursed:
        raise ValueError("principal_disbursed may only appear on the DISBURSED event.")
    for event in events[1:]:
        if not prev.can_transition_to(event.status):
            raise ValueError(f"Illegal loan transition {prev.value} -> {event.status.value}.")
        # A principal repayment may only be recorded on a partial-repayment
        # SELF-event (一部入金): the status is unchanged and the pair is a
        # servicing self-loop. Recording a repayment on any status-CHANGING
        # event (e.g. PERFORMING -> CLOSED) is rejected -- the close event marks
        # completion, it does not itself carry the final payment amount.
        if event.principal_repaid and not (
            prev is event.status and is_servicing_transition(prev, event.status)
        ):
            raise ValueError(
                "principal_repaid may only appear on a repayment self-event "
                f"(got {prev.value} -> {event.status.value})."
            )
        # The principal baseline is stamped exactly once, on the DISBURSED
        # (実行) event -- the moment principal is actually drawn down.
        if event.principal_disbursed and event.status is not LoanStatus.DISBURSED:
            raise ValueError(
                "principal_disbursed may only appear on the DISBURSED event "
                f"(got status {event.status.value})."
            )
        prev = event.status


def current_status(events: Iterable[LoanEvent]) -> LoanStatus:
    """Derive the current status from an ordered event log.

    Args:
        events: The ordered (oldest-first) lifecycle events.

    Returns:
        The status of the last event.

    Raises:
        ValueError: If ``events`` is empty.
    """
    last: LoanEvent | None = None
    for last in events:  # noqa: B007 - we want the final element
        pass
    if last is None:
        raise ValueError("Cannot derive status from an empty event log.")
    return last.status


def outstanding_principal(original: int, events: Iterable[LoanEvent]) -> int:
    """Derive a facility's live outstanding principal (残高) from its event log.

    The outstanding balance is the original principal minus the cumulative
    ``principal_repaid`` recorded across the append-only log
    (元本−累計返済額). Like :func:`current_status`, it is *derived* from the log,
    never stored, so a facility's balance at any point is reconstructable by
    replay. This is the truthful exposure figure the loan-loss provision
    (:func:`provision_amount`) and the 完済 proposal
    (:func:`proposed_servicing_transition`) should reason over, replacing the
    stale lender-stakes snapshot.

    Args:
        original: The original principal in integer yen (>= 0).
        events: The ordered lifecycle events (only ``principal_repaid`` is read).

    Returns:
        The outstanding principal in integer yen, clamped at 0 (a fully-repaid
        facility is 0, never negative).

    Raises:
        ValueError: If ``original`` is negative.
    """
    if original < 0:
        raise ValueError("Original principal cannot be negative.")
    repaid = sum(e.principal_repaid for e in events)
    return max(0, original - repaid)


def outstanding_principal_for_state(state: object) -> int:
    """Derive a facility's live outstanding principal from a graph-state object.

    The single shared seam every call site (workout / HITL loan summary / UI /
    servicing) should use to get a facility's TRUTHFUL outstanding balance,
    replacing the older ``sum(lender_stakes)`` proxy that ignored repayments.

    The principal baseline is resolved LEDGER-FIRST: when the DISBURSED event
    carries a ``principal_disbursed`` stamp (the self-contained record of the
    drawn principal), that is the baseline -- so the live balance is recoverable
    from the loan log ALONE, with no external lender-stakes snapshot. When the
    ledger has no such stamp (a pre-stamp facility, or an intake bootstrap), the
    baseline falls back to the sum of ``lender_stakes`` -- exactly what intake
    sets the facility principal to. Either way the live balance is the baseline
    minus the cumulative ``principal_repaid`` on the log (元本−累計返済). With no
    repayments this equals the old proxy exactly (backward-compatible); once a
    facility amortizes it is the real declining 残高.

    Read defensively via ``getattr`` so it works on a live ``SaiseiState`` or any
    state-like object, and never raises (a malformed log degrades to the
    stakes baseline). Returns 0 when neither a disbursed stamp nor stake data is
    present, so a provision / display line is simply omitted rather than guessed.

    Args:
        state: The graph state (or state-like object) for the facility. Reads
            ``loan_events`` (the disbursed stamp + repayment log) and
            ``lender_stakes`` (the fallback principal baseline).

    Returns:
        The live outstanding principal in integer yen, clamped at 0.
    """
    raw_events = getattr(state, "loan_events", None) or []
    try:
        events = [LoanEvent.model_validate(e) for e in raw_events]
    except Exception:  # noqa: BLE001 - degrade to the stakes-only baseline below
        events = []

    # Ledger-first: a DISBURSED event carrying principal_disbursed is the
    # self-contained baseline (recoverable from the log alone).
    disbursed = sum(e.principal_disbursed for e in events)
    if disbursed > 0:
        return outstanding_principal(disbursed, events)

    # Fallback: the lender-stakes snapshot (what intake bootstraps the principal
    # to) when the ledger carries no disbursed stamp.
    stakes = getattr(state, "lender_stakes", None) or {}
    baseline = sum(int(v) for v in stakes.values()) if stakes else 0
    if baseline <= 0:
        return 0
    return outstanding_principal(baseline, events)


def proposed_transition_for(fsa_class: FsaClass, current: LoanStatus) -> LoanStatus | None:
    """Propose the loan-status transition implied by an FSA classification.

    This is the **depth** bridge: it connects the existing FSA debtor
    classification (the turnaround / workout routing already in the codebase)
    to the loan-lifecycle spine, so a deteriorating classification becomes a
    loan-status transition.

    The mapping is advisory and deterministic — it *proposes* a target status;
    it never records the transition. Every transition it can return is
    HITL-gated (see :data:`HITL_GATED_TRANSITIONS`): the banker decides. This
    helper performs no graph wiring and has no authority of its own.

    Mapping (only from an active, non-distressed servicing status):
        * ``requires_turnaround`` (要注意先 / 破綻懸念先) → RESTRUCTURED (条件変更),
          proposed from PERFORMING only.
        * ``requires_workout`` (実質破綻先 / 破綻先) → WORKOUT (管理回収),
          proposed from PERFORMING or RESTRUCTURED.
        * 正常先 (Normal) and any status from which the target is not a legal
          transition → ``None`` (no proposal).

    Args:
        fsa_class: The borrower's current FSA debtor classification.
        current: The loan's current lifecycle status.

    Returns:
        The proposed target status, or ``None`` if no transition is implied or
        the implied transition is not legal from ``current``.
    """
    if fsa_class.requires_workout:
        target = LoanStatus.WORKOUT
    elif fsa_class.requires_turnaround:
        target = LoanStatus.RESTRUCTURED
    else:
        return None
    # Never propose a servicing SELF-loop (e.g. RESTRUCTURED -> RESTRUCTURED):
    # that pair is a non-distress partial-repayment servicing transition, not a
    # HITL-gated credit move. A facility already in the target distress state
    # has nothing to propose. Only a status-CHANGING, gated distress transition
    # is proposed.
    if current is target:
        return None
    return target if current.can_transition_to(target) else None


def proposed_servicing_transition(current: LoanStatus, outstanding: int) -> LoanStatus | None:
    """Propose the deterministic servicing transition for a performing facility.

    The **servicing** bridge: it advances a facility along the non-distress,
    operational arc of its life that neither the depth (distress) nor the
    breadth (origination) helper covers, closing the middle of the lifecycle.
    Like its siblings (:func:`proposed_transition_for`,
    :func:`recommend_origination`) it only *proposes* a deterministic target; it
    records nothing, performs no graph wiring, and has no authority of its own.

    Mapping (only the non-distress, non-credit servicing transitions):
        * DISBURSED → PERFORMING (正常): a drawn-down facility enters normal
          servicing -- an operational step, proposed whenever the current status
          is DISBURSED regardless of outstanding principal.
        * PERFORMING → CLOSED (完済): full repayment, proposed ONLY when
          ``outstanding`` has reached zero -- a fact, not a credit judgement.
        * any other status, or PERFORMING with outstanding principal remaining,
          → ``None`` (no servicing transition implied).

    It deliberately NEVER proposes a distress / credit transition
    (条件変更 / 管理回収 / 償却): those are owned by the depth half
    (:func:`proposed_transition_for`) and are HITL-gated. Every transition this
    helper can return is in :data:`SERVICING_TRANSITIONS` and, by construction,
    is a legal transition from ``current``. No LLM is involved.

    Args:
        current: The facility's current lifecycle status.
        outstanding: Outstanding principal in integer yen (>= 0). Only consulted
            for the PERFORMING → CLOSED (完済) proposal.

    Returns:
        The proposed servicing target status, or ``None`` when no servicing
        transition is implied.

    Raises:
        ValueError: If ``outstanding`` is negative.
    """
    if outstanding < 0:
        raise ValueError("Outstanding principal cannot be negative.")
    if current is LoanStatus.DISBURSED:
        return LoanStatus.PERFORMING
    if current in (LoanStatus.PERFORMING, LoanStatus.RESTRUCTURED) and outstanding == 0:
        return LoanStatus.CLOSED
    return None


def provision_rate_for(fsa_class: FsaClass, *, special_attention: bool = False) -> float:
    """Return the deterministic loan-loss reserve ratio (貸倒引当金率).

    Maps an FSA debtor classification to its self-assessment (自己査定) reserve
    ratio. ``special_attention=True`` selects the heavier 要管理先 sub-tier of
    要注意先 (mirroring how the classification layer models 要管理先 as a flag on
    a 要注意先 borrower rather than a separate class); it is only meaningful for
    要注意先 and is ignored for every other class.

    The ratios are auditable constants (see ``app.shared.constants``); no LLM is
    involved.

    Args:
        fsa_class: The borrower's FSA debtor classification.
        special_attention: When True and the class is 要注意先, use the heavier
            要管理先 reserve ratio.

    Returns:
        The reserve ratio as a decimal fraction (e.g. 0.70 = 70%).
    """
    if fsa_class is FsaClass.YOCHUISAKI:
        return (
            PROVISION_RATE_SPECIAL_ATTENTION
            if special_attention
            else PROVISION_RATE_NEEDS_ATTENTION
        )
    return _PROVISION_RATE[fsa_class]


def provision_amount(
    outstanding: int, fsa_class: FsaClass, *, special_attention: bool = False
) -> int:
    """Compute the loan-loss provision (貸倒引当金) in integer yen.

    The provision is ``outstanding * provision_rate_for(...)`` rounded to whole
    yen (banker's rounding via ``round``). It is a deterministic figure derived
    from outstanding principal and the FSA class — never produced or altered by
    a model.

    Args:
        outstanding: Outstanding principal in integer yen.
        fsa_class: The borrower's FSA debtor classification.
        special_attention: When True and the class is 要注意先, use the heavier
            要管理先 reserve ratio.

    Returns:
        The provision amount in integer yen.

    Raises:
        ValueError: If ``outstanding`` is negative.
    """
    if outstanding < 0:
        raise ValueError("Outstanding principal cannot be negative.")
    rate = provision_rate_for(fsa_class, special_attention=special_attention)
    return round(outstanding * rate)


# ---------------------------------------------------------------------------
# Origination (融資組成) — deterministic, advisory underwriting recommendation
# ---------------------------------------------------------------------------
#
# The BREADTH bridge, the origination mirror of the distress-side
# ``proposed_transition_for`` / ``provision_amount``. At the 稟議 gate
# (UNDER_REVIEW → APPROVED / DECLINED) it turns an applicant's TDB credit
# assessment into a deterministic credit recommendation plus a provisional
# facility ceiling. It only *proposes*: the UNDER_REVIEW → APPROVED / DECLINED
# transition it implies is HITL-gated (see ``HITL_GATED_TRANSITIONS``), so the
# banker is, by construction, the decider. No LLM is involved and the helper has
# no graph wiring or authority of its own — exactly the additive-spine posture
# the depth half landed with.


class OriginationDecision(StrEnum):
    """Deterministic credit recommendation at the 稟議 (origination) gate.

    ADVISORY ONLY — a *recommendation*, never a recorded decision. The actual
    UNDER_REVIEW → APPROVED / DECLINED transition is HITL-gated; the banker
    decides. Members:
        APPROVE: 承認推奨 — recommend approving the facility.
        DECLINE: 謝絶推奨 — recommend declining the facility.
    """

    APPROVE = "approve"
    DECLINE = "decline"

    @property
    def proposed_status(self) -> LoanStatus:
        """Return the loan status this recommendation proposes for the facility.

        APPROVE → APPROVED (承認); DECLINE → DECLINED (謝絶). Both are legal
        transitions from UNDER_REVIEW and both are HITL-gated.
        """
        return LoanStatus.APPROVED if self is OriginationDecision.APPROVE else LoanStatus.DECLINED


class OriginationRecommendation(BaseModel):
    """A deterministic, advisory origination recommendation for an applicant.

    Bundles the credit recommendation, the loan status it proposes, the
    provisional facility ceiling, and an auditable bilingual reason — mirroring
    the explainability of ``classification_reason`` on the distress side. Frozen
    and figure-only: nothing here records a transition or feeds a gate or route.

    Attributes:
        decision: The :class:`OriginationDecision` (APPROVE / DECLINE).
        proposed_status: The loan status the decision proposes (APPROVED /
            DECLINED) — always a legal, HITL-gated transition from UNDER_REVIEW.
        max_facility_amount: Provisional facility ceiling in integer yen
            (0 when DECLINE or when no sales figure is available).
        reason: A short bilingual explanation naming the decisive threshold.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: OriginationDecision = Field(
        description="The advisory credit recommendation (approve / decline)."
    )
    proposed_status: LoanStatus = Field(
        description="The HITL-gated loan status the recommendation proposes."
    )
    max_facility_amount: JPY = Field(
        description="Provisional facility ceiling in integer yen (0 if decline)."
    )
    reason: str = Field(description="Bilingual reason naming the decisive credit threshold.")


def proposed_origination_decision(
    tdb_score: int | None,
    *,
    anti_social_clear: bool = True,
) -> OriginationDecision:
    """Return the deterministic credit recommendation for an applicant.

    Rules (most-decisive first), mirroring the distress-side cascade style:

    * **DECLINE** when the anti-social-forces (反社会的勢力) check is NOT clear —
      a hard compliance bar that overrides any credit score.
    * **DECLINE** when ``tdb_score`` is missing or below
      :data:`ORIGINATION_TDB_APPROVE_FLOOR` — the applicant is not creditworthy
      enough to recommend a new facility.
    * **APPROVE** otherwise.

    Advisory only: this *proposes*; the UNDER_REVIEW → APPROVED / DECLINED
    transition it implies is HITL-gated. No LLM is involved.

    Args:
        tdb_score: The applicant's TDB credit score (1-100), or None.
        anti_social_clear: Whether the 反社会的勢力 screening returned clear.
            False forces DECLINE regardless of score.

    Returns:
        The :class:`OriginationDecision`.
    """
    if not anti_social_clear:
        return OriginationDecision.DECLINE
    if tdb_score is None or tdb_score < ORIGINATION_TDB_APPROVE_FLOOR:
        return OriginationDecision.DECLINE
    return OriginationDecision.APPROVE


def max_facility_amount(annual_sales: int) -> int:
    """Compute the provisional facility ceiling (融資上限) in integer yen.

    The ceiling is ``annual_sales * ORIGINATION_MAX_FACILITY_SALES_MULTIPLE``
    rounded to whole yen — a deterministic exposure cap relative to firm size,
    the origination analogue of ``provision_amount``. Never produced or altered
    by a model.

    Args:
        annual_sales: The applicant's annualised sales (年商) in integer yen.

    Returns:
        The provisional facility ceiling in integer yen.

    Raises:
        ValueError: If ``annual_sales`` is negative.
    """
    if annual_sales < 0:
        raise ValueError("Annual sales cannot be negative.")
    return round(annual_sales * ORIGINATION_MAX_FACILITY_SALES_MULTIPLE)


def recommend_origination(
    tdb_score: int | None,
    annual_sales: int,
    *,
    anti_social_clear: bool = True,
) -> OriginationRecommendation:
    """Build the full deterministic, advisory origination recommendation.

    Combines :func:`proposed_origination_decision` and
    :func:`max_facility_amount` into one frozen, auditable
    :class:`OriginationRecommendation` carrying a bilingual reason. The facility
    ceiling is 0 on a DECLINE (no facility is being recommended). Pure and
    deterministic; ADVISORY ONLY — the implied UNDER_REVIEW → APPROVED /
    DECLINED transition is HITL-gated, so the banker decides.

    Args:
        tdb_score: The applicant's TDB credit score (1-100), or None.
        annual_sales: The applicant's annualised sales (年商) in integer yen.
        anti_social_clear: Whether the 反社会的勢力 screening returned clear.

    Returns:
        A frozen :class:`OriginationRecommendation`.
    """
    decision = proposed_origination_decision(tdb_score, anti_social_clear=anti_social_clear)
    if decision is OriginationDecision.APPROVE:
        ceiling = max_facility_amount(annual_sales) if annual_sales > 0 else 0
        reason = (
            f"TDBスコア {tdb_score} ≥ {ORIGINATION_TDB_APPROVE_FLOOR} "
            "かつ反社チェッククリア (TDB score clears the floor; anti-social check clear) "
            "[tdb_score]"
        )
    else:
        ceiling = 0
        if not anti_social_clear:
            reason = "反社会的勢力チェック該当 (anti-social-forces check not clear)"
        elif tdb_score is None:
            reason = "TDBスコア未取得 (no TDB credit score available)"
        else:
            reason = (
                f"TDBスコア {tdb_score} < {ORIGINATION_TDB_APPROVE_FLOOR} "
                "(below the origination approval floor) [tdb_score]"
            )
    return OriginationRecommendation(
        decision=decision,
        proposed_status=decision.proposed_status,
        max_facility_amount=ceiling,
        reason=reason,
    )


def loan_events_reducer(
    current: list[dict[str, object]], update: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Append-only LangGraph reducer for a loan-event log.

    The loan-lifecycle log is permanent, append-only record data: it is never
    reset between revision rounds and there is no clear sentinel (mirrors the
    reconciliation-outcomes corpus). An empty ``update`` (no loan attached, or
    no transition recorded) makes the append a no-op.

    Args:
        current: The existing accumulated loan-event dicts in state.
        update: A (possibly empty) list of new LoanEvent dicts to append.

    Returns:
        The concatenated loan-event list.
    """
    return current + update
