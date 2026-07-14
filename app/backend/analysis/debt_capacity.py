"""Deterministic debt-service-capacity check (origination breadth) — ADVISORY ONLY.

The origination mirror of :mod:`app.backend.analysis.uplift_grounding`.

:func:`app.shared.models.loan.recommend_origination` proposes a provisional
facility ceiling (融資上限) that is a flat ``annual_sales * 0.5`` multiple. That
ceiling is anchored to the firm's *size* (年商) but is **blind to its
debt-servicing capacity**: a firm with ¥200M sales and razor-thin or negative
ordinary profit gets the same ceiling as a healthy ¥200M-sales firm. That is the
origination twin of the naive uplift number the distress side carried before
``uplift_grounding`` -- a figure anchored to the wrong denominator.

What this module does
---------------------
The firm's own P&L already tells us what debt it can carry: ordinary profit
(経常利益) is the cash available to service new debt. Given the firm's trial
balance history and a proposed facility, this computes a deterministic
*prudent debt-service ceiling* built entirely from the firm's OWN figures and
compares the facility's implied annual debt service to it:

- **implied annual debt service** -- a conservative principal-amortization leg
  (facility / ``DEBT_CAPACITY_AMORTIZATION_YEARS``) plus an interest leg at the
  existing ``WORKING_CAPITAL_FINANCING_RATE`` (reused here; single source of
  truth, exactly as ``uplift_grounding`` reuses it).
- **prudent service ceiling** -- ``DEBT_CAPACITY_DSCR_FRACTION`` of the firm's
  DEMONSTRATED annual ordinary profit, a DSCR-style cushion that keeps headroom
  for existing obligations and earnings volatility.

The facility is then classified ``within_capacity`` / ``stretch`` /
``over_capacity`` (thresholds in :mod:`app.shared.constants`), with a
deterministic bilingual reason mirroring the distress-side credibility band.

Prudent-banker asymmetry
------------------------
Where ``uplift_grounding`` anchors to the firm's BEST historical margin (you
cannot claim recovery of a margin the firm never lost), this check anchors to a
CONSERVATIVE ordinary-profit base -- ``min(trailing-average, latest)`` floored
at 0 -- because over-stating serviceable capacity is the dangerous direction. A
firm with non-positive demonstrated ordinary profit can service no new debt, so
any positive facility is ``over_capacity`` and the ratio is ``None`` (division
undefined) -- the exact parallel of the uplift ``ceiling <= 0 -> implausible``
rule.

Scope / honesty guard
---------------------
ADVISORY ONLY, exactly like ``uplift_grounding`` and the feasibility critic: it
feeds no gate, no route, and no figure used downstream. It annotates, so a
banker sees an over-sized facility BEFORE trusting the sales-multiple ceiling.
Pure arithmetic over verified inputs: same inputs -> same result, no LLM, no
network, stdlib + models only. It can be hand-verified (see
``tests/test_debt_capacity.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.shared.constants import (
    DEBT_CAPACITY_AMORTIZATION_YEARS,
    DEBT_CAPACITY_DSCR_FRACTION,
    DEBT_CAPACITY_STRETCH_FACTOR,
    MONTHS_PER_YEAR,
    WORKING_CAPITAL_FINANCING_RATE,
)
from app.shared.models.accounting import TrialBalance

__all__ = [
    "DebtServiceProfile",
    "DebtCapacity",
    "demonstrated_ordinary_profit",
    "implied_annual_debt_service",
    "capacity_bounded_ceiling",
    "classify_debt_capacity",
    "debt_capacity_reason",
    "assess_debt_capacity",
]


# ---------------------------------------------------------------------------
# Debt-service profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DebtServiceProfile:
    """The firm's self-derived debt-service profile for a proposed facility.

    Every figure is in integer yen and is derived from the proposed facility and
    the firm's OWN ordinary profit. The two service legs sum to
    :attr:`annual_debt_service`.

    Attributes:
        annual_ordinary_profit: The firm's demonstrated annual ordinary profit
            (経常利益) base -- ``min(trailing-average, latest) * 12`` floored at 0
            (the conservative, prudent-banker base).
        prudent_service_ceiling: The prudent annual debt-service the firm can
            carry -- ``annual_ordinary_profit * DEBT_CAPACITY_DSCR_FRACTION``.
        principal_leg: Annual principal repayment implied by amortizing the
            facility over ``DEBT_CAPACITY_AMORTIZATION_YEARS``.
        interest_leg: Annual interest implied at ``WORKING_CAPITAL_FINANCING_RATE``.
        annual_debt_service: The facility's total implied annual debt service
            (``principal_leg + interest_leg``).
    """

    annual_ordinary_profit: int
    prudent_service_ceiling: int
    principal_leg: int
    interest_leg: int
    annual_debt_service: int


@dataclass(frozen=True)
class DebtCapacity:
    """The capacity verdict for a proposed facility against the firm's figures.

    Attributes:
        facility: The proposed facility ceiling assessed (integer yen).
        profile: The firm's self-derived debt-service profile.
        capacity_bounded_ceiling: The LARGEST facility whose implied annual debt
            service stays within the firm's prudent service ceiling -- the
            advisory "what the P&L could carry" number to set beside the
            size-anchored ceiling. 0 when the firm has no demonstrated capacity.
        band: 'within_capacity' | 'stretch' | 'over_capacity'.
        ratio: annual_debt_service / prudent_service_ceiling (the over-sizing
            multiple); ``None`` when the ceiling is 0 (no demonstrated capacity).
        reason: A deterministic bilingual explanation of the band.
    """

    facility: int
    profile: DebtServiceProfile
    capacity_bounded_ceiling: int
    band: str
    ratio: float | None
    reason: str


def demonstrated_ordinary_profit(shisanhyo: list[TrialBalance]) -> int:
    """Return the firm's demonstrated annual ordinary profit (経常利益), floored at 0.

    The prudent-banker base: the lesser of the trailing monthly average and the
    latest month's ordinary profit, annualised over :data:`MONTHS_PER_YEAR`, then
    floored at 0. Taking the MIN of average and latest is the conservative choice
    -- a single strong month cannot inflate serviceable capacity, and a
    deteriorating latest month is respected. This deliberately inverts the
    BEST-month asymmetry of ``uplift_grounding`` because over-stating capacity is
    the dangerous direction here.

    A firm whose demonstrated ordinary profit is non-positive can service no new
    debt, so the base is 0 (and any positive facility will be ``over_capacity``).

    Args:
        shisanhyo: Actual monthly trial balances, ascending by period. The full
            series supplies the trailing average; the last entry is the latest.

    Returns:
        The demonstrated annual ordinary profit in integer yen, floored at 0
        (0 when there is no history).
    """
    if not shisanhyo:
        return 0
    monthly = [tb.keijo_rieki for tb in shisanhyo]
    trailing_avg = sum(monthly) / len(monthly)
    latest = monthly[-1]
    base_monthly = min(trailing_avg, latest)
    annual = int(round(base_monthly * MONTHS_PER_YEAR))
    return max(0, annual)


def implied_annual_debt_service(facility: int) -> tuple[int, int]:
    """Return the (principal_leg, interest_leg) of a facility's annual debt service.

    Pure deterministic arithmetic. The principal leg amortizes the facility over
    :data:`DEBT_CAPACITY_AMORTIZATION_YEARS`; the interest leg applies
    :data:`WORKING_CAPITAL_FINANCING_RATE` (reused as the single source of truth
    for an assumed financing rate, exactly as ``uplift_grounding`` reuses it).
    Both legs are floored at 0; a non-positive facility implies no debt service.

    Args:
        facility: The proposed facility ceiling in integer yen.

    Returns:
        A tuple of (principal_leg, interest_leg) in integer yen.
    """
    if facility <= 0:
        return 0, 0
    principal_leg = int(round(facility / DEBT_CAPACITY_AMORTIZATION_YEARS))
    interest_leg = int(round(facility * WORKING_CAPITAL_FINANCING_RATE))
    return principal_leg, interest_leg


def capacity_bounded_ceiling(prudent_service_ceiling: int) -> int:
    """Return the largest facility whose debt service stays within capacity.

    The deterministic INVERSE of :func:`implied_annual_debt_service`: the
    debt-service legs are linear in the facility,

        service(F) = F / DEBT_CAPACITY_AMORTIZATION_YEARS + F * WORKING_CAPITAL_FINANCING_RATE
                   = F * (1 / DEBT_CAPACITY_AMORTIZATION_YEARS + WORKING_CAPITAL_FINANCING_RATE)

    so the largest facility whose service does not exceed the prudent ceiling is

        F_max = prudent_service_ceiling / (1 / AMORT_YEARS + WC_RATE)

    This is the advisory "what the firm's P&L could prudently carry" number to
    set beside the size-anchored ceiling, so an over-sized facility is flagged
    WITH a prudent alternative rather than just a warning. Floored (rounded DOWN)
    so it never proposes a facility whose rounded service would tip over
    capacity, and floored at 0 when there is no demonstrated capacity.

    Args:
        prudent_service_ceiling: The firm's prudent annual debt-service ceiling
            (``annual_ordinary_profit * DEBT_CAPACITY_DSCR_FRACTION``).

    Returns:
        The capacity-bounded facility ceiling in integer yen (0 when the prudent
        ceiling is non-positive).
    """
    if prudent_service_ceiling <= 0:
        return 0
    service_rate = 1.0 / DEBT_CAPACITY_AMORTIZATION_YEARS + WORKING_CAPITAL_FINANCING_RATE
    # Round DOWN: never suggest a facility whose service could exceed capacity.
    import math

    return max(0, math.floor(prudent_service_ceiling / service_rate))


def classify_debt_capacity(
    annual_debt_service: int, prudent_service_ceiling: int
) -> tuple[str, float | None]:
    """Classify a facility's annual debt service against the prudent ceiling.

    Bands (thresholds in :mod:`app.shared.constants`):
        service <= ceiling                               -> 'within_capacity'
        ceiling < service <= ceiling * STRETCH           -> 'stretch'
        service > ceiling * STRETCH                      -> 'over_capacity'

    A non-positive debt service is always 'within_capacity' (a facility that
    implies no service cannot exceed capacity). When the ceiling is 0 but a
    positive service is implied, the firm's figures support NO new debt at all,
    so any positive facility is 'over_capacity' and the ratio is None (division
    undefined) -- the parallel of the uplift 'ceiling <= 0 -> implausible' rule.

    Args:
        annual_debt_service: The facility's implied annual debt service.
        prudent_service_ceiling: The firm's prudent annual debt-service ceiling.

    Returns:
        A tuple of (band, ratio) where ratio is service/ceiling (or None when
        the ceiling is 0).
    """
    if annual_debt_service <= 0:
        return "within_capacity", 0.0
    if prudent_service_ceiling <= 0:
        # No demonstrated capacity at all, but a positive facility is proposed.
        return "over_capacity", None
    ratio = annual_debt_service / prudent_service_ceiling
    if annual_debt_service <= prudent_service_ceiling:
        return "within_capacity", ratio
    if annual_debt_service <= prudent_service_ceiling * DEBT_CAPACITY_STRETCH_FACTOR:
        return "stretch", ratio
    return "over_capacity", ratio


def debt_capacity_reason(
    band: str,
    profile: DebtServiceProfile,
    ratio: float | None,
    capacity_ceiling: int = 0,
) -> str:
    """Return the deterministic bilingual reason for a capacity band.

    Names the facility's implied debt service, the firm's prudent ceiling, and
    by what multiple the service exceeds (or sits within) it -- mirroring the
    ``uplift_credibility_reason`` style so a banker reads WHY the facility is or
    isn't within capacity. For an over-sized facility (stretch / over_capacity)
    it also names the capacity-bounded ceiling -- the prudent "what the P&L could
    carry" alternative -- so the warning comes WITH a number. Display/audit prose
    only; it decides nothing.

    Args:
        band: The band from :func:`classify_debt_capacity`.
        profile: The firm's self-derived debt-service profile.
        ratio: The over-sizing multiple, or None when the ceiling is 0.
        capacity_ceiling: The capacity-bounded facility ceiling (named in the
            over-sized bands).

    Returns:
        A short bilingual reason string.
    """
    legs = f"元本 {profile.principal_leg:,}円 + 金利 {profile.interest_leg:,}円"
    base = (
        f"想定年間返済額 {profile.annual_debt_service:,}円/年（{legs}）vs "
        f"健全返済余力 {profile.prudent_service_ceiling:,}円/年"
        f"（経常利益 {profile.annual_ordinary_profit:,}円/年の"
        f"{DEBT_CAPACITY_DSCR_FRACTION:.0%}）"
    )
    if band == "within_capacity":
        if profile.annual_debt_service <= 0:
            return f"{base} ・ 余力内（within capacity; 返済負担なし）"
        pct = f"{ratio * 100:.0f}%" if ratio is not None else "—"
        return f"{base} ・ 余力内（within capacity; 返済余力の {pct} 内）"
    if band == "stretch":
        mult = f"{ratio:.1f}倍" if ratio is not None else "—"
        return (
            f"{base} ・ 余力上限（stretch; 返済余力の {mult}、要追加担保・保証）"
            f"・ 余力相当融資 {capacity_ceiling:,}円"
        )
    # over_capacity
    if ratio is None:
        return f"{base} ・ 余力超過（over capacity; 経常利益から見た返済余力がゼロ）"
    return (
        f"{base} ・ 余力超過（over capacity; 返済余力の {ratio:.1f}倍、過大融資）"
        f"・ 余力相当融資 {capacity_ceiling:,}円"
    )


def assess_debt_capacity(shisanhyo: list[TrialBalance], facility: int) -> DebtCapacity:
    """Assess whether a proposed facility is within the firm's debt-service capacity.

    The single public entry point. Composes the firm's demonstrated ordinary
    profit (the prudent-banker base), the prudent service ceiling, the facility's
    implied annual debt service, classifies it, and attaches a deterministic
    bilingual reason. Pure and ADVISORY ONLY -- it returns an annotation and
    feeds no gate, route, or figure.

    Args:
        shisanhyo: Actual monthly trial balances, ascending by period.
        facility: The proposed facility ceiling (e.g.
            ``OriginationRecommendation.max_facility_amount``) in integer yen.

    Returns:
        A :class:`DebtCapacity` verdict.
    """
    facility = max(0, int(facility))
    annual_ordinary_profit = demonstrated_ordinary_profit(shisanhyo)
    prudent_service_ceiling = int(round(annual_ordinary_profit * DEBT_CAPACITY_DSCR_FRACTION))
    principal_leg, interest_leg = implied_annual_debt_service(facility)
    annual_debt_service = principal_leg + interest_leg
    profile = DebtServiceProfile(
        annual_ordinary_profit=annual_ordinary_profit,
        prudent_service_ceiling=prudent_service_ceiling,
        principal_leg=principal_leg,
        interest_leg=interest_leg,
        annual_debt_service=annual_debt_service,
    )
    bounded_ceiling = capacity_bounded_ceiling(prudent_service_ceiling)
    band, ratio = classify_debt_capacity(annual_debt_service, prudent_service_ceiling)
    reason = debt_capacity_reason(band, profile, ratio, bounded_ceiling)
    return DebtCapacity(
        facility=facility,
        profile=profile,
        capacity_bounded_ceiling=bounded_ceiling,
        band=band,
        ratio=ratio,
        reason=reason,
    )
