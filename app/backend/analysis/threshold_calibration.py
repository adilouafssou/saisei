"""Evidence-based calibration of ``RECONCILIATION_BAND_DISTANCE``.

This is the payoff of the who-was-right corpus captured by the HITL
orchestrator (``SaiseiState.reconciliation_outcomes``). It turns that corpus
into an advisory recommendation for the ``RECONCILIATION_BAND_DISTANCE``
threshold, the magic number flagged CALIBRATION PLACEHOLDER (see #1) in
``app/shared/constants.py``.

Design rationale
----------------
Precision is the right quantity to fit: the cost of too low a threshold is
alert fatigue (the trigger fires on disagreements that were not genuine), the
cost of too high a threshold is missed genuine disagreements. Recommending the
smallest (most sensitive) band distance that still clears a precision floor
maximises sensitivity without chasing noise. The min-samples floor stops us
trusting a precision computed on a handful of outcomes.

Labelling contract (matches ``ReconciliationOutcome.banker_verdict``)
---------------------------------------------------------------------
* ``'floor'`` / ``'llm'`` — USEFUL: the banker judged one side correct, so a
  real disagreement was surfaced. Counts toward precision numerator.
* ``'neither'``           — NOISE: surfaced, but neither side was right.
  Counts toward the labelled denominator but not the numerator.
* ``''`` (empty)          — UNLABELLED: not adjudicated. Excluded from precision
  entirely, but still counted for coverage honesty.

Guardrails
----------
* Advisory only: returns a report; it never edits the constant, gate, or route.
* Pure: no state mutation, no I/O, no clock, no LLM. Deterministic and
  byte-stable for a given input.
* Not wired into the graph: nothing here is imported by the LangGraph spine.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BandDistanceStats",
    "CalibrationReport",
    "calibrate_reconciliation_threshold",
    "report_to_display_rows",
    "USEFUL_VERDICTS",
    "VALID_BAND_DISTANCES",
    "DEFAULT_TARGET_PRECISION",
    "DEFAULT_MIN_SAMPLES",
]

#: Verdicts that count as a genuinely-useful surfaced disagreement.
USEFUL_VERDICTS: frozenset[str] = frozenset({"floor", "llm"})

#: The only band distances a reconciliation trigger can validly use.
#: A distance of 0 is agreement (never triggers); the scale tops out at 2
#: (full-scale 'high' vs 'low'). The threshold is therefore in [1, 2].
VALID_BAND_DISTANCES: tuple[int, ...] = (1, 2)

#: Default precision a band distance must clear to be recommended.
DEFAULT_TARGET_PRECISION: float = 0.70

#: Default minimum labelled outcomes required to trust a precision estimate.
DEFAULT_MIN_SAMPLES: int = 10


class BandDistanceStats(BaseModel):
    """Per-band-distance precision statistics over the outcome corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    band_distance: int = Field(description="The band distance these stats describe (1 or 2).")
    total: int = Field(description="All outcomes recorded at this band distance.")
    labelled: int = Field(
        description="Outcomes with a floor/llm/neither verdict (excludes unlabelled '')."
    )
    useful: int = Field(description="Outcomes whose verdict was 'floor' or 'llm'.")
    unlabelled: int = Field(description="Outcomes with an empty (not-adjudicated) verdict.")
    precision: float | None = Field(
        description=(
            "useful / labelled, rounded to 4 dp. None when labelled == 0 "
            "(precision is undefined with no adjudicated outcomes)."
        )
    )
    meets_target: bool = Field(
        description="Whether precision >= target AND labelled >= min_samples."
    )


class CalibrationReport(BaseModel):
    """Advisory calibration report for ``RECONCILIATION_BAND_DISTANCE``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_outcomes: int = Field(
        description=(
            "Every outcome supplied, including any skipped for a malformed / "
            "out-of-range band_distance (= sum of per-distance totals + skipped)."
        )
    )
    skipped_outcomes: int = Field(
        description=(
            "Outcomes excluded from all per-distance stats because their "
            "band_distance was not a valid int in VALID_BAND_DISTANCES."
        )
    )
    target_precision: float = Field(description="The precision floor used for this report.")
    min_samples: int = Field(description="The minimum labelled-sample floor used.")
    per_distance: list[BandDistanceStats] = Field(
        description="Per-band-distance stats, ascending by band_distance (1 then 2)."
    )
    recommended_band_distance: int | None = Field(
        description=(
            "Smallest band distance meeting both floors; None when none qualifies "
            "(keep the current constant)."
        )
    )
    rationale: str = Field(description="Human-readable explanation of the recommendation.")


def _coerce_band_distance(raw: Any) -> int | None:
    """Return a valid band distance, or None if it is malformed/out-of-range.

    ``bool`` is explicitly rejected even though ``isinstance(True, int)`` is
    True in Python: a boolean band_distance is malformed data, not a 0/1
    distance. Strings and floats are also rejected (the corpus stores ints).
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw not in VALID_BAND_DISTANCES:
        return None
    return raw


def _precision(useful: int, labelled: int) -> float | None:
    """Return useful / labelled rounded to 4 dp, or None when labelled == 0."""
    if labelled == 0:
        return None
    return round(useful / labelled, 4)


