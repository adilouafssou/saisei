"""Feature 5 — the multi-period P&L bridge (損益ブリッジ).

A hand-rolled, dependency-free DUAL-AXIS SVG that answers the question the
recovery curve does not: "how does 経常利益 (ordinary profit) climb to break-even,
month by month?". It is the charts toolkit's intended dual-axis consumer
(Feature 9 §8):

- **経常利益 bars** on the RIGHT axis, growing from the break-even (0) line —
  red below break-even (loss), green above (profit);
- a dashed **break-even 0 line** — the single most important reference;
- the **EWS line** on the LEFT axis (same 0..100 scale as the recovery curve),
  so the banker sees profit recovering and risk falling together;
- a collapsible **data table** mirroring the figures for assistive tech / print.

The component is PURE PRESENTATION: it reads only the ``bridge_*`` geometry vars
on ``SaiseiUIState`` (which only map the deterministic projection's
already-computed figures to pixels). It never computes a figure. It lives in the
Plan tab of the meta-interface and self-hides when no projection exists.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, RADII, SHADOW, TABLE_STYLE, TYPE

__all__ = ["pnl_bridge"]

#: Logical viewBox (matches the geometry vars in state / the recovery chart).
_VB_W = 720
_VB_H = 300


def _defs() -> rx.Component:
    """SVG <defs>: profit/loss bar gradients + the EWS line gradient."""
    return rx.el.svg.defs(
        # Profit bars (above break-even): green, growth.
        rx.el.svg.linear_gradient(
            rx.el.svg.stop(offset="0%", stop_color=COLORS["positive"], stop_opacity="0.9"),
            rx.el.svg.stop(offset="100%", stop_color=COLORS["positive"], stop_opacity="0.25"),
            id="bridgeProfitGrad",
            x1="0",
            y1="0",
            x2="0",
            y2="1",
        ),
        # Loss bars (below break-even): warm red.
        rx.el.svg.linear_gradient(
            rx.el.svg.stop(offset="0%", stop_color=COLORS["fail"], stop_opacity="0.3"),
            rx.el.svg.stop(offset="100%", stop_color=COLORS["fail"], stop_opacity="0.85"),
            id="bridgeLossGrad",
            x1="0",
            y1="0",
            x2="0",
            y2="1",
        ),
        # EWS line gradient: warm (distress) -> blue -> green (recovery).
        rx.el.svg.linear_gradient(
            rx.el.svg.stop(offset="0%", stop_color=COLORS["warn"]),
            rx.el.svg.stop(offset="55%", stop_color=COLORS["chrome"]),
            rx.el.svg.stop(offset="100%", stop_color=COLORS["positive"]),
            id="bridgeEwsGrad",
            x1="0",
            y1="0",
            x2="1",
            y2="0",
        ),
        custom_attrs={"xmlns": "http://www.w3.org/2000/svg"},
    )


def _profit_bar(point: rx.Var[dict[str, str]]) -> rx.Component:
    """One 経常利益 bar; green above break-even, red below."""
    return rx.el.svg.rect(
        x=point["bar_x"],
        y=point["bar_y"],
        width=point["bar_w"],
        height=point["bar_h"],
        rx="2",
        fill=rx.cond(
            point["profitable"] == "yes",
            "url(#bridgeProfitGrad)",
            "url(#bridgeLossGrad)",
        ),
    )


def _ews_dot(point: rx.Var[dict[str, str]]) -> rx.Component:
    """An EWS data dot; the recovered month gets the positive accent."""
    return rx.el.svg.circle(
        cx=point["cx"],
        cy=point["cy"],
        r=rx.cond(point["recovered"] == "yes", "4.5", "3"),
        fill=rx.cond(point["recovered"] == "yes", COLORS["positive"], COLORS["surface"]),
        stroke=rx.cond(point["recovered"] == "yes", COLORS["positive"], COLORS["chrome"]),
        stroke_width="2",
    )


def _hover_point(point: rx.Var[dict[str, str]]) -> rx.Component:
    """Per-month interactive group: a wide hover band + CSS-revealed tooltip.

    Reuses the same pure-CSS hover mechanism as the recovery chart
    (``.saisei-chart-hover:hover .saisei-chart-tip`` in THEME_CSS) — no state
    round-trip, no new deps. Shows the month's 経常利益 / EWS / 月次改善.
    """
    return rx.el.svg.g(
        rx.el.svg.circle(
            cx=point["cx"],
            cy=point["cy"],
            r=6,
            fill="none",
            stroke=COLORS["chrome"],
            stroke_width="2",
            class_name="saisei-chart-tip",
        ),
        rx.el.svg.rect(
            x=point["bar_x"],
            y=point["bar_y"],
            width=point["bar_w"],
            height=point["bar_h"],
            rx="2",
            fill="none",
            stroke=COLORS["text"],
            stroke_width="1.5",
            class_name="saisei-chart-tip",
        ),
        # Invisible wide hover target LAST so it catches the cursor across the
        # whole month column.
        rx.el.svg.rect(
            x=rx.cond(point["profitable"] == "yes", point["bar_x"], point["bar_x"]),
            y="24",
            width=point["bar_w"],
            height="240",
            fill="transparent",
        ),
        rx.el.svg.title(
            point["month_label"] + " ・ " + point["keijo_label"] + " ・ " + point["ews_label"]
        ),
        class_name="saisei-chart-hover",
    )


def _chart_svg() -> rx.Component:
    return rx.el.svg(
        rx.el.svg.title(SaiseiUIState.bridge_aria_label, id="bridge-title"),
        rx.el.svg.desc(
            "経常利益（棒・右軸）とEWS（線・左軸）の推移、損益分岐（0）ライン。"
            "詳細は下のデータ表を参照。 (Ordinary-profit bars on the right axis, "
            "EWS line on the left, and the break-even line; see the data table.)",
            id="bridge-desc",
        ),
        _defs(),
        # Profit/loss bars (drawn first, behind the line).
        rx.foreach(SaiseiUIState.bridge_points, _profit_bar),
        # Break-even (0) line — dashed, the key reference.
        rx.cond(
            SaiseiUIState.bridge_breakeven_y != "",
            rx.el.svg.g(
                rx.el.svg.line(
                    x1=SaiseiUIState.recovery_x0,
                    y1=SaiseiUIState.bridge_breakeven_y,
                    x2=SaiseiUIState.recovery_x1,
                    y2=SaiseiUIState.bridge_breakeven_y,
                    stroke=COLORS["text_muted"],
                    stroke_width="1.5",
                    stroke_dasharray="5 4",
                    opacity="0.7",
                ),
                rx.el.svg.text(
                    "損益分岐 (break-even)",
                    x=SaiseiUIState.recovery_threshold_label_x,
                    y=SaiseiUIState.bridge_breakeven_y,
                    fill=COLORS["text_muted"],
                    font_size="11",
                    font_weight="600",
                ),
            ),
        ),
        # EWS line (left axis) + dots.
        rx.el.svg.polyline(
            points=SaiseiUIState.bridge_line_path,
            fill="none",
            stroke="url(#bridgeEwsGrad)",
            stroke_width="3",
            stroke_linecap="round",
            stroke_linejoin="round",
        ),
        rx.foreach(SaiseiUIState.bridge_points, _ews_dot),
        # Interactive hover layer LAST.
        rx.foreach(SaiseiUIState.bridge_points, _hover_point),
        view_box=f"0 0 {_VB_W} {_VB_H}",
        width="100%",
        height="auto",
        custom_attrs={
            "preserveAspectRatio": "xMidYMid meet",
            "role": "img",
            "aria-labelledby": "bridge-title bridge-desc",
        },
        style={"display": "block", "maxWidth": "100%"},
    )


def _legend() -> rx.Component:
    def _chip(color: rx.Var[str] | str, label: str) -> rx.Component:
        return rx.hstack(
            rx.box(width="10px", height="10px", border_radius=RADII["pill"], background=color),
            rx.text(label, style=TYPE["caption"], color=COLORS["text_faint"]),
            align="center",
            spacing="2",
        )

    return rx.hstack(
        _chip(COLORS["positive"], "経常利益・黒字 (profit)"),
        _chip(COLORS["fail"], "経常利益・赤字 (loss)"),
        _chip(COLORS["chrome"], "EWSスコア (line)"),
        spacing="4",
        wrap="wrap",
    )


def _data_table() -> rx.Component:
    """Collapsible semantic data table mirroring the bridge figures."""

    def _row(r: rx.Var[dict[str, str]]) -> rx.Component:
        return rx.table.row(
            rx.table.row_header_cell(r["month"]),
            rx.table.cell(r["period"]),
            rx.table.cell(r["keijo"]),
            rx.table.cell(r["uplift"]),
            rx.table.cell(r["ews"]),
            rx.table.cell(r["state"]),
        )

    return rx.el.details(
        rx.el.summary(
            rx.hstack(
                rx.icon("table-2", size=13, color=COLORS["text_faint"]),
                rx.text("データ表 (Data table)", style=TYPE["caption"], color=COLORS["text_faint"]),
                align="center",
                spacing="2",
            ),
            style={"cursor": "pointer", "listStyle": "none"},
        ),
        rx.table.root(
            rx.table.header(
                rx.table.row(
                    rx.table.column_header_cell("月 (M)"),
                    rx.table.column_header_cell("期間 (Period)"),
                    rx.table.column_header_cell("経常利益 (Keijo)"),
                    rx.table.column_header_cell("月次改善 (Uplift)"),
                    rx.table.column_header_cell("EWS"),
                    rx.table.column_header_cell("状態 (State)"),
                )
            ),
            rx.table.body(rx.foreach(SaiseiUIState.bridge_table_rows, _row)),
            variant="surface",
            size="1",
            width="100%",
            style=TABLE_STYLE,
            margin_top="8px",
        ),
        style={"width": "100%"},
    )


def pnl_bridge() -> rx.Component:
    """Render the multi-period P&L bridge card (only when a projection exists)."""
    return rx.cond(
        SaiseiUIState.has_pnl_bridge,
        rx.vstack(
            rx.hstack(
                rx.icon("chart-column-big", size=16, color=COLORS["positive"]),
                rx.heading("損益ブリッジ (P&L bridge)", size="4", color=COLORS["text"]),
                align="center",
                spacing="2",
            ),
            rx.badge(
                SaiseiUIState.bridge_caption,
                variant="soft",
                color_scheme="grass",
                radius="full",
                size="2",
            ),
            rx.box(
                _chart_svg(),
                padding="12px",
                background=COLORS["surface"],
                border=f"1px solid {COLORS['border']}",
                border_radius=RADII["lg"],
                box_shadow=SHADOW["sm"],
                width="100%",
            ),
            _data_table(),
            _legend(),
            spacing="3",
            width="100%",
            align="start",
        ),
    )
