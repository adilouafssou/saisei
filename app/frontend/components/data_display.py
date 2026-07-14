"""Product-grade data-display primitives for Saisei.

The original tables used Radix's default ``variant="surface"`` with the generic
``TABLE_STYLE`` bolted on, so they read like a hand-made admin grid: heavy
uniform borders, left-aligned numbers, no hierarchy. For a financial product the
**numbers are the hero** — they must line up, be right-aligned with tabular
figures, and carry semantic colour (red for a loss, green for health).

This module is the single source of truth for that upgrade so every table
(Shisanhyo, burden-sharing, calibration, upload preview) inherits it at once:

- :data:`DATA_TABLE_STYLE` — refined table chrome (uppercase tracked headers,
  hairline row separators only, zebra + hover, mono tabular numerics in cells).
- :func:`data_table` — a thin wrapper that builds a styled ``rx.table.root``
  from header labels + a row renderer.
- :func:`num_cell` / :func:`money_cell` — right-aligned numeric cells; money
  cells colour negatives red and positives green by reading a pre-formatted
  string (the UI computes no figure — it only styles what state produced).
- :func:`section_title` — the consistent icon + heading row used above a table.

Display-only: nothing here computes a verdict or a number.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import reflex as rx

from app.frontend.theme import COLORS, FONT, RADII, TYPE

__all__ = [
    "DATA_TABLE_STYLE",
    "data_table",
    "num_cell",
    "money_cell",
    "money_sign",
    "text_cell",
    "section_title",
]

#: Shared style for the upgraded data tables. Key differences vs the old
#: TABLE_STYLE: uppercase letter-spaced caption headers, NO vertical borders and
#: only a hairline under each row, zebra striping + a clear hover, and tabular
#: mono numerics so digits align vertically (the single biggest "pro" signal).
DATA_TABLE_STYLE: dict[str, Any] = {
    "color": COLORS["text"],
    "width": "100%",
    "borderCollapse": "separate",
    "borderSpacing": "0",
    "borderRadius": RADII["md"],
    "overflow": "hidden",
    "border": f"1px solid {COLORS['border']}",
    # Header: small, uppercase, tracked, muted — a label, not a shout.
    "& th": {
        "color": COLORS["text_faint"],
        "fontSize": "11px",
        "fontWeight": "700",
        "letterSpacing": "0.06em",
        "textTransform": "uppercase",
        "background": COLORS["surface_2"],
        "borderBottom": f"1px solid {COLORS['border']}",
        "padding": "10px 14px",
        "whiteSpace": "nowrap",
    },
    # Cells: comfortable padding, hairline separators only (no column borders).
    "& td, & th[scope='row']": {
        "color": COLORS["text"],
        "fontSize": "13.5px",
        "padding": "11px 14px",
        "borderBottom": f"1px solid {COLORS['border']}",
        "verticalAlign": "middle",
    },
    "& tbody tr:last-child td, & tbody tr:last-child th[scope='row']": {
        "borderBottom": "none",
    },
    # Zebra + hover for scannability.
    "& tbody tr:nth-child(even)": {"background": COLORS["surface_2"]},
    "& tbody tr:hover": {"background": COLORS["surface_3"]},
    # Right-aligned numeric columns use tabular mono so digits line up.
    "& .saisei-num": {
        "textAlign": "right",
        "fontFamily": FONT["mono"],
        "fontVariantNumeric": "tabular-nums",
        "fontFeatureSettings": "'tnum' 1",
        "whiteSpace": "nowrap",
    },
}


def section_title(icon: str, label: str) -> rx.Component:
    """Render the consistent icon + heading row used above a data table."""
    return rx.hstack(
        rx.icon(icon, size=16, color=COLORS["chrome"]),
        rx.heading(label, style=TYPE["h3"], color=COLORS["text"]),
        align="center",
        spacing="2",
    )


def num_cell(value: rx.Var[str] | str) -> rx.Component:
    """Render a right-aligned tabular numeric cell (neutral colour)."""
    return rx.table.cell(value, class_name="saisei-num")


def money_sign(formatted: str) -> str:
    """Classify an already-¥-formatted money string by sign for colouring.

    Pure (no Reflex), so the colour RULE behind :func:`money_cell` is unit
    testable. Returns one of:

    - ``"negative"`` — a leading ``-`` (a loss / deficit) → fail colour;
    - ``"positive"`` — a genuine non-zero figure → positive colour;
    - ``"neutral"`` — zero (``¥0``) or a non-figure placeholder (``—``, empty)
      → default text colour, because ¥0 is not a positive outcome and ``—``
      means "not assessed".

    The positive test is "contains a 1-9 digit", so any magnitude > 0 (without a
    minus sign) is positive while a string whose only digit is 0, or which has
    no digits at all, is neutral.
    """
    text = (formatted or "").strip()
    if text.startswith("-"):
        return "negative"
    if any(ch in "123456789" for ch in text):
        return "positive"
    return "neutral"


def money_cell(formatted: rx.Var[str] | str, *, color_by_sign: bool = True) -> rx.Component:
    """Render a right-aligned money cell, optionally coloured by sign.

    ``formatted`` is the already-¥-formatted string from state (e.g.
    ``-¥5,000,000``); when ``color_by_sign`` is True the value is coloured by
    its sign per :func:`money_sign`:

    - a leading ``-`` paints it in the ``fail`` colour (a loss / deficit);
    - a genuine POSITIVE figure paints it in ``positive`` (green);
    - **zero (``¥0``) and non-figure placeholders (``—``, empty) stay NEUTRAL**
      (the default text colour), because ¥0 is not a positive outcome and
      ``—`` means "not assessed" — painting either green would mislead.

    The UI computes no figure — it only inspects an already-``format_jpy``
    string. The same three-way rule is encoded in :func:`money_sign` (pure,
    unit-tested) and mirrored here as Reflex ``rx.cond`` chains.
    """
    if not color_by_sign:
        return rx.table.cell(formatted, class_name="saisei-num")
    text = rx.Var.create(formatted).to(str)
    is_negative = text.startswith("-")
    # A genuine positive figure has a non-zero magnitude. Stripping the common
    # currency / grouping / sign characters and the digit 0 leaves something
    # non-empty ONLY when a 1-9 digit is present (e.g. "¥0" -> "", "—" has no
    # 1-9 so also neutral, "¥1,000" -> "1" -> positive). Mirrors money_sign.
    has_nonzero_digit = (
        text.replace("-", "")
        .replace("\u00a5", "")
        .replace("\uffe5", "")
        .replace("\u5186", "")
        .replace(",", "")
        .replace("0", "")
        .replace(" ", "")
        != ""
    )
    color = rx.cond(
        is_negative,
        COLORS["fail"],
        rx.cond(has_nonzero_digit, COLORS["positive"], COLORS["text"]),
    )
    return rx.table.cell(
        rx.text(formatted, style={"color": color, "fontWeight": "600"}),
        class_name="saisei-num",
    )


def text_cell(value: rx.Var[str] | str) -> rx.Component:
    """Render a plain left-aligned text cell."""
    return rx.table.cell(value)


def data_table(
    headers: list[str],
    rows: rx.Var[list[dict[str, str]]],
    row_renderer: Callable[[rx.Var[dict[str, str]]], rx.Component],
    *,
    size: str = "2",
) -> rx.Component:
    """Build a product-grade table from header labels + a row renderer.

    Args:
        headers: Column header labels (rendered as uppercase tracked captions).
        rows: The reactive list of row dicts.
        row_renderer: Builds one ``rx.table.row`` from a row dict var.
        size: Radix table size token.

    Returns:
        A styled ``rx.table.root`` using :data:`DATA_TABLE_STYLE`.
    """
    return rx.table.root(
        rx.table.header(rx.table.row(*[rx.table.column_header_cell(h) for h in headers])),
        rx.table.body(rx.foreach(rows, row_renderer)),
        size=size,
        width="100%",
        style=DATA_TABLE_STYLE,
    )