def _build_rationale(
    *,
    recommended: int | None,
    per_distance: list[BandDistanceStats],
    skipped: int,
    total: int,
    target_precision: float,
    min_samples: int,
) -> str:
    """Compose a precise, non-misleading explanation of the recommendation."""
    if total == 0:
        return "No outcomes in the corpus; keep the current RECONCILIATION_BAND_DISTANCE."

    if recommended is not None:
        stats = next(s for s in per_distance if s.band_distance == recommended)
        return (
            f"Recommend band distance {recommended}: precision "
            f"{stats.precision} over {stats.labelled} labelled outcomes "
            f"clears the {target_precision} target with >= {min_samples} samples. "
            f"This is the smallest (most sensitive) distance that qualifies."
        )

    # No recommendation — explain the real cause precisely rather than asserting
    # "all unlabelled" when the true cause may be out-of-range distances or an
    # unmet precision/sample floor.
    labelled_total = sum(s.labelled for s in per_distance)
    reasons: list[str] = []
    if skipped:
        reasons.append(
            f"{skipped} of {total} outcome(s) had an out-of-range or malformed "
            f"band_distance and were excluded"
        )
    if labelled_total == 0:
        in_range = total - skipped
        if in_range > 0:
            reasons.append(
                f"all {in_range} in-range outcome(s) are unlabelled (no banker verdict yet)"
            )
    else:
        reasons.append(
            f"no band distance cleared the {target_precision} precision target "
            f"with at least {min_samples} labelled outcomes"
        )
    detail = "; ".join(reasons) if reasons else "insufficient evidence"
    return f"No recommendation: {detail}. Keep the current RECONCILIATION_BAND_DISTANCE."


def calibrate_reconciliation_threshold(
    outcomes: list[dict[str, Any]],
    *,
    target_precision: float = DEFAULT_TARGET_PRECISION,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> CalibrationReport:
    """Compute an advisory calibration report for the reconciliation threshold.

    Args:
        outcomes: The who-was-right corpus — a list of
            ``ReconciliationOutcome`` dicts (as stored in
            ``SaiseiState.reconciliation_outcomes``). Robust to malformed
            entries: a missing/empty verdict counts as unlabelled, and an
            out-of-range or non-int ``band_distance`` is skipped (and reported).
        target_precision: Minimum precision a band distance must reach to be
            recommended. Defaults to ``DEFAULT_TARGET_PRECISION`` (0.70).
        min_samples: Minimum labelled outcomes a band distance needs before its
            precision is trusted. Defaults to ``DEFAULT_MIN_SAMPLES`` (10).

    Returns:
        A :class:`CalibrationReport`. ``recommended_band_distance`` is the
        smallest band distance clearing both floors, or ``None`` when nothing
        qualifies (keep the current constant).
    """
    # Tally per valid band distance.
    totals: dict[int, int] = dict.fromkeys(VALID_BAND_DISTANCES, 0)
    labelled: dict[int, int] = dict.fromkeys(VALID_BAND_DISTANCES, 0)
    useful: dict[int, int] = dict.fromkeys(VALID_BAND_DISTANCES, 0)
    unlabelled: dict[int, int] = dict.fromkeys(VALID_BAND_DISTANCES, 0)
    skipped = 0

    for outcome in outcomes:
        distance = _coerce_band_distance(outcome.get("band_distance"))
        if distance is None:
            skipped += 1
            continue
        totals[distance] += 1
        verdict = str(outcome.get("banker_verdict", "") or "").strip().lower()
        if verdict == "":
            unlabelled[distance] += 1
            continue
        labelled[distance] += 1
        if verdict in USEFUL_VERDICTS:
            useful[distance] += 1

    per_distance: list[BandDistanceStats] = []
    for distance in VALID_BAND_DISTANCES:
        precision = _precision(useful[distance], labelled[distance])
        meets_target = (
            precision is not None
            and precision >= target_precision
            and labelled[distance] >= min_samples
        )
        per_distance.append(
            BandDistanceStats(
                band_distance=distance,
                total=totals[distance],
                labelled=labelled[distance],
                useful=useful[distance],
                unlabelled=unlabelled[distance],
                precision=precision,
                meets_target=meets_target,
            )
        )

    # Smallest (most sensitive) qualifying distance wins.
    recommended = next((s.band_distance for s in per_distance if s.meets_target), None)

    total_outcomes = sum(totals.values()) + skipped
    rationale = _build_rationale(
        recommended=recommended,
        per_distance=per_distance,
        skipped=skipped,
        total=total_outcomes,
        target_precision=target_precision,
        min_samples=min_samples,
    )

    return CalibrationReport(
        total_outcomes=total_outcomes,
        skipped_outcomes=skipped,
        target_precision=target_precision,
        min_samples=min_samples,
        per_distance=per_distance,
        recommended_band_distance=recommended,
        rationale=rationale,
    )


def report_to_display_rows(report: CalibrationReport) -> list[dict[str, str]]:
    """Map a report's per-distance stats to all-string display rows for the UI.

    Pure presentation helper so the frontend's data path is unit-testable
    without instantiating Reflex. Every value is a string; precision renders as
    a percentage or an em-dash when undefined (no labelled outcomes). The
    ``recommended`` flag marks the row the report recommends.

    Args:
        report: The calibration report to render.

    Returns:
        One dict per band distance, ascending, with string-only values:
        ``band_distance``, ``total``, ``labelled``, ``useful``, ``precision``,
        ``meets_target``, ``recommended``.
    """
    rows: list[dict[str, str]] = []
    for stats in report.per_distance:
        precision = "—" if stats.precision is None else f"{stats.precision * 100:.1f}%"
        rows.append(
            {
                "band_distance": str(stats.band_distance),
                "total": str(stats.total),
                "labelled": str(stats.labelled),
                "useful": str(stats.useful),
                "precision": precision,
                "meets_target": "yes" if stats.meets_target else "no",
                "recommended": (
                    "yes" if stats.band_distance == report.recommended_band_distance else "no"
                ),
            }
        )
    return rows
