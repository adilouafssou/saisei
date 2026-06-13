"""Shisanhyo (trial balance) table component.

Renders the monthly J-GAAP figures with ¥-formatted values in the case-file
column. Collapses to nothing until data is available.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS

__all__ = ["shisanhyo_table"]


def _row(row: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.table.row(
        rx.table.row_header_cell(row["period"]),
        rx.table.cell(row["uriage"]),
        rx.table.cell(row["uriage_genka"]),
        rx.table.cell(row["keijo_rieki"]),
    )


def shisanhyo_table() -> rx.Component:
    """Render the monthly Shisanhyo table."""
    return rx.cond(
        SaiseiUIState.shisanhyo_rows.length() > 0,
        rx.vstack(
            rx.hstack(
                rx.icon("table", size=16, color=COLORS["brand"]),
                rx.heading("試算表 (Shisanhyo)", size="4", color=COLORS["text"]),
                align="center",
                spacing="2",
            ),
            rx.table.root(
                rx.table.header(
                    rx.table.row(
                        rx.table.column_header_cell("期間 (Period)"),
                        rx.table.column_header_cell("売上 (Uriage)"),
                        rx.table.column_header_cell("売上原価 (Genka)"),
                        rx.table.column_header_cell("経常利益 (Keijo)"),
                    )
                ),
                rx.table.body(rx.foreach(SaiseiUIState.shisanhyo_rows, _row)),
                variant="surface",
                size="1",
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
    )
