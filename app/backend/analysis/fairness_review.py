"""Deterministic bias / fairness review over classification outcomes (Feature 7).

The FSA debtor classification is the most consequential output Saisei produces,
so a regulator must be able to ask: *does the band a borrower lands in vary
systematically by something it should not* — by **industry** or by **region** —
rather than purely by the borrower's own figures? This module answers that with
a pure, deterministic disparity analysis over a corpus of past classification
outcomes.

Design rationale
----------------
The quantity of interest is the **adverse-outcome rate** per group: the fraction
of a group's borrowers placed in a distressed band (要管理先 / 要注意先 special
attention and worse). A group whose adverse rate deviates far from the overall
rate is the signal an examiner should review — not because deviation proves bias
(a region genuinely hit harder by a downturn *should* show a higher rate), but
because an unexplained deviation is exactly what a fairness review must surface
for a human to judge.

The ``min_group_size`` floor stops us flagging a group on a handful of borrowers
(one distressed borrower out of two is 50%, but means nothing). The
``disparity_tolerance`` is the absolute rate gap above which a group is flagged.

Guardrails (identical posture to threshold_calibration.py)
----------------------------------------------------------
* **Advisory only.** Returns a report; it never edits a class, gate, route, or
  figure. The classification has already happened; this reviews it after the
  fact for a human.
* **Pure / deterministic / offline.** No state mutation, no I/O, no clock, no
  LLM. Byte-stable for a given input.
* **Not wired into the graph.** Nothing here is imported by the LangGraph spine.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FairnessAxis",
    "ClassificationOutcome",
    "GroupFairnessStats",
    "FairnessReport",
    "analyse_fairness",
    "fairness_outcomes_from_audit",
    "DEFAULT_DISPARITY_TOLERANCE",
    "DEFAULT_MIN_GROUP_SIZE",
    "DISTRESSED_FSA_CLASSES",
]

#: FSA class string values (FsaClass StrEnum) counted as a distressed / adverse
#: outcome. 要注意先 (yochuisaki) and worse; 正常先 (seijosaki) is not adverse.
#: Plain strings (not an FsaClass import) so this module stays a pure,
#: dependency-light analysis primitive like threshold_calibration.
DISTRESSED_FSA_CLASSES: frozenset[str] = frozenset(
    {"yochuisaki", "hatan_kenensaki", "jisshitsu_hatansaki", "hatansaki"}
)

#: Default absolute adverse-rate gap (vs. the overall rate) above which a group
#: is flagged for human review.
DEFAULT_DISPARITY_TOLERANCE: float = 0.20

#: Default minimum borrowers in a group before its rate is trusted / flaggable.
DEFAULT_MIN_GROUP_SIZE: int = 5


class FairnessAxis(StrEnum):
    """The protected-ish attribute a fairness review groups outcomes by."""

    INDUSTRY = "industry"
    REGION = "region"


class ClassificationOutcome(BaseModel):
    """One borrower's classification outcome, the unit of the fairness corpus.

    Built from a borrower's ``CompanyProfile`` + final ``fsa_classification`` /
    ``special_attention``. The bank supplies these from its assessed book; the
    review never re-classifies — it only reads the outcome.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    industry: str = Field(default="", description="Industry / 業種 (CompanyProfile.industry).")
    region: str = Field(
        default="", description="Prefecture / 都道府県 (CompanyProfile.prefecture)."
    )
    fsa_class: str = Field(
        default="",
        description="FsaClass string value (e.g. 'yochuisaki'). Empty = unclassified.",
    )
    special_attention: bool = Field(
        default=False, description="要管理先 sub-tier flag (counts as distressed)."
    )

    def is_distressed(self) -> bool:
        """Whether this outcome is an adverse (distressed) classification.

        Adverse = the FSA class is 要注意先 or worse, OR the 要管理先 special-
        attention sub-tier flag is set (a special-attention 要注意先 is adverse
        even though the base class string alone would already qualify).
        """
        return self.special_attention or self.fsa_class in DISTRESSED_FSA_CLASSES


