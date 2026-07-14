"""Regression net for the recovery-chart geometry refactor (Feature 9 §8).

The recovery chart's value->pixel math in ``SaiseiUIState`` was migrated to the
dependency-free charts toolkit (``app.frontend.components.charts``) so there is
one shared, tested scale implementation. Because there is no CI here, these
tests are the verifier: they pin that the toolkit-backed helpers produce the
EXACT pixel coordinates the hand-rolled version did, and that the toolkit's own
scale agrees with the chart's fixed-axis conventions.

The helpers are pure (no Reflex runtime), so they run on a bare instance.
"""

from __future__ import annotations

import pytest
from app.frontend.components.charts import Bounds, LinearScale
from app.frontend.state import SaiseiUIState
from app.shared.constants import EWS_SUBSTANDARD

# The fixed viewBox constants the chart uses (mirrored from SaiseiUIState).
_ML, _MR, _MT, _MB = 48, 16, 24, 36
_W, _H = 720, 300
_EWS_MAX = 100.0

# Expected inner-plot rectangle from the ORIGINAL hand-rolled formulae:
#   x0 = ML, y0 = MT, plot_w = W - ML - MR, plot_h = H - MT - MB
_EXP_X0 = float(_ML)
_EXP_Y0 = float(_MT)
_EXP_W = float(_W - _ML - _MR)  # 656.0
_EXP_H = float(_H - _MT - _MB)  # 240.0


def _inst() -> SaiseiUIState:
    """A bare state instance (geometry helpers need no Reflex session)."""
    return SaiseiUIState.__new__(SaiseiUIState)


def _orig_ews_y(ews: float, y0: float, plot_h: float) -> float:
    """The ORIGINAL hand-rolled EWS y-pixel formula, for parity comparison."""
    frac = max(0.0, min(1.0, ews / _EWS_MAX))
    return y0 + plot_h * (1.0 - frac)


def _orig_month_x(index: int, count: int, x0: float, plot_w: float) -> float:
    """The ORIGINAL hand-rolled month x-pixel formula, for parity comparison."""
    if count <= 1:
        return x0 + plot_w / 2.0
    return x0 + plot_w * (index - 1) / (count - 1)


def test_chart_plot_rectangle_unchanged() -> None:
    """The toolkit-backed plot rectangle matches the original (x0,y0,w,h)."""
    x0, y0, w, h = _inst()._chart_plot()
    assert (x0, y0, w, h) == (_EXP_X0, _EXP_Y0, _EXP_W, _EXP_H)


@pytest.mark.parametrize("ews", [0.0, EWS_SUBSTANDARD, 37.5, 50.0, 70.0, 85.0, 100.0])
def test_ews_y_matches_original(ews: float) -> None:
    """EWS->y delegation is byte-identical to the hand-rolled formula."""
    inst = _inst()
    got = inst._ews_y(ews, _EXP_Y0, _EXP_H)
    assert got == _orig_ews_y(ews, _EXP_Y0, _EXP_H)


def test_ews_y_inversion_endpoints() -> None:
    """EWS 0 sits on the plot floor; EWS_MAX sits at the plot ceiling."""
    inst = _inst()
    assert inst._ews_y(0.0, _EXP_Y0, _EXP_H) == _EXP_Y0 + _EXP_H  # bottom
    assert inst._ews_y(_EWS_MAX, _EXP_Y0, _EXP_H) == _EXP_Y0  # top


def test_ews_y_clamps_out_of_domain() -> None:
    """Out-of-range EWS values clamp to the plot floor/ceiling (no overflow)."""
    inst = _inst()
    assert inst._ews_y(-10.0, _EXP_Y0, _EXP_H) == _EXP_Y0 + _EXP_H
    assert inst._ews_y(999.0, _EXP_Y0, _EXP_H) == _EXP_Y0


@pytest.mark.parametrize("count", [1, 2, 3, 6, 12, 36])
def test_month_x_matches_original_all_positions(count: int) -> None:
    """Every month x-position equals the hand-rolled even-spacing formula."""
    inst = _inst()
    for index in range(1, count + 1):
        got = inst._month_x(index, count, _EXP_X0, _EXP_W)
        assert got == _orig_month_x(index, count, _EXP_X0, _EXP_W)


def test_month_x_spans_full_width_for_multi_month() -> None:
    """First month sits on the left edge, last on the right edge."""
    inst = _inst()
    assert inst._month_x(1, 6, _EXP_X0, _EXP_W) == _EXP_X0
    assert inst._month_x(6, 6, _EXP_X0, _EXP_W) == _EXP_X0 + _EXP_W


def test_single_month_is_centered() -> None:
    """A lone month is centered in the plot width."""
    inst = _inst()
    assert inst._month_x(1, 1, _EXP_X0, _EXP_W) == _EXP_X0 + _EXP_W / 2.0


def test_toolkit_scale_agrees_with_chart_helper() -> None:
    """The chart helper and a directly-built toolkit scale produce equal pixels.

    Guards against the chart and the toolkit drifting apart: the EWS y-pixel for
    the 正常 threshold must be the same whether read via the chart helper or via
    a LinearScale built straight from the toolkit over the same bounds.
    """
    inst = _inst()
    bounds = Bounds(x0=_EXP_X0, y0=_EXP_Y0, x1=_EXP_X0, y1=_EXP_Y0 + _EXP_H)
    direct = LinearScale.for_y(0.0, _EWS_MAX, bounds).scale(float(EWS_SUBSTANDARD))
    via_helper = inst._ews_y(float(EWS_SUBSTANDARD), _EXP_Y0, _EXP_H)
    assert direct == via_helper
