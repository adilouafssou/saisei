"""Regression: the Core Banking boundary guard must catch duplicate periods.

The old guard only checked the gross-profit identity, which is COMPUTED by
TrialBalance (uriage_sourieki = sales - COGS) and therefore can never fail on a
model instance — dead protection that still claimed to catch "upstream data
drift". Duplicate periods are real drift the model cannot express: the EWS
window treats each row as a distinct month and uses [0]/[-1] as endpoints, so a
duplicated month double-counts and distorts the score. This pins the new guard.
Fully offline.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.backend.tools.core_banking_client import (
    CoreBankingBoundaryError,
    guard_shisanhyo,
)
from app.shared.models.accounting import TrialBalance


def _row(period: dt.date, uriage: int = 100_000_000) -> TrialBalance:
    return TrialBalance(
        period=period,
        uriage=uriage,
        uriage_genka=60_000_000,
        hanbaihi=1_000_000,
    )


def test_guard_rejects_duplicate_periods() -> None:
    rows = [
        _row(dt.date(2025, 3, 31)),
        _row(dt.date(2025, 4, 30)),
        _row(dt.date(2025, 3, 31)),  # duplicate of the first month
    ]
    with pytest.raises(CoreBankingBoundaryError, match="duplicate"):
        guard_shisanhyo(rows)


def test_guard_accepts_distinct_periods() -> None:
    rows = [
        _row(dt.date(2025, 3, 31)),
        _row(dt.date(2025, 4, 30)),
        _row(dt.date(2025, 5, 31)),
    ]
    assert guard_shisanhyo(rows) is rows


def test_guard_still_rejects_empty_series() -> None:
    with pytest.raises(CoreBankingBoundaryError):
        guard_shisanhyo([])
