"""Deterministic restructure self-curing grounding (depth step 5) — ADVISORY ONLY.

The distress mirror of :mod:`app.backend.analysis.debt_capacity`.

A restructure (条件変更 / リスケ) grants a distressed borrower relief — a principal
grace period and/or a lending-rate reduction — to buy time to recover. The danger
this check guards against is **forbearance masquerading as turnaround**: a
restructure that does NOT bring the borrower back under the 正常先 EWS floor within
a prudent horizon is a 貸出条件緩和債権 that defers (and often deepens) the loss
rather than curing it. Before this gate nothing deterministically checked whether
a proposed restructure is actually SELF-CURING against the borrower's own EWS
trajectory.

What this module does
---------------------
Given the borrower's trial-balance history, the facility's outstanding principal,
and the proposed restructure terms (grace months + rate-reduction bps), it:

1. computes the recurring annual ordinary-profit **relief** the restructure
   produces, built entirely from the facility's OWN figures:

   - **grace relief** — the scheduled annual principal repayment
     (``outstanding / DEBT_CAPACITY_AMORTIZATION_YEARS``, the SAME amortization
     horizon the debt-capacity check uses) that a grace period defers, scaled by
     ``RESTRUCTURE_FULL_GRACE_FRACTION``. Zero when no grace is granted.
   - **rate relief** — interest saved by the cut
     (``outstanding * rate_reduction_bps / 10_000``). Zero when no cut.

2. feeds that annual relief through the SAME deterministic recovery projector the
   recovery curve uses (:func:`app.backend.analysis.pnl_recovery.project_recovery`),
   so the borrower's recomputed EWS trajectory under the relief is computed
   identically to the live spine; and

3. classifies the month EWS crosses back under the floor against the prudent
   regulatory horizon (:data:`MIN_RECOVERY_HORIZON_YEARS`):

   - recovers within horizon            -> ``self_curing``
   - recovers, but only beyond horizon  -> ``marginal``
   - never recovers within projection   -> ``non_curing``

Scope / honesty guard
---------------------
ADVISORY ONLY, exactly like ``debt_capacity`` / ``uplift_grounding``: it feeds no
gate, no route, and no figure used downstream — the 条件変更 transition stays
HITL-gated. Pure arithmetic over verified inputs reusing the existing EWS
projector: same inputs -> same result, no LLM, no network, stdlib + models only.
Hand-verifiable (see ``tests/test_restructure_grounding.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.backend.analysis.pnl_recovery import project_recovery
from app.shared.constants import (
    DEBT_CAPACITY_AMORTIZATION_YEARS,
    MIN_RECOVERY_HORIZON_YEARS,
    MONTHS_PER_YEAR,
    RESTRUCTURE_FULL_GRACE_FRACTION,
)
from app.shared.models.accounting import TrialBalance

__all__ = [
    "RestructureRelief",
    "RestructureCuring",
    "compute_restructure_relief",
    "classify_restructure_curing",
    "restructure_curing_reason",
    "assess_restructure",
]

#: The prudent regulatory recovery horizon in months (the guarantor-critic
#: 5-year horizon, expressed in months for the EWS-crossing comparison).
_HORIZON_MONTHS: int = MIN_RECOVERY_HORIZON_YEARS * MONTHS_PER_YEAR


@dataclass(frozen=True)
class RestructureRelief:
    """The recurring annual ordinary-profit relief a restructure produces.

    Every figure is integer yen, derived from the facility's OWN outstanding
    principal and the proposed terms. The two components sum to
    :attr:`annual_relief`.

    Attributes:
        grace_relief: Annual principal repayment deferred by the grace period
            (0 when no grace months are granted).
        rate_relief: Annual interest saved by the rate reduction (0 when the
            rate-reduction bps is 0).
        annual_relief: ``grace_relief + rate_relief`` — the recurring annual
            ordinary-profit uplift the restructure books.
    """

    grace_relief: int
    rate_relief: int
    annual_relief: int


@dataclass(frozen=True)
class RestructureCuring:
    """The self-curing verdict for a proposed restructure.

    Attributes:
        relief: The facility's self-derived annual relief breakdown.
        recovery_month_index: 1-based month the recomputed EWS first crosses
            back under the 正常先 floor under the relief, or ``None`` if it never
            does within the projection.
        horizon_months: The prudent regulatory horizon recovery is judged
            against (:data:`MIN_RECOVERY_HORIZON_YEARS` in months).
        band: 'self_curing' | 'marginal' | 'non_curing'.
        reason: A deterministic bilingual explanation of the band.
    """

    relief: RestructureRelief
    recovery_month_index: int | None
    horizon_months: int
    band: str
    reason: str


def compute_restructure_relief(
    outstanding: int,
    grace_months: int,
    rate_reduction_bps: int,
) -> RestructureRelief:
    """Compute the recurring annual relief a restructure produces.

    Pure deterministic arithmetic over the facility's OWN outstanding principal
    and the proposed terms. The grace leg defers a fraction
    (``RESTRUCTURE_FULL_GRACE_FRACTION``) of the scheduled annual principal
    repayment (amortized over ``DEBT_CAPACITY_AMORTIZATION_YEARS``, the single
    source of truth shared with the debt-capacity check); the rate leg saves the
    interest the cut removes. Both legs are floored at 0 — no grace months means
    no grace relief, a 0-bps cut means no rate relief, and a non-positive
    outstanding produces no relief at all.

    Args:
        outstanding: The facility's outstanding principal in integer yen.
        grace_months: Months of principal grace (元本返済猶予) granted; > 0 enables
            the grace relief leg.
        rate_reduction_bps: Lending-rate reduction in basis points (e.g. 200 =
            2.00%); > 0 enables the rate relief leg.

    Returns:
        A :class:`RestructureRelief`. All-zero when ``outstanding`` <= 0 or
        neither relief lever is pulled.
    """
    if outstanding <= 0:
        return RestructureRelief(0, 0, 0)

    grace_relief = 0
    if grace_months > 0:
        scheduled_annual_principal = outstanding / DEBT_CAPACITY_AMORTIZATION_YEARS
        grace_relief = int(round(scheduled_annual_principal * RESTRUCTURE_FULL_GRACE_FRACTION))

    rate_relief = 0
    if rate_reduction_bps > 0:
        rate_relief = int(round(outstanding * rate_reduction_bps / 10_000))

    grace_relief = max(0, grace_relief)
    rate_relief = max(0, rate_relief)
    return RestructureRelief(
        grace_relief=grace_relief,
        rate_relief=rate_relief,
        annual_relief=grace_relief + rate_relief,
    )


def classify_restructure_curing(recovery_month_index: int | None, horizon_months: int) -> str:
    """Classify a restructure by when (if ever) it returns the borrower to 正常.

    Bands:
        recovers at/under horizon            -> 'self_curing'
        recovers, but only beyond horizon    -> 'marginal'
        never recovers within the projection -> 'non_curing'

    Args:
        recovery_month_index: 1-based month EWS first crosses under the floor
            under the relief, or ``None`` if it never does.
        horizon_months: The prudent regulatory horizon in months.

    Returns:
        The band string.
    """
    if recovery_month_index is None:
        return "non_curing"
    if recovery_month_index <= horizon_months:
        return "self_curing"
    return "marginal"


def restructure_curing_reason(
    band: str, relief: RestructureRelief, recovery_month_index: int | None
) -> str:
    """Return the deterministic bilingual reason for a curing band.

    Names the relief the restructure produces and the month (if any) the
    borrower returns to 正常, mirroring the ``debt_capacity_reason`` style so a
    banker reads WHY the restructure does or doesn't cure. Display/audit prose
    only; it decides nothing.

    Args:
        band: The band from :func:`classify_restructure_curing`.
        relief: The facility's self-derived relief breakdown.
        recovery_month_index: The recovery month, or ``None``.

    Returns:
        A short bilingual reason string.
    """
    legs = f"元本猶予 {relief.grace_relief:,}円 + 金利軽減 {relief.rate_relief:,}円"
    base = f"条件変更による年間改善 {relief.annual_relief:,}円/年（{legs}）"
    if band == "self_curing":
        return f"{base} ・ 自己治癒（self-curing; {recovery_month_index}ヶ月で正常化見込）"
    if band == "marginal":
        return (
            f"{base} ・ 限界的（marginal; {recovery_month_index}ヶ月で正常化だが"
            "健全期限超過、要追加施策）"
        )
    # non_curing
    if relief.annual_relief <= 0:
        return f"{base} ・ 治癒不能（non-curing; 条件変更による改善がゼロ）"
    return f"{base} ・ 治癒不能（non-curing; 投影期間内に正常化せず、実質的な忘集）"


def assess_restructure(
    shisanhyo: list[TrialBalance],
    outstanding: int,
    grace_months: int,
    rate_reduction_bps: int,
    *,
    horizon_months: int = _HORIZON_MONTHS,
) -> RestructureCuring:
    """Assess whether a proposed restructure is self-curing for the borrower.

    The single public entry point. Composes the facility's self-derived annual
    relief, projects the borrower's EWS trajectory under that relief through the
    SAME recovery projector the recovery curve uses, and classifies the recovery
    month against the prudent regulatory horizon. Pure and ADVISORY ONLY — it
    returns an annotation and feeds no gate, route, or figure.

    Args:
        shisanhyo: Actual monthly trial balances, ascending by period.
        outstanding: The facility's outstanding principal in integer yen.
        grace_months: Months of principal grace granted by the restructure.
        rate_reduction_bps: Lending-rate reduction in basis points.
        horizon_months: The prudent recovery horizon in months (defaults to
            ``MIN_RECOVERY_HORIZON_YEARS`` years).

    Returns:
        A :class:`RestructureCuring` verdict. ``non_curing`` with no recovery
        month when the relief is zero or the history is too short to project.
    """
    relief = compute_restructure_relief(
        max(0, int(outstanding)),
        max(0, int(grace_months)),
        max(0, int(rate_reduction_bps)),
    )
    # Honesty guard: a restructure that produces NO relief cannot be credited
    # with a cure. The borrower's unaided EWS baseline may cross the floor on
    # its own (self-recovery), but that recovery is not attributable to a
    # zero-relief restructure -- treating it as self-curing would be forbearance
    # masquerading as turnaround. So short-circuit to no recovery (non_curing)
    # whenever the relief is non-positive, before consulting the projector.
    if relief.annual_relief <= 0:
        band = classify_restructure_curing(None, horizon_months)
        return RestructureCuring(
            relief=relief,
            recovery_month_index=None,
            horizon_months=horizon_months,
            band=band,
            reason=restructure_curing_reason(band, relief, None),
        )
    # Project the borrower's EWS trajectory under the relief over the FULL
    # horizon (do not stop at recovery) so 'marginal' vs 'self_curing' can be
    # distinguished by the recovery month. project_recovery returns an empty
    # projection (recovery_month_index None) for <2 months of history or a
    # non-positive relief that never crosses the floor — both -> non_curing.
    projection = project_recovery(
        shisanhyo,
        relief.annual_relief,
        horizon_months=max(horizon_months, _HORIZON_MONTHS),
        stop_at_recovery=False,
    )
    recovery_month_index = projection.recovery_month_index
    band = classify_restructure_curing(recovery_month_index, horizon_months)
    reason = restructure_curing_reason(band, relief, recovery_month_index)
    return RestructureCuring(
        relief=relief,
        recovery_month_index=recovery_month_index,
        horizon_months=horizon_months,
        band=band,
        reason=reason,
    )
