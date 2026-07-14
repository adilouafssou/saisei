"""Hand-verifiable tests for the Feature 5 P&L recovery bridge.

The projection is pure deterministic arithmetic, so each assertion is traceable
by hand from the inputs — no CI or LLM required to trust it.
"""

from __future__ import annotations

import datetime as dt

from app.backend.analysis.pnl_recovery import project_recovery
from app.shared.constants import EWS_SUBSTANDARD
from app.shared.models.accounting import TrialBalance


def _declining_history() -> list[TrialBalance]:
    """A deteriorating 12-month history: sales slide, margin compresses, losses.

    Built so ``compute_ews_score`` returns a clearly-distressed baseline that an
    uplift can then pull back under 40.
    """
    rows: list[TrialBalance] = []
    for i in range(12):
        # Sales fall from 150M to ~120M; COGS share rises; keijo turns negative.
        sales = 150_000_000 - i * 2_500_000
        cogs = int(sales * (0.80 + i * 0.005))
        sga = 20_000_000
        rows.append(
            TrialBalance(
                period=dt.date(2025, 1, 1) + dt.timedelta(days=30 * i),
                uriage=sales,
                uriage_genka=cogs,
                hanbaihi=sga,
            )
        )
    return rows


def test_insufficient_history_returns_empty_projection() -> None:
    one = [
        TrialBalance(
            period=dt.date(2025, 1, 31),
            uriage=100_000_000,
            uriage_genka=80_000_000,
            hanbaihi=10_000_000,
        )
    ]
    proj = project_recovery(one, annual_uplift=12_000_000)
    assert proj.months == []
    assert proj.recovered is False
    assert proj.recovery_month_index is None


def test_full_monthly_uplift_is_annual_over_twelve() -> None:
    proj = project_recovery(
        _declining_history(),
        annual_uplift=54_000_000,
        stop_at_recovery=False,
        horizon_months=3,
    )
    # 54,000,000 / 12 = 4,500,000 exactly.
    assert proj.full_monthly_uplift == 4_500_000


def test_ramp_phases_uplift_in_linearly() -> None:
    proj = project_recovery(
        _declining_history(),
        annual_uplift=54_000_000,
        ramp_months=6,
        horizon_months=8,
        stop_at_recovery=False,
    )
    # Month k books min(k,6)/6 of 4,500,000.
    assert proj.months[0].monthly_uplift == 750_000  # 1/6
    assert proj.months[2].monthly_uplift == 2_250_000  # 3/6
    assert proj.months[5].monthly_uplift == 4_500_000  # 6/6 (full)
    assert proj.months[6].monthly_uplift == 4_500_000  # steady-state


def test_ews_is_monotonically_non_increasing_during_ramp() -> None:
    proj = project_recovery(
        _declining_history(),
        annual_uplift=54_000_000,
        ramp_months=6,
        horizon_months=18,
        stop_at_recovery=False,
    )
    scores = [m.ews_score for m in proj.months]
    assert all(b <= a + 1e-9 for a, b in zip(scores, scores[1:], strict=False))


def test_recovery_is_reached_and_flagged() -> None:
    proj = project_recovery(
        _declining_history(),
        annual_uplift=120_000_000,  # generous uplift to guarantee recovery
        ramp_months=6,
        horizon_months=36,
    )
    assert proj.recovered is True
    assert proj.recovery_month_index is not None
    # The flagged recovery month is the first with EWS < 40, and stop_at_recovery
    # means it is the LAST month in the list.
    last = proj.months[-1]
    assert last.month_index == proj.recovery_month_index
    assert last.ews_score < EWS_SUBSTANDARD
    assert last.recovered is True
    # All earlier months were still >= 40 (not yet recovered).
    assert all(not m.recovered for m in proj.months[:-1])


def test_no_recovery_within_short_horizon_returns_none() -> None:
    proj = project_recovery(
        _declining_history(),
        annual_uplift=6_000_000,  # tiny uplift
        ramp_months=6,
        horizon_months=2,
        stop_at_recovery=True,
    )
    assert proj.recovery_month_index is None
    assert proj.recovered is False
    assert len(proj.months) == 2
