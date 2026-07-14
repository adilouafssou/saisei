"""Verifier for the Feature 5 multi-period P&L bridge geometry (§8 / Plan tab).

The bridge is the charts toolkit's dual-axis consumer: EWS on the left
(0..100, inverted) and 経常利益 on the right (bars from the break-even 0 line).
With no CI, these tests are the verifier. They are pure: the ``bridge_*`` vars
are computed on a bare ``SaiseiUIState`` whose ``recovery_serialised`` is set to
a hand-written projection, so no Reflex runtime or backend is needed.

Every assertion checks that already-computed figures map to the EXPECTED pixels
(the UI computes no business value), and that the break-even / profit-loss
logic is correct.
"""

from __future__ import annotations

from typing import Any

from app.frontend.components.charts import Bounds, LinearScale
from app.frontend.state import SaiseiUIState

from tests._bare_state import bare_ui_state


def _fget(var: Any, inst: SaiseiUIState) -> Any:
    """Invoke a computed ``rx.var``'s getter directly (runtime ``.fget``).

    Reflex preserves the original getter on ``.fget``; this thin ``Any`` wrapper
    keeps the call site honest at runtime while satisfying the type checker,
    which does not model the descriptor's ``.fget`` attribute.
    """
    return var.fget(inst)


# Fixed viewBox plot rectangle (mirrors SaiseiUIState chart constants).
_X0, _Y0, _W, _H = 48.0, 24.0, 656.0, 240.0
_EWS_MAX = 100.0


def _with_projection(months: list[dict[str, Any]]) -> SaiseiUIState:
    """A bare state instance with ``recovery_serialised.months`` populated."""
    inst = bare_ui_state()
    inst.recovery_serialised = {"months": months}
    return inst


def _month(
    idx: int, *, keijo: int, ews: float, uplift: int = 0, recovered: bool = False
) -> dict[str, Any]:
    return {
        "month_index": idx,
        "period": f"2025-{idx:02d}-28",
        "keijo_rieki": keijo,
        "ews_score": ews,
        "monthly_uplift": uplift,
        "recovered": recovered,
    }


# A loss->profit ramp: month 1 is a loss, month 3 turns profitable.
_RAMP = [
    _month(1, keijo=-3_000_000, ews=78.0),
    _month(2, keijo=-1_000_000, ews=60.0),
    _month(3, keijo=2_000_000, ews=45.0),
    _month(4, keijo=5_000_000, ews=30.0, recovered=True),
]


def test_empty_projection_is_empty() -> None:
    """No projection -> no points, no line, no break-even line, not available."""
    inst = _with_projection([])
    assert _fget(SaiseiUIState.bridge_points, inst) == []
    assert _fget(SaiseiUIState.bridge_line_path, inst) == ""
    assert _fget(SaiseiUIState.bridge_breakeven_y, inst) == ""
    assert _fget(SaiseiUIState.has_pnl_bridge, inst) is False
    assert _fget(SaiseiUIState.bridge_table_rows, inst) == []


def test_one_point_per_month() -> None:
    inst = _with_projection(_RAMP)
    pts = _fget(SaiseiUIState.bridge_points, inst)
    assert len(pts) == len(_RAMP)
    assert [p["index"] for p in pts] == ["1", "2", "3", "4"]
    assert _fget(SaiseiUIState.has_pnl_bridge, inst) is True


def test_ews_uses_left_axis_scale() -> None:
    """Each EWS dot's cy equals the toolkit's 0..100 inverted left-axis pixel."""
    inst = _with_projection(_RAMP)
    pts = _fget(SaiseiUIState.bridge_points, inst)
    bounds = Bounds(x0=_X0, y0=_Y0, x1=_X0 + _W, y1=_Y0 + _H)
    ews_scale = LinearScale.for_y(0.0, _EWS_MAX, bounds)
    for p, m in zip(pts, _RAMP, strict=True):
        assert float(p["cy"]) == round(ews_scale.scale(float(m["ews_score"])), 2)


def test_profit_and_loss_flagged() -> None:
    """Bars are flagged profitable iff 経常利益 >= 0 (drives green vs red)."""
    inst = _with_projection(_RAMP)
    pts = _fget(SaiseiUIState.bridge_points, inst)
    assert [p["profitable"] for p in pts] == ["no", "no", "yes", "yes"]


def test_break_even_line_inside_plot() -> None:
    """The break-even (0) line falls within the plot vertical range."""
    inst = _with_projection(_RAMP)
    y = float(_fget(SaiseiUIState.bridge_breakeven_y, inst))
    assert _Y0 <= y <= _Y0 + _H


def test_loss_bar_below_breakeven_profit_bar_above() -> None:
    """A loss bar sits below the break-even line; a profit bar above it."""
    inst = _with_projection(_RAMP)
    pts = _fget(SaiseiUIState.bridge_points, inst)
    be = float(_fget(SaiseiUIState.bridge_breakeven_y, inst))
    # Month 1 is a loss: its bar starts at the break-even line and extends down,
    # so bar_y == break-even y (top of a downward bar).
    loss = pts[0]
    assert float(loss["bar_y"]) == round(be, 2)
    # Month 4 is a profit: the bar top is ABOVE (smaller y than) break-even.
    profit = pts[3]
    assert float(profit["bar_y"]) < be


def test_line_path_matches_points() -> None:
    inst = _with_projection(_RAMP)
    pts = _fget(SaiseiUIState.bridge_points, inst)
    expected = " ".join(f"{p['cx']},{p['cy']}" for p in pts)
    assert _fget(SaiseiUIState.bridge_line_path, inst) == expected


def test_x_positions_span_plot_width() -> None:
    """First month on the left edge, last on the right edge of the plot."""
    inst = _with_projection(_RAMP)
    pts = _fget(SaiseiUIState.bridge_points, inst)
    assert float(pts[0]["cx"]) == _X0
    assert float(pts[-1]["cx"]) == _X0 + _W


def test_caption_reports_break_even_month() -> None:
    inst = _with_projection(_RAMP)
    caption = _fget(SaiseiUIState.bridge_caption, inst)
    assert "3" in caption  # month 3 is the first non-negative keijo
    assert "Break-even in month 3" in caption


def test_caption_reports_no_break_even() -> None:
    """An all-loss projection reports no break-even within the horizon."""
    losses = [_month(1, keijo=-5_000_000, ews=80.0), _month(2, keijo=-4_000_000, ews=75.0)]
    inst = _with_projection(losses)
    caption = _fget(SaiseiUIState.bridge_caption, inst)
    assert "No break-even" in caption


def test_table_rows_state_labels() -> None:
    """Data-table rows label each month 黒字 (profit) or 赤字 (loss)."""
    inst = _with_projection(_RAMP)
    rows = _fget(SaiseiUIState.bridge_table_rows, inst)
    assert [r["state"] for r in rows] == ["赤字", "赤字", "黒字", "黒字"]
    assert [r["month"] for r in rows] == ["1", "2", "3", "4"]
