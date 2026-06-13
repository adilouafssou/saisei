"""Tests for J-GAAP accounting models."""

from __future__ import annotations

import datetime as dt

from app.shared.models.accounting import TrialBalance, fiscal_year_of


def _tb(**kwargs: int) -> TrialBalance:
    base = {
        "uriage": 100_000_000,
        "uriage_genka": 70_000_000,
        "hanbaihi": 20_000_000,
    }
    base.update(kwargs)
    return TrialBalance(period=dt.date(2025, 4, 30), **base)  # type: ignore[arg-type]


def test_derived_profit_lines() -> None:
    tb = _tb(eigai_shueki=1_000_000, eigai_hiyo=2_000_000)
    assert tb.uriage_sourieki == 30_000_000
    assert tb.eigyo_rieki == 10_000_000
    assert tb.keijo_rieki == 9_000_000


def test_negative_keijo_rieki() -> None:
    tb = _tb(uriage=80_000_000, uriage_genka=85_000_000, eigai_hiyo=3_000_000)
    assert tb.keijo_rieki < 0


def test_fiscal_year_march_end() -> None:
    assert fiscal_year_of(dt.date(2025, 4, 1)) == 2025
    assert fiscal_year_of(dt.date(2026, 3, 31)) == 2025
    assert fiscal_year_of(dt.date(2026, 4, 1)) == 2026


def test_trial_balance_fiscal_year_property() -> None:
    assert _tb().fiscal_year == 2025
