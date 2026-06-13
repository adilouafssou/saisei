"""Saisei main page — the creditor-meeting rehearsal room.

A production-grade two-column workspace:
- A sticky top bar with the brand, the TDB-code input, and the run button.
- Left column = the case file (EWS dashboard, Shisanhyo, burden-sharing table,
  and the resulting Keikakusho draft).
- Right column = the live creditor-meeting transcript with the inline HITL
  action bar.

This module is the canonical location under ``app.frontend.pages.index``.
The legacy path ``saisei_ui.saisei_ui`` re-exports from here.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.components.ews_dashboard import burden_table, ews_dashboard
from app.frontend.components.meeting_panel import meeting_panel
from app.frontend.components.shisanhyo_table import shisanhyo_table
from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, FONT, RADII, SHADOW

__all__ = ["index"]


def _brand() -> rx.Component:
    return rx.hstack(
        rx.box(
            rx.center(
                rx.text(
                    "再",
                    style={"fontSize": "22px", "fontWeight": "800", "color": COLORS["bg"]},
                ),
                width="100%",
                height="100%",
            ),
            width="40px",
            height="40px",
            background=COLORS["brand"],
            border_radius=RADII["md"],
            box_shadow=SHADOW["glow"],
        ),
        rx.vstack(
            rx.heading("Saisei 再生", size="5", color=COLORS["text"]),
            rx.text(
                "経営改善 Orchestrator",
                size="1",
                color=COLORS["text_faint"],
            ),
            spacing="0",
            align="start",
        ),
        align="center",
        spacing="3",
    )


def _phase_chip() -> rx.Component:
    """A status pill reflecting the current lifecycle phase."""
    return rx.badge(
        rx.match(
            SaiseiUIState.phase,
            ("idle", "待機中"),
            ("assessing", "診断中…"),
            ("meeting", "会議中…"),
            ("awaiting_decision", "決定待ち"),
            ("drafting", "計画書作成中…"),
            ("done", "完了"),
            ("error", "エラー"),
            "待機中",
        ),
        variant="soft",
        color_scheme=rx.match(
            SaiseiUIState.phase,
            ("awaiting_decision", "indigo"),
            ("done", "green"),
            ("error", "red"),
            "gray",
        ),
        radius="full",
        size="2",
    )


def _top_bar() -> rx.Component:
    return rx.box(
        rx.hstack(
            _brand(),
            rx.spacer(),
            rx.hstack(
                _phase_chip(),
                rx.input(
                    value=SaiseiUIState.tdb_code,
                    on_change=SaiseiUIState.set_tdb_code,
                    placeholder="TDB企業コード (7 digits)",
                    max_length=7,
                    width="200px",
                    size="3",
                ),
                rx.button(
                    rx.cond(
                        SaiseiUIState.is_running,
                        rx.spinner(size="2"),
                        rx.icon("play", size=16),
                    ),
                    "診断実行",
                    on_click=SaiseiUIState.run_assessment,
                    disabled=SaiseiUIState.is_running | ~SaiseiUIState.code_valid,
                    color_scheme="indigo",
                    size="3",
                ),
                align="center",
                spacing="3",
            ),
            align="center",
            width="100%",
        ),
        position="sticky",
        top="0",
        z_index="10",
        padding="14px 24px",
        background=COLORS["surface"] + "ee",
        border_bottom=f"1px solid {COLORS['border']}",
        backdrop_filter="blur(10px)",
        width="100%",
    )


def _panel(*children: rx.Component) -> rx.Component:
    """A column wrapper card."""
    return rx.box(
        rx.vstack(*children, spacing="5", width="100%"),
        padding="20px",
        background=COLORS["bg"],
        width="100%",
    )


def _case_file() -> rx.Component:
    return _panel(
        ews_dashboard(),
        shisanhyo_table(),
        burden_table(),
        rx.cond(
            SaiseiUIState.keikakusho_draft != "",
            rx.vstack(
                rx.hstack(
                    rx.icon("file-text", size=16, color=COLORS["brand"]),
                    rx.heading(
                        "経営改善計画書 (Keikakusho)",
                        size="4",
                        color=COLORS["text"],
                    ),
                    align="center",
                    spacing="2",
                ),
                rx.box(
                    rx.markdown(SaiseiUIState.keikakusho_draft),
                    padding="16px",
                    background=COLORS["surface"],
                    border=f"1px solid {COLORS['border']}",
                    border_radius=RADII["md"],
                    width="100%",
                ),
                spacing="2",
                width="100%",
            ),
        ),
    )


def _meeting_room() -> rx.Component:
    return rx.box(
        meeting_panel(),
        padding="20px",
        background=COLORS["surface"] + "55",
        border_left=f"1px solid {COLORS['border']}",
        min_height="calc(100vh - 69px)",
        width="100%",
    )


def index() -> rx.Component:
    """Render the main Saisei meeting-room page."""
    return rx.box(
        _top_bar(),
        rx.grid(
            _case_file(),
            _meeting_room(),
            grid_template_columns=["1fr", "1fr", "1fr", "minmax(0, 1.15fr) minmax(0, 0.85fr)"],
            width="100%",
            align_items="start",
        ),
        background=COLORS["bg"],
        min_height="100vh",
        width="100%",
        style={"fontFamily": FONT["sans"], "color": COLORS["text"]},
    )