def fairness_outcomes_from_audit(
    events: list[Any],
) -> list[ClassificationOutcome]:
    """Build the fairness corpus from audit-ledger ``classification`` events.

    The immutable audit ledger is the source of truth for *what was classified*:
    every `classifier_node` run emits a ``classification`` event whose payload
    carries ``fsa_classification`` / ``special_attention`` and (additively) the
    borrower's ``industry`` / ``prefecture``. This adapter maps those events into
    :class:`ClassificationOutcome` records so :func:`analyse_fairness` can run on
    real history with no new storage.

    Pure and defensive: non-classification events are skipped; each event is read
    via ``event.event_type`` / ``event.payload`` (an :class:`AuditEvent`) or via
    a plain mapping (a rehydrated dict), so it works on either shape. Missing
    payload keys degrade to empty / False rather than raising.

    Args:
        events: Audit events (e.g. from ``AuditSink.read(thread_id)``), as
            :class:`AuditEvent` objects or dicts.

    Returns:
        One :class:`ClassificationOutcome` per ``classification`` event.
    """
    outcomes: list[ClassificationOutcome] = []
    for event in events:
        if isinstance(event, dict):
            event_type = str(event.get("event_type", ""))
            payload = event.get("payload") or {}
        else:
            event_type = str(getattr(getattr(event, "event_type", ""), "value", "") or "")
            payload = getattr(event, "payload", {}) or {}
        if event_type != "classification":
            continue
        outcomes.append(
            ClassificationOutcome(
                industry=str(payload.get("industry", "") or ""),
                region=str(payload.get("prefecture", "") or ""),
                fsa_class=str(payload.get("fsa_classification", "") or ""),
                special_attention=bool(payload.get("special_attention", False)),
            )
        )
    return outcomes


class GroupFairnessStats(BaseModel):
    """Adverse-outcome statistics for one group along a fairness axis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str = Field(description="The group value (an industry or a prefecture).")
    total: int = Field(description="Borrowers in this group.")
    distressed: int = Field(description="Borrowers in this group with an adverse outcome.")
    adverse_rate: float = Field(
        description="distressed / total, rounded to 4 dp (0.0 when total == 0)."
    )
    disparity: float = Field(
        description=(
            "adverse_rate - overall_adverse_rate, rounded to 4 dp "
            "(signed; positive = this group fares worse than the book)."
        )
    )
    flagged: bool = Field(
        description=(
            "True when |disparity| >= disparity_tolerance AND total >= "
            "min_group_size — a deviation large enough, on enough borrowers, to "
            "warrant human review."
        )
    )


class FairnessReport(BaseModel):
    """Advisory bias / fairness report for one axis (industry or region)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    axis: FairnessAxis = Field(description="The attribute outcomes were grouped by.")
    total_outcomes: int = Field(description="All outcomes supplied (incl. unknown-group).")
    classified_outcomes: int = Field(
        description="Outcomes with a non-empty group value (the analysed denominator)."
    )
    overall_adverse_rate: float = Field(
        description="Distressed / classified across all groups, rounded to 4 dp."
    )
    disparity_tolerance: float = Field(description="The absolute rate gap used for flagging.")
    min_group_size: int = Field(description="The minimum group size used for flagging.")
    per_group: list[GroupFairnessStats] = Field(
        description=(
            "Per-group stats, sorted by disparity descending then group ascending "
            "(worst-faring, most-review-worthy groups first; byte-stable ties)."
        )
    )
    flagged_groups: list[str] = Field(
        description="Group values flagged for review (|disparity| >= tolerance, size ok)."
    )
    rationale: str = Field(description="Human-readable summary of the finding.")


def _rate(distressed: int, total: int) -> float:
    """Return distressed / total rounded to 4 dp, or 0.0 when total == 0."""
    if total == 0:
        return 0.0
    return round(distressed / total, 4)


def _group_value(outcome: ClassificationOutcome, axis: FairnessAxis) -> str:
    """Return the group key for an outcome along the given axis (trimmed)."""
    raw = outcome.industry if axis is FairnessAxis.INDUSTRY else outcome.region
    return raw.strip()


