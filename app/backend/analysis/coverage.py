"""Deterministic collateral / guarantee coverage check (origination breadth) — ADVISORY ONLY.

The breadth twin of :mod:`app.backend.analysis.debt_capacity`, on the OTHER side
of the credit question.

The debt-capacity check asks *"can the firm's P&L SERVICE this facility?"* — the
income-statement lens. This module asks the complementary balance-sheet question:
*"if it cannot, what of the facility is SECURED?"* — the collateral (担保) and
guarantee (保証) coverage standing behind the exposure.

Why both lenses are needed
--------------------------
A facility can be comfortably within debt-service capacity yet almost entirely
unsecured (a clean credit risk the bank carries on the borrower's word), or over
capacity yet fully collateralised (a stretched but recoverable one). At the 稟議
gate the banker needs BOTH signals; until now only the capacity lens existed, so
an over-sized facility was flagged with no view of whether collateral made it
recoverable, and a within-capacity facility hid how much of it was a clean risk.

What this module does
---------------------
Given the pledged collateral value (担保評価額), the guarantee coverage (保証カバー額),
and the proposed facility, it computes the secured fraction of the exposure and
bands it:

- **covered amount** -- ``collateral_value + guarantee_coverage`` (each floored
  at 0; unknown coverage is treated as none, never assumed).
- **coverage ratio** -- ``covered_amount / facility`` (``None`` when the
  facility is non-positive -- division undefined, the parallel of the
  debt-capacity ``ceiling <= 0 -> ratio None`` rule).
- **uncovered amount** -- ``max(0, facility - covered_amount)``, the clean-risk
  tail the bank carries unsecured.

The facility is then classified ``well_covered`` / ``partial`` / ``uncovered``
(thresholds in :mod:`app.shared.constants`), with a deterministic bilingual
reason mirroring the debt-capacity reason style.

Prudent-banker asymmetry
------------------------
Like ``debt_capacity``, this errs toward UNDER-stating safety: unknown coverage
figures default to 0, so a facility with no supplied collateral data bands as
``uncovered`` rather than being assumed secured. Over-stating security is the
dangerous direction.

Scope / honesty guard
---------------------
ADVISORY ONLY, exactly like ``debt_capacity`` and ``uplift_grounding``: it feeds
no gate, no route, and no figure used downstream. It annotates, so a banker sees
an unsecured facility BEFORE trusting the headline recommendation. Pure
arithmetic over verified inputs: same inputs -> same result, no LLM, no network,
stdlib + models only. It can be hand-verified (see ``tests/test_coverage.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.shared.constants import (
    COVERAGE_PARTIAL_FLOOR,
    COVERAGE_WELL_COVERED_FLOOR,
)

__all__ = [
    "CollateralCoverage",
    "covered_amount",
    "classify_coverage",
    "coverage_reason",
    "assess_coverage",
]


@dataclass(frozen=True)
class CollateralCoverage:
    """The collateral / guarantee coverage verdict for a proposed facility.

    Every figure is in integer yen. The two coverage legs sum to
    :attr:`covered_amount`; the secured and unsecured portions of the facility
    sum to :attr:`facility`.

    Attributes:
        facility: The proposed facility ceiling assessed (integer yen).
        collateral_value: The pledged collateral's value (担保評価額), floored at 0.
        guarantee_coverage: The guaranteed portion (保証カバー額), floored at 0.
        covered_amount: ``collateral_value + guarantee_coverage`` (the secured +
            guaranteed value standing behind the exposure).
        uncovered_amount: ``max(0, facility - covered_amount)`` -- the clean-risk
            tail the bank carries unsecured.
        band: 'well_covered' | 'partial' | 'uncovered'.
        ratio: ``covered_amount / facility`` (the coverage multiple); ``None``
            when the facility is non-positive (division undefined).
        reason: A deterministic bilingual explanation of the band.
    """

    facility: int
    collateral_value: int
    guarantee_coverage: int
    covered_amount: int
    uncovered_amount: int
    band: str
    ratio: float | None
    reason: str


def covered_amount(collateral_value: int, guarantee_coverage: int) -> int:
    """Return the total secured + guaranteed value behind a facility, floored at 0.

    The sum of the pledged collateral value (担保評価額) and the guaranteed portion
    (保証カバー額). Each leg is floored at 0 first, so a negative / malformed input
    can never inflate coverage (the prudent direction).

    Args:
        collateral_value: The pledged collateral's value in integer yen.
        guarantee_coverage: The guaranteed portion in integer yen.

    Returns:
        The total covered amount in integer yen (>= 0).
    """
    return max(0, int(collateral_value)) + max(0, int(guarantee_coverage))


def classify_coverage(covered: int, facility: int) -> tuple[str, float | None]:
    """Classify a facility's coverage against the proposed facility amount.

    Bands (thresholds in :mod:`app.shared.constants`):
        ratio >= COVERAGE_WELL_COVERED_FLOOR   -> 'well_covered'
        ratio >= COVERAGE_PARTIAL_FLOOR        -> 'partial'
        ratio <  COVERAGE_PARTIAL_FLOOR        -> 'uncovered'

    A non-positive facility is trivially 'well_covered' (no exposure to cover),
    with ratio ``None`` (division undefined) -- mirroring the debt-capacity
    0-facility rule. This keeps a DECLINE (0 ceiling) from banding as a risk.

    Args:
        covered: The total secured + guaranteed value (integer yen).
        facility: The proposed facility ceiling (integer yen).

    Returns:
        A tuple of (band, ratio) where ratio is covered/facility (or None when
        the facility is non-positive).
    """
    if facility <= 0:
        # No exposure -> trivially covered; ratio undefined (no denominator).
        return "well_covered", None
    ratio = covered / facility
    if ratio >= COVERAGE_WELL_COVERED_FLOOR:
        return "well_covered", ratio
    if ratio >= COVERAGE_PARTIAL_FLOOR:
        return "partial", ratio
    return "uncovered", ratio


def coverage_reason(
    band: str,
    facility: int,
    collateral_value: int,
    guarantee_coverage: int,
    covered: int,
    uncovered: int,
    ratio: float | None,
) -> str:
    """Return the deterministic bilingual reason for a coverage band.

    Names the pledged collateral, the guarantee, the total covered amount, the
    facility, and the coverage ratio -- mirroring the ``debt_capacity_reason``
    style so a banker reads WHY the facility is or isn't secured. For an
    under-secured facility (partial / uncovered) it also names the uncovered
    (clean-risk) tail, so the warning comes WITH the unsecured number. Display /
    audit prose only; it decides nothing.

    Args:
        band: The band from :func:`classify_coverage`.
        facility: The proposed facility ceiling (integer yen).
        collateral_value: The pledged collateral value (integer yen).
        guarantee_coverage: The guaranteed portion (integer yen).
        covered: The total covered amount (integer yen).
        uncovered: The uncovered (clean-risk) tail (integer yen).
        ratio: The coverage ratio, or None when the facility is non-positive.

    Returns:
        A short bilingual reason string.
    """
    if facility <= 0:
        return "融資なし（no facility; カバー不要）"
    legs = f"担保 {collateral_value:,}円 + 保証 {guarantee_coverage:,}円"
    pct = f"{ratio * 100:.0f}%" if ratio is not None else "—"
    base = f"カバー額 {covered:,}円（{legs}）vs 融資額 {facility:,}円（カバー率 {pct}）"
    if band == "well_covered":
        return f"{base} ・ 保全十分（well covered; カバー率 {pct}）"
    if band == "partial":
        return f"{base} ・ 一部保全（partial; 無担保部分 {uncovered:,}円）"
    # uncovered
    return f"{base} ・ 保全不足（uncovered; 無担保部分 {uncovered:,}円、要追加担保・保証）"


def assess_coverage(
    facility: int,
    collateral_value: int,
    guarantee_coverage: int,
) -> CollateralCoverage:
    """Assess the collateral / guarantee coverage of a proposed facility.

    The single public entry point. Composes the covered amount, the uncovered
    tail, classifies the coverage ratio, and attaches a deterministic bilingual
    reason. Pure and ADVISORY ONLY -- it returns an annotation and feeds no gate,
    route, or figure.

    Coverage figures are floored at 0 (unknown coverage is treated as none, the
    prudent-banker base), so a call with no collateral / guarantee data bands as
    'uncovered' for any positive facility rather than guessing.

    Args:
        facility: The proposed facility ceiling (e.g.
            ``OriginationRecommendation.max_facility_amount``) in integer yen.
        collateral_value: The pledged collateral's value (担保評価額) in integer yen.
        guarantee_coverage: The guaranteed portion (保証カバー額) in integer yen.

    Returns:
        A :class:`CollateralCoverage` verdict.
    """
    facility = max(0, int(facility))
    collateral_value = max(0, int(collateral_value))
    guarantee_coverage = max(0, int(guarantee_coverage))
    covered = covered_amount(collateral_value, guarantee_coverage)
    uncovered = max(0, facility - covered)
    band, ratio = classify_coverage(covered, facility)
    reason = coverage_reason(
        band,
        facility,
        collateral_value,
        guarantee_coverage,
        covered,
        uncovered,
        ratio,
    )
    return CollateralCoverage(
        facility=facility,
        collateral_value=collateral_value,
        guarantee_coverage=guarantee_coverage,
        covered_amount=covered,
        uncovered_amount=uncovered,
        band=band,
        ratio=ratio,
        reason=reason,
    )
