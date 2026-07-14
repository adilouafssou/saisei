"""Tests for the portfolio charts primitives (Feature 8.1 watchlist).

The sparkline + deterioration-ranking helpers are pure geometry/ordering, so
they are fully unit-testable offline (no CI here -> these are the verifier).
They pin: sparkline auto-scaling (own min/max), the flat-series midline case,
empty handling, even x-spacing, the trend sign, and the deterministic
worst-first ranking (crossed-first, then EWS desc, then key asc).
"""

from __future__ import annotations

from app.frontend.components.charts import (
    Bounds,
    DeteriorationRow,
    build_band_distribution,
    build_sparkline,
    rank_by_deterioration,
    sparkline_trend,
)

# A short, wide cell like a watchlist sparkline column.
_CELL = Bounds(x0=0.0, y0=0.0, x1=120.0, y1=24.0)


def _coords(points: str) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for p in points.split(" "):
        x, y = p.split(",")
        coords.append((float(x), float(y)))
    return coords


class TestSparkline:
    def test_empty_is_empty(self) -> None:
        assert build_sparkline([], _CELL) == ""

    def test_one_point_centered(self) -> None:
        coords = _coords(build_sparkline([50.0], _CELL))
        assert len(coords) == 1
        # Single x is centered; flat domain -> row midline.
        assert coords[0][0] == 60.0
        assert coords[0][1] == 12.0

    def test_points_span_cell_width(self) -> None:
        coords = _coords(build_sparkline([10.0, 20.0, 30.0], _CELL))
        assert coords[0][0] == 0.0
        assert coords[-1][0] == 120.0

    def test_rising_series_descends_in_pixels(self) -> None:
        """Higher value -> smaller y (SVG inversion): a rising EWS goes UP."""
        coords = _coords(build_sparkline([10.0, 90.0], _CELL))
        # second value is higher -> its y must be smaller (nearer the top).
        assert coords[1][1] < coords[0][1]

    def test_flat_series_is_midline(self) -> None:
        coords = _coords(build_sparkline([42.0, 42.0, 42.0], _CELL))
        assert all(y == 12.0 for _x, y in coords)

    def test_padding_keeps_extremes_inside_cell(self) -> None:
        coords = _coords(build_sparkline([0.0, 100.0], _CELL, pad_frac=0.2))
        ys = [y for _x, y in coords]
        # With padding, neither extreme sits exactly on the cell edge.
        assert min(ys) > 0.0
        assert max(ys) < 24.0


class TestSparklineTrend:
    def test_rising(self) -> None:
        assert sparkline_trend([10.0, 50.0]) == 1

    def test_falling(self) -> None:
        assert sparkline_trend([50.0, 10.0]) == -1

    def test_flat(self) -> None:
        assert sparkline_trend([30.0, 30.0]) == 0

    def test_too_short(self) -> None:
        assert sparkline_trend([30.0]) == 0
        assert sparkline_trend([]) == 0


class TestRankByDeterioration:
    def test_crossed_rows_lead(self) -> None:
        rows = [
            DeteriorationRow(key="A", ews=90.0, crossed=False),
            DeteriorationRow(key="B", ews=50.0, crossed=True),
        ]
        ranked = rank_by_deterioration(rows)
        # B just crossed -> it leads despite a lower EWS than A.
        assert [r.key for r in ranked] == ["B", "A"]

    def test_then_by_ews_descending(self) -> None:
        rows = [
            DeteriorationRow(key="A", ews=40.0),
            DeteriorationRow(key="B", ews=80.0),
            DeteriorationRow(key="C", ews=60.0),
        ]
        ranked = rank_by_deterioration(rows)
        assert [r.key for r in ranked] == ["B", "C", "A"]

    def test_ties_broken_by_key_ascending(self) -> None:
        rows = [
            DeteriorationRow(key="Z", ews=70.0),
            DeteriorationRow(key="A", ews=70.0),
        ]
        ranked = rank_by_deterioration(rows)
        assert [r.key for r in ranked] == ["A", "Z"]

    def test_input_not_mutated(self) -> None:
        rows = [
            DeteriorationRow(key="A", ews=40.0),
            DeteriorationRow(key="B", ews=80.0),
        ]
        original = list(rows)
        rank_by_deterioration(rows)
        assert rows == original

    def test_deterministic_repeatable(self) -> None:
        rows = [
            DeteriorationRow(key="A", ews=80.0, crossed=True),
            DeteriorationRow(key="B", ews=80.0, crossed=True),
            DeteriorationRow(key="C", ews=90.0),
        ]
        assert rank_by_deterioration(rows) == rank_by_deterioration(list(rows))


# Four FSA health bands, best-first, as the portfolio panel passes them.
_BANDS = [
    ("normal", "正常", "positive"),
    ("attention", "要注意", "warn"),
    ("doubtful", "破綻懸念", "chrome"),
    ("danger", "実質破綻", "fail"),
]


class TestBuildBandDistribution:
    def test_empty_book_all_zero(self) -> None:
        bands = build_band_distribution(_BANDS, {})
        assert [b.count for b in bands] == [0, 0, 0, 0]
        assert [b.width_pct for b in bands] == [0.0, 0.0, 0.0, 0.0]

    def test_order_and_labels_preserved(self) -> None:
        bands = build_band_distribution(_BANDS, {"normal": 1})
        assert [b.key for b in bands] == ["normal", "attention", "doubtful", "danger"]
        assert [b.label for b in bands] == ["正常", "要注意", "破綻懸念", "実質破綻"]
        assert [b.accent for b in bands] == ["positive", "warn", "chrome", "fail"]

    def test_counts_tallied(self) -> None:
        counts = {"normal": 2, "attention": 1, "doubtful": 0, "danger": 1}
        bands = build_band_distribution(_BANDS, counts)
        assert [b.count for b in bands] == [2, 1, 0, 1]

    def test_widths_sum_to_100(self) -> None:
        # 3 borrowers split unevenly -> widths must still total exactly 100.
        counts = {"normal": 1, "attention": 1, "doubtful": 1, "danger": 0}
        bands = build_band_distribution(_BANDS, counts)
        assert round(sum(b.width_pct for b in bands), 2) == 100.0

    def test_remainder_absorbed_by_largest(self) -> None:
        # 1/1/1 -> 33.33 each leaves 0.01 drift; the largest band absorbs it.
        counts = {"normal": 2, "attention": 1, "doubtful": 1, "danger": 0}
        bands = build_band_distribution(_BANDS, counts)
        assert round(sum(b.width_pct for b in bands), 2) == 100.0
        # 'normal' has the most borrowers, so it carries the rounding remainder.
        normal = next(b for b in bands if b.key == "normal")
        assert normal.width_pct >= 50.0

    def test_negative_counts_floored_to_zero(self) -> None:
        bands = build_band_distribution(_BANDS, {"normal": -3, "attention": 2})
        normal = next(b for b in bands if b.key == "normal")
        assert normal.count == 0

    def test_deterministic_repeatable(self) -> None:
        counts = {"normal": 3, "attention": 2, "doubtful": 1, "danger": 1}
        assert build_band_distribution(_BANDS, counts) == build_band_distribution(
            _BANDS, dict(counts)
        )