def _build_rationale(
    *,
    axis: FairnessAxis,
    classified: int,
    overall_rate: float,
    flagged: list[str],
    tolerance: float,
    min_group_size: int,
) -> str:
    """Compose a precise, non-alarmist summary of the fairness finding."""
    if classified == 0:
        return (
            f"No outcomes carry a {axis.value} value; nothing to review. "
            "Supply classification outcomes with the group attribute set."
        )
    if not flagged:
        return (
            f"No {axis.value} group deviates from the overall adverse rate "
            f"({overall_rate:.1%}) by >= {tolerance:.0%} on >= {min_group_size} "
            "borrowers. No disparity to review on this axis (not a proof of "
            "absence — only that none crossed the review threshold)."
        )
    groups = ", ".join(flagged)
    return (
        f"{len(flagged)} {axis.value} group(s) deviate from the overall adverse "
        f"rate ({overall_rate:.1%}) by >= {tolerance:.0%} on >= {min_group_size} "
        f"borrowers and warrant human review: {groups}. A deviation is NOT proof "
        "of bias — a group genuinely harder hit should show a higher rate — but "
        "each flagged group must be explained or remediated by a human."
    )


def analyse_fairness(
    outcomes: list[ClassificationOutcome] | list[dict[str, Any]],
    axis: FairnessAxis,
    *,
    disparity_tolerance: float = DEFAULT_DISPARITY_TOLERANCE,
    min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
) -> FairnessReport:
    """Compute an advisory bias / fairness report over classification outcomes.

    Groups the outcomes by ``axis`` (industry or region), computes each group's
    adverse-outcome rate (要管理先 / 要注意先-and-worse), and flags any group
    whose rate deviates from the overall rate by ``>= disparity_tolerance`` on
    ``>= min_group_size`` borrowers.

    Args:
        outcomes: The classification corpus — :class:`ClassificationOutcome`
            objects or their dicts (each validated). Outcomes with an empty
            group value for this axis are excluded from the analysed denominator
            but still counted in ``total_outcomes``.
        axis: The attribute to group by (:class:`FairnessAxis`).
        disparity_tolerance: Absolute adverse-rate gap above which a group is
            flagged. Defaults to ``DEFAULT_DISPARITY_TOLERANCE`` (0.20).
        min_group_size: Minimum borrowers in a group before it can be flagged.
            Defaults to ``DEFAULT_MIN_GROUP_SIZE`` (5).

    Returns:
        A :class:`FairnessReport`. Advisory only — it recommends human review of
        flagged groups and never changes any classification.
    """
    parsed = [
        o if isinstance(o, ClassificationOutcome) else ClassificationOutcome.model_validate(o)
        for o in outcomes
    ]

    # Tally per group (only outcomes that carry a group value for this axis).
    totals: dict[str, int] = {}
    distressed: dict[str, int] = {}
    classified = 0
    distressed_overall = 0
    for outcome in parsed:
        group = _group_value(outcome, axis)
        if not group:
            continue
        classified += 1
        totals[group] = totals.get(group, 0) + 1
        if outcome.is_distressed():
            distressed[group] = distressed.get(group, 0) + 1
            distressed_overall += 1

    overall_rate = _rate(distressed_overall, classified)

    per_group: list[GroupFairnessStats] = []
    flagged_groups: list[str] = []
    for group in totals:
        g_total = totals[group]
        g_distressed = distressed.get(group, 0)
        g_rate = _rate(g_distressed, g_total)
        disparity = round(g_rate - overall_rate, 4)
        is_flagged = abs(disparity) >= disparity_tolerance and g_total >= min_group_size
        if is_flagged:
            flagged_groups.append(group)
        per_group.append(
            GroupFairnessStats(
                group=group,
                total=g_total,
                distressed=g_distressed,
                adverse_rate=g_rate,
                disparity=disparity,
                flagged=is_flagged,
            )
        )

    # Worst-faring (highest disparity) first; ties broken by group asc for
    # byte-stable deterministic output.
    per_group.sort(key=lambda s: (-s.disparity, s.group))
    flagged_groups.sort()

    rationale = _build_rationale(
        axis=axis,
        classified=classified,
        overall_rate=overall_rate,
        flagged=flagged_groups,
        tolerance=disparity_tolerance,
        min_group_size=min_group_size,
    )

    return FairnessReport(
        axis=axis,
        total_outcomes=len(parsed),
        classified_outcomes=classified,
        overall_adverse_rate=overall_rate,
        disparity_tolerance=disparity_tolerance,
        min_group_size=min_group_size,
        per_group=per_group,
        flagged_groups=flagged_groups,
        rationale=rationale,
    )
