"""Tests for the dependency-free charting primitives (Feature 9 step 1).

The toolkit is pure geometry, so it is fully unit-testable offline. These tests
pin the contracts future charts depend on: linear scaling (incl. the SVG y
inversion and the degenerate flat-domain case), clamping, even x spacing, and
the line / area / bar path builders.

Imports only from ``app.*`` + stdlib + pytest.
"""

from __future__ import annotations

from app.frontend.components.charts import (
    Bounds,
    ChartGeometry,
    LinearScale,
    Series,
    SeriesKind,
    build_area_path,
    build_bars,
    build_polyline,
    linear_ticks,
)

_BOUNDS = Bounds(x0=0.0, y0=0.0, x1=100.0, y1=200.0)


class TestBounds:
    def test_width_and_height(self) -> None:
        assert _BOUNDS.width == 100.0
        assert _BOUNDS.height == 200.0

    def test_never_negative(self) -> None:
        inverted = Bounds(x0=100.0, y0=200.0, x1=0.0, y1=0.0)
        assert inverted.width == 0.0
        assert inverted.height == 0.0


class TestLinearScale:
    def test_x_scale_endpoints_and_mid(self) -> None:
        s = LinearScale.for_x(0.0, 10.0, _BOUNDS)
        assert s.scale(0.0) == 0.0
        assert s.scale(10.0) == 100.0
        assert s.scale(5.0) == 50.0

    def test_y_scale_is_inverted(self) -> None:
        # Higher value must map to a SMALLER pixel (top of the plot).
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        assert s.scale(0.0) == 200.0  # bottom (y1)
        assert s.scale(100.0) == 0.0  # top (y0)
        assert s.scale(50.0) == 100.0

    def test_clamps_out_of_domain(self) -> None:
        s = LinearScale.for_x(0.0, 10.0, _BOUNDS)
        assert s.scale(-5.0) == 0.0
        assert s.scale(99.0) == 100.0

    def test_degenerate_domain_maps_to_midpoint(self) -> None:
        s = LinearScale.for_x(5.0, 5.0, _BOUNDS)
        assert s.scale(5.0) == 50.0
        assert s.scale(999.0) == 50.0  # no division by zero


class TestLinearTicks:
    def test_count_and_endpoints(self) -> None:
        s = LinearScale.for_x(0.0, 100.0, _BOUNDS)
        ticks = linear_ticks(s, count=5)
        assert len(ticks) == 5
        assert ticks[0].value == 0.0
        assert ticks[-1].value == 100.0
        # Ticks line up with the scale.
        assert ticks[0].pixel == s.scale(0.0)
        assert ticks[-1].pixel == s.scale(100.0)

    def test_integer_labels(self) -> None:
        s = LinearScale.for_x(0.0, 4.0, _BOUNDS)
        ticks = linear_ticks(s, count=5, integer=True)
        assert [t.label for t in ticks] == ["0", "1", "2", "3", "4"]

    def test_count_clamped_to_two(self) -> None:
        s = LinearScale.for_x(0.0, 1.0, _BOUNDS)
        assert len(linear_ticks(s, count=1)) == 2


def _line(values: tuple[float, ...]) -> Series:
    return Series(key="ews", label="EWS", kind=SeriesKind.LINE, values=values)


class TestPolyline:
    def test_points_span_plot_width(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        poly = build_polyline(_line((100.0, 50.0, 0.0)), s, _BOUNDS)
        coords = [tuple(map(float, p.split(","))) for p in poly.split(" ")]
        # Three points, first at left edge, last at right edge.
        assert coords[0][0] == 0.0
        assert coords[-1][0] == 100.0
        # y inverted: value 100 -> top (0), value 0 -> bottom (200).
        assert coords[0][1] == 0.0
        assert coords[-1][1] == 200.0

    def test_single_point_is_centered(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        poly = build_polyline(_line((50.0,)), s, _BOUNDS)
        x, _y = map(float, poly.split(","))
        assert x == 50.0


class TestAreaPath:
    def test_area_closes_to_floor(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        path = build_area_path(_line((100.0, 0.0)), s, _BOUNDS)
        # Starts with a moveto, ends closed (Z), and touches the floor (y1=200).
        assert path.startswith("M ")
        assert path.endswith("Z")
        assert "200.00" in path  # the floor close

    def test_empty_series_is_empty_path(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        assert build_area_path(_line(()), s, _BOUNDS) == ""


def _bars(values: tuple[float, ...]) -> Series:
    return Series(
        key="uplift",
        label="Uplift",
        kind=SeriesKind.BARS,
        values=values,
        accent="positive",
    )


class TestBars:
    def test_one_bar_per_value(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        bars = build_bars(_bars((20.0, 40.0, 80.0)), s, _BOUNDS)
        assert len(bars) == 3

    def test_bar_grows_from_baseline_zero_upward(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        bars = build_bars(_bars((100.0,)), s, _BOUNDS)
        bar = bars[0]
        # value 100 -> top pixel 0; baseline 0 -> bottom pixel 200.
        assert bar.y == 0.0
        assert bar.height == 200.0

    def test_negative_value_draws_below_baseline(self) -> None:
        # Domain spanning negatives so a negative bar is representable.
        s = LinearScale.for_y(-100.0, 100.0, _BOUNDS)
        bars = build_bars(_bars((-100.0,)), s, _BOUNDS, baseline=0.0)
        bar = bars[0]
        base_px = s.scale(0.0)  # midpoint = 100
        val_px = s.scale(-100.0)  # bottom = 200
        assert bar.y == min(base_px, val_px)
        assert bar.height == abs(base_px - val_px)

    def test_width_ratio_controls_bar_width(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        narrow = build_bars(_bars((50.0, 50.0)), s, _BOUNDS, width_ratio=0.3)
        wide = build_bars(_bars((50.0, 50.0)), s, _BOUNDS, width_ratio=0.9)
        assert wide[0].width > narrow[0].width

    def test_empty_series(self) -> None:
        s = LinearScale.for_y(0.0, 100.0, _BOUNDS)
        assert build_bars(_bars(()), s, _BOUNDS) == []


class TestDualAxisSeries:
    def test_series_axis_selection(self) -> None:
        bounds = Bounds(0, 0, 100, 200)
        geom = ChartGeometry(
            bounds=bounds,
            x_scale=LinearScale.for_x(0, 5, bounds),
            y_left=LinearScale.for_y(0, 100, bounds),
            y_right=LinearScale.for_y(0, 1_000_000, bounds),
        )
        left_series = Series(
            key="ews", label="EWS", kind=SeriesKind.LINE, values=(1.0,), axis="left"
        )
        right_series = Series(
            key="yen", label="Yen", kind=SeriesKind.BARS, values=(1.0,), axis="right"
        )
        assert geom.y_for(left_series) is geom.y_left
        assert geom.y_for(right_series) is geom.y_right

    def test_right_axis_falls_back_to_left_when_absent(self) -> None:
        bounds = Bounds(0, 0, 100, 200)
        geom = ChartGeometry(
            bounds=bounds,
            x_scale=LinearScale.for_x(0, 5, bounds),
            y_left=LinearScale.for_y(0, 100, bounds),
            y_right=None,
        )
        right_series = Series(
            key="yen", label="Yen", kind=SeriesKind.BARS, values=(1.0,), axis="right"
        )
        assert geom.y_for(right_series) is geom.y_left
