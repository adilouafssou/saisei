"""Case-file dashboard: the facts column of the meeting room.

Shows the company header and a grid of metric cards (FSA classification, EWS
score, working-capital gap, guarantee-release score, succession readiness),
plus the deterministic burden-sharing table once the meeting has consolidated.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, RADII, SHADOW

__all__ = ["ews_dashboard", "burden_table"]


def _metric(
    label: str,
    value: rx.Var[str] | str,
    *,
    accent: rx.Var[str] | str = COLORS["text"],
    icon: str = "circle",
) -> rx.Component:
    """Render a single metric card."""
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.icon(icon, size=14, color=COLORS["text_faint"]),
                rx.text(label, size="1", color=COLORS["text_faint"]),
                align="center",
                spacing="2",
            ),
            rx.heading(value, size="6", style={"color": accent}),
            spacing="2",
            align="start",
        ),
        padding="14px 16px",
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["md"],
        box_shadow=SHADOW["sm"],
        flex="1 1 150px",
        min_width="150px",
    )


def _classification_accent() -> rx.Var[str]:
    """Color the classification card by severity."""
    return rx.match(
        SaiseiUIState.fsa_kanji,
        ("正常", COLORS["pass"]),
        ("要注意", COLORS["warn"]),
        ("要管理", COLORS["fail"]),
        COLORS["text_muted"],
    )


def ews_dashboard() -> rx.Component:
    """Render the case-file header + metric grid."""
    return rx.vstack(
        rx.hstack(
            rx.icon("building-2", size=20, color=COLORS["brand"]),
            rx.heading(
                rx.cond(SaiseiUIState.company_name != "", SaiseiUIState.company_name, "—"),
                size="6",
                color=COLORS["text"],
            ),
            align="center",
            spacing="2",
            width="100%",
        ),
        rx.flex(
            _metric(
                "債務者区分 (FSA)",
                SaiseiUIState.classification_label,
                accent=_classification_accent(),
                icon="shield",
            ),
            _metric(
                "EWS Score",
                SaiseiUIState.ews_score.to_string(),
                icon="activity",
            ),
            _metric(
                "資金繰り (Shikin Kuri)",
                SaiseiUIState.working_capital_gap_display,
                icon="banknote",
            ),
            _metric(
                "保証解除 (Hosho Kaijo)",
                SaiseiUIState.hosho_kaijo_score.to_string(),
                icon="unlock",
            ),
            _metric(
                "承継準備 (Succession)",
                rx.cond(SaiseiUIState.succession_ready, "✓ 準備完了", "✗ 未準備"),
                accent=rx.cond(
                    SaiseiUIState.succession_ready, COLORS["pass"], COLORS["text_muted"]
                ),
                icon="users",
            ),
            gap="12px",
            wrap="wrap",
            width="100%",
        ),
        rx.cond(
            SaiseiUIState.error != "",
            rx.callout(
                SaiseiUIState.error,
                color_scheme="red",
                icon="triangle_alert",
                width="100%",
            ),
        ),
        spacing="3",
        width="100%",
    )


def _burden_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.table.row(
        rx.table.row_header_cell(row["persona"]),
        rx.table.cell(row["share"]),
        rx.table.cell(row["grace"]),
        rx.table.cell(row["haircut"]),
        rx.table.cell(row["new_money"]),
        rx.table.cell(
            rx.badge(row["allocation"], variant="soft", color_scheme="gray")
        ),
    )


def burden_table() -> rx.Component:
    """Render the deterministic per-lender burden-sharing table."""
    return rx.cond(
        SaiseiUIState.burden_rows.length() > 0,
        rx.vstack(
            rx.hstack(
                rx.icon("split", size=16, color=COLORS["brand"]),
                rx.heading(
                    "負担分担表 (Burden-Sharing)", size="4", color=COLORS["text"]
                ),
                align="center",
                spacing="2",
            ),
            rx.table.root(
                rx.table.header(
                    rx.table.row(
                        rx.table.column_header_cell("貸出人"),
                        rx.table.column_header_cell("負担比率"),
                        rx.table.column_header_cell("猶予"),
                        rx.table.column_header_cell("ヘアカット"),
                        rx.table.column_header_cell("新規融資"),
                        rx.table.column_header_cell("配分方式"),
                    )
                ),
                rx.table.body(rx.foreach(SaiseiUIState.burden_rows, _burden_row)),
                variant="surface",
                size="1",
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
    )
