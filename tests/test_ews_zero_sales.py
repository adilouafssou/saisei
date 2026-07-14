"""Regression tests for EWS scoring with zero-sales months.

Locks in the fix that stops ``compute_ews_score`` from fabricating signals when
a trial-balance month has zero sales:

1. A zero-sales BASELINE no longer measures sales-decline against a fabricated
   ¥1 baseline (the old ``sales_first or 1``), which produced a meaningless,
   near-maximal decline signal.
2. A zero-sales month is no longer reported as a real 0% gross margin; the
   margin-compression signal skips an undefined endpoint instead.

The distress of a zero-sales month is still reflected via the loss-ratio and
ordinary-profit signals, so the score stays meaningful — it is just no longer
distorted by phantom values.

All tests are offline, deterministic, and import only from ``app.*``.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.ews_scoring import _gross_margin, compute_ews_score
from app.shared.models.accounting import TrialBalance


def _tb(
    period: dt.date,
    uriage: int,
    uriage_genka: int,
    hanbaihi: int = 0,
    eigai_shueki: int = 0,
    eigai_hiyo: int = 0,
) -> TrialBalance:
    return TrialBalance(
        period=period,
        uriage=uriage,
        uriage_genka=uriage_genka,
        hanbaihi=hanbaihi,
        eigai_shueki=eigai_shueki,
        eigai_hiyo=eigai_hiyo,
    )


class TestGrossMarginZeroSales:
    """_gross_margin must distinguish 'no sales' from a real 0% margin."""

    def test_zero_sales_returns_none(self) -> None:
        """A zero-sales month has an undefined margin (None), not 0.0."""
        tb = _tb(dt.date(2025, 4, 30), uriage=0, uriage_genka=0)
        assert _gross_margin(tb) is None

    def test_normal_margin_unchanged(self) -> None:
        """A normal month returns its real margin ratio."""
        tb = _tb(dt.date(2025, 4, 30), uriage=100, uriage_genka=70)
        margin = _gross_margin(tb)
        assert margin is not None
        assert abs(margin - 0.3) < 1e-9


class TestComputeEwsZeroSales:
    """compute_ews_score must not fabricate signals for zero-sales months."""

    def test_zero_sales_baseline_no_fabricated_decline(self) -> None:
        """A zero-sales BASELINE must not produce a near-maximal sales-drop.

        Before the fix, ``sales_first or 1`` measured the decline of ``last``
        against ¥1, so any positive ``last`` sales produced a huge (clamped)
        sales_drop. Here the baseline has zero sales and the firm then RECOVERS
        (positive sales, positive profit) — a fabricated decline would inflate
        the score. With the fix the sales signal contributes 0.
        """
        rows = [
            # Zero-sales baseline with break-even profit (keijo == 0, not a loss).
            _tb(dt.date(2025, 4, 30), uriage=0, uriage_genka=0, hanbaihi=0),
            _tb(
                dt.date(2025, 5, 31),
                uriage=100_000_000,
                uriage_genka=60_000_000,
                hanbaihi=20_000_000,
            ),
        ]
        score = compute_ews_score(rows)
        # No month is loss-making (keijo 0 then +20M), the firm RECOVERS, and the
        # zero-sales baseline yields an undefined (skipped) sales/margin signal.
        # Before the fix, the ¥1-baseline fabrication made sales_drop near-maximal
        # and inflated the score; with the fix every signal is 0.
        assert score == 0.0

    def test_zero_sales_final_month_is_distress_via_real_signals(self) -> None:
        """A firm collapsing TO zero sales scores high via real signals, not phantom 0% margin."""
        rows = [
            _tb(
                dt.date(2025, 4, 30),
                uriage=100_000_000,
                uriage_genka=60_000_000,
                hanbaihi=20_000_000,
            ),
            _tb(dt.date(2025, 5, 31), uriage=0, uriage_genka=0, hanbaihi=20_000_000),
        ]
        score = compute_ews_score(rows)
        # Real distress: full sales decline, ordinary-profit collapse, and the
        # final month is loss-making. The score must be clearly elevated.
        assert score > 40.0
        # And it must stay within the clamped range.
        assert 0.0 <= score <= 100.0

    def test_never_exceeds_bounds_with_zero_sales(self) -> None:
        """Score stays in [0, 100] even on all-zero-sales history."""
        rows = [
            _tb(dt.date(2025, 4, 30), uriage=0, uriage_genka=0),
            _tb(dt.date(2025, 5, 31), uriage=0, uriage_genka=0),
        ]
        score = compute_ews_score(rows)
        assert 0.0 <= score <= 100.0

    def test_normal_history_score_with_trend_signal(self) -> None:
        """A normal (no zero-sales) declining history scores as expected.

        Mirrors the needs_attention derivation in the golden spine. With the
        five-signal model (trend added, weights rebalanced) this monotonic
        two-month decline scores ~29.38:
          sales_drop 0.0125 -> 22*0.0375           = 0.825
          margin_drop 0.02215 -> 26*0.22152        = 5.76
          keijo_drop 0.4 -> 27*0.4                 = 10.80
          loss_ratio 0 -> 13*0                     = 0.0
          trend 1.0 (1/1 declining step) -> 12*1.0 = 12.0
          total                                    = 29.38
        The trend signal correctly adds weight to a monotonic decline.
        """
        rows = [
            _tb(
                dt.date(2025, 4, 30),
                uriage=80_000_000,
                uriage_genka=60_000_000,
                hanbaihi=15_000_000,
            ),
            _tb(
                dt.date(2025, 5, 31),
                uriage=79_000_000,
                uriage_genka=61_000_000,
                hanbaihi=15_000_000,
            ),
        ]
        score = compute_ews_score(rows)
        assert abs(score - 29.38) < 0.05

    def test_deterministic(self) -> None:
        """compute_ews_score is deterministic for zero-sales input."""
        rows = [
            _tb(dt.date(2025, 4, 30), uriage=0, uriage_genka=0, hanbaihi=10_000),
            _tb(
                dt.date(2025, 5, 31),
                uriage=50_000_000,
                uriage_genka=30_000_000,
                hanbaihi=10_000_000,
            ),
        ]
        assert compute_ews_score(rows) == compute_ews_score(rows)
