"""Shisanhyo (trial balance) table component.

Renders the monthly J-GAAP figures with ¥-formatted values in the case-file
column. Collapses to nothing until data is available.

Uses the product-grade :mod:`app.frontend.components.data_display` primitives
(tabular mono numerics, right-aligned + sign-coloured money) so the table reads
as a financial instrument, not a hand-made admin grid.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.components.data_display import (
    data_table,
    money_cell,
    num_cell,
    section_title,
)
from app.frontend.state import SaiseiUIState

__all__ = ["shisanhyo_table"]


def _row(row: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.table.row(
        rx.table.row_header_cell(row["period"]),
        num_cell(row["uriage"]),
        num_cell(row["uriage_genka"]),
        # 経常利益 is the signal line — colour it by sign (loss = red).
        money_cell(row["keijo_rieki"]),
    )


def shisanhyo_table() -> rx.Component:
    """Render the monthly Shisanhyo table."""
    return rx.cond(
        SaiseiUIState.shisanhyo_rows.length() > 0,
        rx.vstack(
            section_title("table", "試算表 (Shisanhyo)"),
            data_table(
                [
                    "期間 (Period)",
                    "売上 (Uriage)",
                    "売上原価 (Genka)",
                    "経常利益 (Keijo)",
                ],
                SaiseiUIState.shisanhyo_rows,
                _row,
            ),
            spacing="3",
            width="100%",
        ),
    )
