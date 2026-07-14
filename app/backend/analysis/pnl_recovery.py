"""Feature 5 — deterministic P&L recovery bridge (損益計画).

Projects the approved turnaround strategy month-by-month and reports when the
borrower's recomputed EWS score crosses back under ``EWS_SUBSTANDARD`` (40 — the
正常先 floor). This is the "recovery curve" a banker needs to see: not just a
single annual uplift number, but the month it actually lands the firm back in
normal territory.

Why this is safe to ship without CI
-----------------------------------
Every figure here is **pure deterministic arithmetic** over existing verified
inputs:

- the latest trial balance (``shisanhyo[-1]``) — the deterministic base,
- ``approved_strategy.expected_keijo_uplift`` — the annual uplift the strategist
  already computed,
- ``compute_ews_score`` — the SAME EWS function the live spine uses (reused, not
  reimplemented), so the projected EWS is computed identically to the real one.

The phased uplift is modelled by raising each projected month's non-operating
income (``eigai_shueki``), which lifts ``keijo_rieki`` (経常利益) WITHOUT
fabricating sales or COGS — so the base operating figures stay honest and the
only thing that changes month to month is the booked recovery benefit.

The function is pure: same inputs → same curve, no network, no LLM. It can be
hand-verified month-by-month (see ``tests/test_pnl_recovery.py``).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.backend.nodes.ews_scoring import compute_ews_score
from app.shared.constants import EWS_SUBSTANDARD, MONTHS_PER_YEAR
from app.shared.models.accounting import TrialBalance

__all__ = [
    "RecoveryMonth",
    "RecoveryProjection",
    "project_recovery",
]

#: Default ramp: months over which the monthly uplift phases in linearly.
_DEFAULT_RAMP_MONTHS = 6
#: Default projection horizon (months) when not running until recovery.
_DEFAULT_HORIZON_MONTHS = 36


def _add_months(period: dt.date, months: int) -> dt.date:
    """Return ``period`` advanced by ``months`` calendar months (clamped day).

    Pure date arithmetic with no external dependency. The day is clamped to 28
    so month-end base periods never overflow a short month; this only affects
    the displayed projected period label, never a figure.
    """
    zero_based = (period.month - 1) + months
    year = period.year + zero_based // 12
    month = zero_based % 12 + 1
    return dt.date(year, month, min(period.day, 28))


@dataclass(frozen=True)
class RecoveryMonth:
    """One projected month on the recovery curve.

    Attributes:
        month_index: 1-based month offset from the latest actual period.
        period: The projected month-end date (label only).
        monthly_uplift: Phased ordinary-profit uplift booked this month (JPY).
        keijo_rieki: Projected ordinary profit for this month (JPY).
        ews_score: EWS recomputed over the trailing 12-month window.
        recovered: True once ``ews_score`` is below ``EWS_SUBSTANDARD`` (40).
    """

    month_index: int
    period: dt.date
    monthly_uplift: int
    keijo_rieki: int
    ews_score: float
    recovered: bool


@dataclass(frozen=True)
class RecoveryProjection:
    """The full month-by-month recovery projection.

    Attributes:
        months: Ordered projected months (length == horizon actually run).
        annual_uplift: The approved strategy's annual ordinary-profit uplift.
        full_monthly_uplift: ``annual_uplift`` divided across 12 months.
        ramp_months: Months over which the monthly uplift phased in.
        recovery_month_index: 1-based index of the first month EWS < 40, or
            ``None`` if recovery is not reached within the horizon.
        baseline_ews: EWS of the actual history before any uplift (month 0).
    """

    months: list[RecoveryMonth] = field(default_factory=list)
    annual_uplift: int = 0
    full_monthly_uplift: int = 0
    ramp_months: int = _DEFAULT_RAMP_MONTHS
    recovery_month_index: int | None = None
    baseline_ews: float = 0.0

    @property
    def recovered(self) -> bool:
        """Whether recovery (EWS < 40) is reached within the projected horizon."""
        return self.recovery_month_index is not None


def _phased_monthly_uplift(month_index: int, full_monthly_uplift: int, ramp_months: int) -> int:
    """Return the uplift booked in ``month_index`` (1-based) under a linear ramp.

    Month ``k`` books ``min(k, ramp) / ramp`` of the full monthly uplift, so the
    benefit phases in linearly over the ramp window and is then steady-state.
    Integer yen with explicit rounding (no float leaks into a figure).
    """
    if ramp_months <= 1:
        return full_monthly_uplift
    fraction_num = min(month_index, ramp_months)
    return int(round(full_monthly_uplift * fraction_num / ramp_months))


def _project_tb(base: TrialBalance, period: dt.date, cumulative_uplift: int) -> TrialBalance:
    """Return a projected trial balance: base operating figures + booked uplift.

    The uplift is booked as additional non-operating income (``eigai_shueki``)
    on top of the base month's own non-operating income, so ``keijo_rieki``
    rises by exactly ``cumulative_uplift`` while sales / COGS / SG&A stay equal
    to the honest base figures. ``cumulative_uplift`` is the running monthly
    benefit (not annual), matching a single month's P&L.
    """
    return TrialBalance(
        period=period,
        uriage=int(base.uriage),
        uriage_genka=int(base.uriage_genka),
        hanbaihi=int(base.hanbaihi),
        eigai_shueki=int(base.eigai_shueki) + cumulative_uplift,
        eigai_hiyo=int(base.eigai_hiyo),
    )


def project_recovery(
    shisanhyo: list[TrialBalance],
    annual_uplift: int,
    *,
    ramp_months: int = _DEFAULT_RAMP_MONTHS,
    horizon_months: int = _DEFAULT_HORIZON_MONTHS,
    stop_at_recovery: bool = True,
) -> RecoveryProjection:
    """Project the recovery curve month-by-month from the approved uplift.

    Phases ``annual_uplift / 12`` in over ``ramp_months`` and, for each future
    month, recomputes EWS over the trailing 12-month window (actual history
    rolled forward with the projected months appended). Reports the first month
    whose EWS falls below ``EWS_SUBSTANDARD`` (40).

    Args:
        shisanhyo: Actual monthly trial balances, ascending by period. The last
            entry is the recovery base.
        annual_uplift: Approved strategy's annual ordinary-profit uplift (JPY).
        ramp_months: Months over which the monthly uplift phases in linearly.
        horizon_months: Maximum months to project.
        stop_at_recovery: When True, stop the month after recovery is reached;
            when False, always project the full ``horizon_months``.

    Returns:
        A :class:`RecoveryProjection`. Empty (no months) when there is
        insufficient history (< 2 actual months) to compute EWS.
    """
    if len(shisanhyo) < 2:
        return RecoveryProjection(annual_uplift=int(annual_uplift))

    base = shisanhyo[-1]
    full_monthly = int(round(int(annual_uplift) / MONTHS_PER_YEAR))
    baseline_ews = compute_ews_score(list(shisanhyo))

    # Rolling window seeded with the actual history; each projected month is
    # appended and the trailing 12 months are scored, so EWS is computed exactly
    # as the live spine would for that rolled-forward history.
    rolling: list[TrialBalance] = list(shisanhyo)

    months: list[RecoveryMonth] = []
    recovery_index: int | None = None

    for k in range(1, int(horizon_months) + 1):
        monthly_uplift = _phased_monthly_uplift(k, full_monthly, ramp_months)
        period = _add_months(base.period, k)
        projected = _project_tb(base, period, monthly_uplift)
        rolling.append(projected)

        window = rolling[-MONTHS_PER_YEAR:] if len(rolling) >= MONTHS_PER_YEAR else rolling
        ews = compute_ews_score(window)
        recovered = ews < EWS_SUBSTANDARD

        months.append(
            RecoveryMonth(
                month_index=k,
                period=period,
                monthly_uplift=monthly_uplift,
                keijo_rieki=projected.keijo_rieki,
                ews_score=ews,
                recovered=recovered,
            )
        )

        if recovered and recovery_index is None:
            recovery_index = k
            if stop_at_recovery:
                break

    return RecoveryProjection(
        months=months,
        annual_uplift=int(annual_uplift),
        full_monthly_uplift=full_monthly,
        ramp_months=ramp_months,
        recovery_month_index=recovery_index,
        baseline_ews=baseline_ews,
    )
