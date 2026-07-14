"""Feature 5 — the recovery-curve chart (損益計画チャート).

A hand-rolled, dependency-free SVG visualisation of the deterministic recovery
projection. It tells the turnaround story at a glance:

- **falling EWS line** (with a gradient stroke + soft area fill) descending
  from the distressed baseline toward health;
- **rising uplift bars** showing the phased monthly 経常利益 improvement;
- a dashed **EWS 40 「正常」 threshold** with the healthy zone shaded green;
- a **pulsing marker** on the month the curve crosses into 正常 territory.

The component is PURE PRESENTATION: it reads only the geometry vars on
``SaiseiUIState`` (which themselves only map the deterministic projection's
already-computed numbers to pixel coordinates). It never computes a figure.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, FONT, RADII, SHADOW, TABLE_STYLE, TYPE

__all__ = ["recovery_chart"]

#: Logical viewBox (matches the geometry vars in state).
_VB_W = 720
_VB_H = 300


def _defs() -> rx.Component:
    """SVG <defs>: gradients + a glow filter for the curve and marker."""
    return rx.el.svg.defs(
        # EWS line gradient: warm (distress) → green (recovery), left to right.
        rx.el.svg.linear_gradient(
            rx.el.svg.stop(offset="0%", stop_color=COLORS["warn"]),
            rx.el.svg.stop(offset="55%", stop_color=COLORS["chrome"]),
            rx.el.svg.stop(offset="100%", stop_color=COLORS["positive"]),
            id="ewsLineGrad",
            x1="0",
            y1="0",
            x2="1",
            y2="0",
        ),
        # Soft area fill under the curve (chrome → transparent).
        rx.el.svg.linear_gradient(
            rx.el.svg.stop(offset="0%", stop_color=COLORS["chrome"], stop_opacity="0.22"),
            rx.el.svg.stop(offset="100%", stop_color=COLORS["chrome"], stop_opacity="0"),
            id="ewsAreaGrad",
            x1="0",
            y1="0",
            x2="0",
            y2="1",
        ),
        # Uplift bar gradient (green, growth), top brighter.
        rx.el.svg.linear_gradient(
            rx.el.svg.stop(offset="0%", stop_color=COLORS["positive"], stop_opacity="0.85"),
            rx.el.svg.stop(offset="100%", stop_color=COLORS["positive"], stop_opacity="0.18"),
            id="upliftBarGrad",
            x1="0",
            y1="0",
            x2="0",
            y2="1",
        ),
        # Healthy-zone wash (below the 40 line).
        rx.el.svg.linear_gradient(
            rx.el.svg.stop(offset="0%", stop_color=COLORS["positive"], stop_opacity="0.10"),
            rx.el.svg.stop(offset="100%", stop_color=COLORS["positive"], stop_opacity="0.03"),
            id="healthyZoneGrad",
            x1="0",
            y1="0",
            x2="0",
            y2="1",
        ),
        custom_attrs={"xmlns": "http://www.w3.org/2000/svg"},
    )


def _uplift_bar(point: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.el.svg.rect(
        x=point["bar_x"],
        y=point["bar_y"],
        width=point["bar_w"],
        height=point["bar_h"],
        rx="2",
        fill="url(#upliftBarGrad)",
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
    """Per-month interactive group: wide hover band + a CSS-revealed tooltip.

    The tooltip is hidden by default and made visible when the group is hovered
    (``.saisei-chart-hover:hover .saisei-chart-tip`` in THEME_CSS) — pure CSS, no
    state round-trip, no hover lag, no new deps. It shows that month's
    already-formatted month / EWS / 月次改善 / 経常利益 values.
    """
    return rx.el.svg.g(
        # A vertical guide line that appears on hover (subtle).
        rx.el.svg.line(
            x1=point["cx"],
            y1=point["tip_y"],
            x2=point["cx"],
            y2=point["cy"],
            stroke=COLORS["chrome"],
            stroke_width="1",
            stroke_dasharray="3 3",
            class_name="saisei-chart-tip",
        ),
        # Highlight ring on the hovered dot.
        rx.el.svg.circle(
            cx=point["cx"],
            cy=point["cy"],
            r=6,
            fill="none",
            stroke=COLORS["chrome"],
            stroke_width="2",
            class_name="saisei-chart-tip",
        ),
        # Tooltip box.
        rx.el.svg.rect(
            x=point["tip_x"],
            y=point["tip_y"],
            width=point["tip_w"],
            height=point["tip_h"],
            rx="8",
            fill=COLORS["text"],
            opacity="0.95",
            class_name="saisei-chart-tip",
        ),
        rx.el.svg.text(
            point["month_label"],
            x=point["tip_text_x"],
            y=point["tip_l1_y"],
            fill=COLORS["surface"],
            font_size="11",
            font_weight="700",
            class_name="saisei-chart-tip",
        ),
        rx.el.svg.text(
            point["ews_label"],
            x=point["tip_text_x"],
            y=point["tip_l2_y"],
            fill=COLORS["surface"],
            font_size="11",
            class_name="saisei-chart-tip",
        ),
        rx.el.svg.text(
            point["uplift_label"],
            x=point["tip_text_x"],
            y=point["tip_l3_y"],
            fill=COLORS["surface"],
            font_size="11",
            class_name="saisei-chart-tip",
        ),
        # Invisible wide hover target LAST so it sits on top and catches the
        # cursor across the whole month column.
        rx.el.svg.rect(
            x=point["band_x"],
            y=point["band_y"],
            width=point["band_w"],
            height=point["band_h"],
            fill="transparent",
        ),
        class_name="saisei-chart-hover",
    )


def _chart_svg() -> rx.Component:
    marker = SaiseiUIState.recovery_marker
    return rx.el.svg(
        # Accessible name/description: an SVG is opaque to screen readers unless
        # it carries a <title>/<desc> referenced by aria-labelledby and is given
        # role="img". These carry the deterministic recovery summary so a
        # non-sighted user gets the same at-a-glance read; the detailed figures
        # live in the data table below the chart.
        rx.el.svg.title(SaiseiUIState.recovery_aria_label, id="recovery-chart-title"),
        rx.el.svg.desc(
            "EWSスコアの推移（線）、月次改善額（棒）、正常ラインEWS40。"
            "詳細は下のデータ表を参照。 (EWS trend line, monthly-uplift bars, "
            "and the EWS 40 normal threshold; see the data table below for figures.)",
            id="recovery-chart-desc",
        ),
        _defs(),
        # Healthy zone: from the 40 line down to the plot floor.
        rx.el.svg.rect(
            x=SaiseiUIState.recovery_x0,
            y=SaiseiUIState.recovery_threshold_y,
            width=SaiseiUIState.recovery_w,
            height=SaiseiUIState.recovery_healthy_zone_h,
            fill="url(#healthyZoneGrad)",
        ),
        # Scrubber playhead: a vertical line at the selected month (hidden at 0).
        rx.cond(
            SaiseiUIState.scrubber_playhead_visible,
            rx.el.svg.g(
                rx.el.svg.line(
                    x1=SaiseiUIState.scrubber_playhead_x,
                    y1=SaiseiUIState.recovery_y0,
                    x2=SaiseiUIState.scrubber_playhead_x,
                    y2=SaiseiUIState.recovery_y1,
                    stroke=COLORS["chrome"],
                    stroke_width="2",
                    opacity="0.7",
                ),
                rx.el.svg.circle(
                    cx=SaiseiUIState.scrubber_playhead_x,
                    cy=SaiseiUIState.recovery_y0,
                    r="4",
                    fill=COLORS["chrome"],
                ),
            ),
        ),
        # Baseline plot frame (bottom + left axis).
        rx.el.svg.line(
            x1=SaiseiUIState.recovery_x0,
            y1=SaiseiUIState.recovery_y1,
            x2=SaiseiUIState.recovery_x1,
            y2=SaiseiUIState.recovery_y1,
            stroke=COLORS["border"],
            stroke_width="1",
        ),
        # Dashed EWS 40 threshold line.
        rx.el.svg.line(
            x1=SaiseiUIState.recovery_x0,
            y1=SaiseiUIState.recovery_threshold_y,
            x2=SaiseiUIState.recovery_x1,
            y2=SaiseiUIState.recovery_threshold_y,
            stroke=COLORS["positive"],
            stroke_width="1.5",
            stroke_dasharray="5 4",
            opacity="0.8",
        ),
        rx.el.svg.text(
            "正常 EWS 40",
            x=SaiseiUIState.recovery_threshold_label_x,
            y=SaiseiUIState.recovery_threshold_label_y,
            fill=COLORS["positive"],
            font_size="11",
            font_weight="600",
        ),
        # Uplift bars (drawn first, behind the curve).
        rx.foreach(SaiseiUIState.recovery_points, _uplift_bar),
        # Area fill under the EWS curve.
        rx.el.svg.path(d=SaiseiUIState.recovery_area_path, fill="url(#ewsAreaGrad)"),
        # The EWS curve itself (gradient stroke, rounded joins).
        rx.el.svg.polyline(
            points=SaiseiUIState.recovery_line_path,
            fill="none",
            stroke="url(#ewsLineGrad)",
            stroke_width="3",
            stroke_linecap="round",
            stroke_linejoin="round",
        ),
        # EWS dots.
        rx.foreach(SaiseiUIState.recovery_points, _ews_dot),
        # Pulsing recovery marker (only when recovery is reached).
        rx.cond(
            marker,
            rx.el.svg.g(
                rx.el.svg.circle(
                    cx=marker["cx"],
                    cy=marker["cy"],
                    r=9,
                    fill=COLORS["positive"],
                    opacity="0.35",
                    class_name="saisei-recovery-pulse",
                ),
                rx.el.svg.circle(
                    cx=marker["cx"],
                    cy=marker["cy"],
                    r=5,
                    fill=COLORS["positive"],
                    stroke=COLORS["surface"],
                    stroke_width="2",
                ),
            ),
        ),
        # Interactive hover layer LAST so tooltips render above everything.
        rx.foreach(SaiseiUIState.recovery_points, _hover_point),
        view_box=f"0 0 {_VB_W} {_VB_H}",
        width="100%",
        height="auto",
        custom_attrs={
            "preserveAspectRatio": "xMidYMid meet",
            "role": "img",
            "aria-labelledby": "recovery-chart-title recovery-chart-desc",
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
        _chip(COLORS["chrome"], "EWSスコア (line)"),
        _chip(COLORS["positive"], "月次改善額 (bars)"),
        _chip(COLORS["positive"], "正常ライン (EWS 40)"),
        spacing="4",
        wrap="wrap",
    )


def _scrubber() -> rx.Component:
    """The recovery time-scrubber: a month slider + play/pause + a live readout.

    Drag the slider (or press play) to move ``selected_month`` across the
    projected timeline; the chart playhead tracks it and the readout shows the
    projected EWS / 経常利益 / FSA band at that month. Display-only: it only reads
    already-computed projection values.
    """
    view = SaiseiUIState.selected_month_view
    recovered = view["recovered"] == "yes"
    return rx.vstack(
        # Live "at month N" readout.
        rx.hstack(
            rx.badge(
                view["label"],
                variant="soft",
                color_scheme="blue",
                radius="full",
                size="2",
            ),
            rx.spacer(),
            rx.hstack(
                rx.text("EWS", style=TYPE["caption"], color=COLORS["text_faint"]),
                rx.text(
                    view["ews"],
                    style={
                        "fontFamily": FONT["mono"],
                        "fontVariantNumeric": "tabular-nums",
                        "fontWeight": "700",
                    },
                    color=rx.cond(recovered, COLORS["positive"], COLORS["warn"]),
                ),
                rx.badge(
                    view["fsa"],
                    variant="soft",
                    color_scheme=rx.cond(recovered, "grass", "amber"),
                    radius="full",
                ),
                align="center",
                spacing="3",
            ),
            align="center",
            width="100%",
        ),
        # Controls: play/pause + slider + reset.
        rx.hstack(
            rx.button(
                rx.cond(
                    SaiseiUIState.scrubber_playing,
                    rx.icon("pause", size=16),
                    rx.icon("play", size=16),
                ),
                on_click=SaiseiUIState.scrubber_play,
                color_scheme="grass",
                variant="soft",
                size="2",
            ),
            rx.slider(
                min=0,
                max=SaiseiUIState.recovery_month_count,
                value=[SaiseiUIState.selected_month],
                on_change=SaiseiUIState.set_selected_month,
                color_scheme="grass",
                width="100%",
            ),
            rx.button(
                rx.icon("rotate-ccw", size=14),
                on_click=SaiseiUIState.scrubber_reset,
                color_scheme="gray",
                variant="soft",
                size="2",
            ),
            align="center",
            spacing="3",
            width="100%",
        ),
        # Secondary readout: projected monthly figures at the selected month.
        rx.hstack(
            rx.text(
                "月次改善 " + view["uplift"],
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            rx.text(
                "経常利益 " + view["keijo"],
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            spacing="4",
            wrap="wrap",
        ),
        spacing="3",
        width="100%",
    )


def _data_table() -> rx.Component:
    """A collapsible, semantic data table mirroring the chart's figures.

    The bespoke SVG is invisible to assistive tech, so the SAME deterministic
    per-month figures are exposed here as a real ``<table>`` inside a
    ``<details>`` (collapsed by default, so sighted users keep the clean chart
    but can expand to read/copy exact numbers, and screen-reader users + the
    printed Keikakusho always reach the data). Display-only: it renders
    ``recovery_table_rows`` verbatim and computes nothing.
    """

    def _row(r: rx.Var[dict[str, str]]) -> rx.Component:
        return rx.table.row(
            rx.table.row_header_cell(r["month"]),
            rx.table.cell(r["period"]),
            rx.table.cell(r["ews"]),
            rx.table.cell(r["uplift"]),
            rx.table.cell(r["keijo"]),
            rx.table.cell(r["recovered"]),
        )

    return rx.el.details(
        rx.el.summary(
            rx.hstack(
                rx.icon("table-2", size=13, color=COLORS["text_faint"]),
                rx.text(
                    "データ表 (Data table)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
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
                    rx.table.column_header_cell("EWS"),
                    rx.table.column_header_cell("月次改善 (Uplift)"),
                    rx.table.column_header_cell("経常利益 (Keijo)"),
                    rx.table.column_header_cell("状態 (State)"),
                )
            ),
            rx.table.body(rx.foreach(SaiseiUIState.recovery_table_rows, _row)),
            variant="surface",
            size="1",
            width="100%",
            style=TABLE_STYLE,
            margin_top="8px",
        ),
        # A short caption read by assistive tech, summarising the same story.
        rx.el.figcaption(
            SaiseiUIState.recovery_aria_label,
            style={
                "position": "absolute",
                "width": "1px",
                "height": "1px",
                "padding": "0",
                "margin": "-1px",
                "overflow": "hidden",
                "clip": "rect(0 0 0 0)",
                "whiteSpace": "nowrap",
                "border": "0",
            },
        ),
        style={"width": "100%"},
    )


def recovery_chart() -> rx.Component:
    """Render the recovery-curve chart card (only when a projection exists)."""
    return rx.cond(
        SaiseiUIState.has_recovery_projection,
        rx.vstack(
            rx.hstack(
                rx.icon("trending-up", size=16, color=COLORS["positive"]),
                rx.heading("損益計画カーブ (Recovery curve)", size="4", color=COLORS["text"]),
                align="center",
                spacing="2",
            ),
            rx.badge(
                SaiseiUIState.recovery_caption,
                variant="soft",
                color_scheme=rx.cond(SaiseiUIState.recovery_marker, "grass", "gray"),
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
            _scrubber(),
            _legend(),
            spacing="3",
            width="100%",
            align="start",
        ),
    )
