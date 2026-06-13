"""HITL negotiation panel component.

Lists the proposed turnaround strategies and lets the banker approve a strategy,
request a revision (with a note), or reject. Visible only while the graph is
paused at the ``interrupt()``.

Also shows the creditor-meeting status (PART 3) and revision directive.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState

__all__ = ["negotiation_panel"]


def _strategy_card(strategy: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.heading(strategy["title"], size="3"),
            rx.text(strategy["rationale"], size="2"),
            rx.text("期待経常利益改善: " + strategy["uplift"] + " / 年", size="2", weight="bold"),
            rx.button(
                "承認 (Approve)",
                on_click=SaiseiUIState.approve(strategy["index"].to(int)),
                color_scheme="green",
            ),
            spacing="2",
            align="start",
        ),
        width="100%",
    )


def negotiation_panel() -> rx.Component:
    """Render the strategy negotiation panel (HITL)."""
    return rx.cond(
        SaiseiUIState.awaiting_decision,
        rx.vstack(
            rx.heading("戦略交渉 (Strategy Negotiation)", size="4"),
            # PART 3: Show creditor-meeting status.
            rx.cond(
                SaiseiUIState.negotiation_status != "pending",
                rx.vstack(
                    rx.text(
                        "債権者会議ステータス: " + SaiseiUIState.negotiation_status,
                        weight="bold",
                    ),
                    rx.cond(
                        SaiseiUIState.revision_directive != "",
                        rx.text(SaiseiUIState.revision_directive, size="2"),
                    ),
                    spacing="1",
                ),
            ),
            rx.foreach(SaiseiUIState.strategies, _strategy_card),
            rx.divider(),
            rx.input(
                placeholder="修正依頼メモ (revision note)",
                on_blur=SaiseiUIState.set_revision_note_buffer,
                id="revision_note",
            ),
            rx.hstack(
                rx.button(
                    "修正依頼 (Revise)",
                    on_click=SaiseiUIState.revise(SaiseiUIState.revision_note_buffer),
                    color_scheme="amber",
                ),
                rx.button("却下 (Reject)", on_click=SaiseiUIState.reject, color_scheme="red"),
                spacing="2",
            ),
            spacing="3",
            width="100%",
        ),
    )
