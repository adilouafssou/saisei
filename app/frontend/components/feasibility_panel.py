"""Advisory feasibility panel with per-claim provenance (Feature 0 phase 4).

The feasibility critic is ADVISORY ONLY: it never gates a verdict or moves a
figure. Its value to the banker is operational judgement — "can this firm
actually execute this strategy?" — but an LLM-phrased advisory is only safe to
act on if the banker can see *what each claim is grounded in*.

This panel renders, per strategy:

- the deterministic achievability band + score (coloured by the shared health
  gradient: higher score = more achievable = greener), and
- each advisory claim with a provenance chip:
    * grounded   → green chip naming the source(s) it resolves to ("per ews",
      "per past_keikakusho"), so the banker can weight an attributable claim; and
    * unverified → amber "未検証 / unverified" chip, so model commentary is
      visibly marked rather than mistaken for analysis.

This is the UI half of the project's core stance — *every claim attributable or
visibly unverified* — extended from numbers to qualitative prose. Display-only:
it reads ``SaiseiUIState.feasibility_rows`` (computed at snapshot time) and never
computes a verdict, figure, or route.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.components.data_display import section_title
from app.frontend.state import FeasibilityClaim, FeasibilityRow, SaiseiUIState
from app.frontend.theme import COLORS, RADII, TYPE

__all__ = ["feasibility_panel"]


def _band_badge(band: rx.Var[str], score: rx.Var[str]) -> rx.Component:
    """Render the deterministic achievability band + score as a coloured badge.

    Colour follows the shared health gradient via a band->scheme match (high =
    green, medium = amber, low = red), mirroring the EWS/score gradient used on
    the metric cards so the banker reads achievability the same way everywhere.
    """
    label = rx.match(
        band,
        ("high", "実現性 高 (High)"),
        ("medium", "実現性 中 (Medium)"),
        ("low", "実現性 低 (Low)"),
        "実現性 —",
    ).to_string()
    return rx.badge(
        rx.text(
            label + " · " + score.to_string(),
            style={"fontWeight": "600"},
        ),
        variant="soft",
        color_scheme=rx.match(
            band,
            ("high", "grass"),
            ("medium", "amber"),
            ("low", "red"),
            "gray",
        ),
        radius="full",
    )


def _provenance_chip(claim: rx.Var[FeasibilityClaim]) -> rx.Component:
    """Render the source chip for one advisory claim (grounded vs unverified)."""
    grounded = claim.status == "grounded"
    return rx.badge(
        rx.cond(
            grounded,
            rx.hstack(
                rx.icon("link", size=11),
                rx.text(
                    rx.cond(
                        claim.citations != "",
                        "出典: " + claim.citations,
                        "grounded",
                    )
                ),
                align="center",
                spacing="1",
            ),
            rx.hstack(
                rx.icon("circle-help", size=11),
                rx.text("未検証 / unverified"),
                align="center",
                spacing="1",
            ),
        ),
        variant="soft",
        color_scheme=rx.cond(grounded, "grass", "amber"),
        radius="full",
        size="1",
    )


def _claim_row(claim: rx.Var[FeasibilityClaim]) -> rx.Component:
    """Render one advisory claim sentence with its provenance chip.

    Grounded claims read in primary ink; unverified ones are muted and italic so
    the eye treats them as commentary, not analysis (reinforcing the chip).
    """
    grounded = claim.status == "grounded"
    return rx.hstack(
        rx.box(
            width="3px",
            min_width="3px",
            align_self="stretch",
            border_radius=RADII["pill"],
            background=rx.cond(grounded, COLORS["pass"], COLORS["warn"]),
        ),
        rx.vstack(
            rx.text(
                claim.text,
                style={
                    "fontSize": "13px",
                    "lineHeight": "1.5",
                    "color": rx.cond(grounded, COLORS["text"], COLORS["text_muted"]),
                    "fontStyle": rx.cond(grounded, "normal", "italic"),
                },
            ),
            _provenance_chip(claim),
            spacing="1",
            align="start",
            width="100%",
        ),
        align="start",
        spacing="2",
        width="100%",
    )


def _note_card(row: rx.Var[FeasibilityRow]) -> rx.Component:
    """Render one strategy's advisory feasibility note + grounded claims."""
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.text(row.title, weight="bold", size="2", color=COLORS["text"]),
                rx.spacer(),
                _band_badge(row.band, row.score),
                align="center",
                width="100%",
                wrap="wrap",
            ),
            rx.vstack(
                rx.foreach(row.provenance, _claim_row),
                spacing="3",
                width="100%",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        padding="14px 16px",
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        width="100%",
    )


def feasibility_panel() -> rx.Component:
    """Render the advisory feasibility panel, or nothing when there is no advisory.

    Collapses to nothing until at least one strategy has an advisory note with
    claim provenance, so offline (no-LLM) runs are visually unchanged.
    """
    return rx.cond(
        SaiseiUIState.feasibility_rows.length() > 0,
        rx.vstack(
            section_title("clipboard-check", "実現性助言 (Feasibility · advisory)"),
            rx.text(
                "参考情報です。各主張には出典を付しています。未検証の記述は参考意見です。 "
                "(Advisory only — every claim is shown with its source; unverified "
                "items are model commentary.)",
                style=TYPE["small"],
                color=COLORS["text_faint"],
            ),
            rx.foreach(SaiseiUIState.feasibility_rows, _note_card),
            spacing="3",
            width="100%",
        ),
    )
