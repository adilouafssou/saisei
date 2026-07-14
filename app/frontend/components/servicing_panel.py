"""貸出管理 (servicing) entry — advance a facility along its performing arc.

The servicing counterpart to the origination entry: a banker enters (or
inherits) a facility id and records a deterministic, NON-DISTRESS lifecycle
transition along the performing arc — 実行→正常 (confirm normal servicing) or
正常→完済 (record full repayment). Unlike origination and turnaround there is
NO banker-decision interrupt here: a servicing transition is an operational fact
(the facility entered normal servicing; the facility was fully repaid), so the
action itself IS the record. Every credit / distress move (条件変更 / 管理回収 /
償却) stays HITL-gated elsewhere; nothing here can reach those.

Pure presentation, display-only: every value shown is read from
``SaiseiUIState`` (the graph + checkpointer machinery lives in the state
handlers). This component only renders the form / outcome and transports the
banker's click.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, RADII, SPACE, TYPE

__all__ = ["servicing_dialog", "servicing_trigger_button"]

_FOCUSABLE: dict[str, dict[str, str]] = {
    "&:focus-visible": {"boxShadow": "0 0 0 3px rgba(31,143,106,0.35)", "outline": "none"}
}


def servicing_trigger_button() -> rx.Component:
    """The top-bar button that opens the 貸出管理 (servicing) dialog."""
    return rx.button(
        rx.icon("banknote", size=16),
        "貸出管理",
        on_click=SaiseiUIState.open_servicing,
        variant="soft",
        color_scheme="gray",
        size="2",
        style=_FOCUSABLE,
    )


def _action_bar() -> rx.Component:
    """The servicing-action controls (operational, non-gated).

    正常認定 (confirm 実行→正常), a 一部入金 (partial-repayment) amount field +
    button, and 完済 (full payoff). Repayments draw down the on-screen facility's
    principal balance; the banker is recording an operational fact, not a credit
    decision.
    """
    return rx.vstack(
        rx.button(
            rx.icon("circle-check", size=16),
            "正常認定 (Confirm performing)",
            on_click=SaiseiUIState.service_facility("confirm"),
            disabled=SaiseiUIState.servicing_running | ~SaiseiUIState.servicing_loan_id_valid,
            color_scheme="grass",
            size="2",
            width="100%",
            style=_FOCUSABLE,
        ),
        rx.hstack(
            rx.input(
                value=SaiseiUIState.servicing_amount_input,
                on_change=SaiseiUIState.set_servicing_amount,
                placeholder="一部入金額 (partial repayment, yen)",
                size="2",
                width="100%",
                style=_FOCUSABLE,
            ),
            rx.button(
                rx.icon("banknote-arrow-down", size=16),
                "一部入金",
                on_click=SaiseiUIState.service_facility("repay_amount"),
                disabled=SaiseiUIState.servicing_running
                | ~SaiseiUIState.servicing_loan_id_valid
                | ~SaiseiUIState.servicing_amount_valid,
                color_scheme="teal",
                variant="soft",
                size="2",
                white_space="nowrap",
                style=_FOCUSABLE,
            ),
            spacing="2",
            width="100%",
        ),
        rx.button(
            rx.icon("badge-check", size=16),
            "完済 (Mark fully repaid)",
            on_click=SaiseiUIState.service_facility("repay"),
            disabled=SaiseiUIState.servicing_running | ~SaiseiUIState.servicing_loan_id_valid,
            color_scheme="teal",
            variant="soft",
            size="2",
            width="100%",
            style=_FOCUSABLE,
        ),
        spacing="3",
        width="100%",
    )


def _outcome_card() -> rx.Component:
    """The terminal record after a servicing action (正常 / 完済)."""
    closed = SaiseiUIState.servicing_loan_status == "完済"
    return rx.vstack(
        rx.hstack(
            rx.icon(
                rx.cond(closed, "badge-check", "circle-check"),
                size=18,
                color=rx.cond(closed, COLORS["positive"], COLORS["chrome"]),
            ),
            rx.text(
                rx.cond(
                    closed,
                    "完済 (Repaid & closed)",
                    "記録しました (Recorded)",
                ),
                style=TYPE["small"],
                color=COLORS["text"],
                font_weight="700",
            ),
            rx.spacer(),
            rx.cond(
                SaiseiUIState.servicing_loan_status != "",
                rx.badge(
                    SaiseiUIState.servicing_loan_status,
                    color_scheme=rx.cond(closed, "green", "teal"),
                    variant="soft",
                    radius="full",
                ),
            ),
            align="center",
            width="100%",
        ),
        rx.text(
            rx.cond(
                closed,
                "ファシリティを完済として記録しました。 (Facility recorded as fully repaid.)",
                "運用状態を記録しました（正常稼働 / 一部入金）。 "
                "(Servicing state recorded — performing / partial repayment.)",
            ),
            style=TYPE["caption"],
            color=COLORS["text_muted"],
        ),
        rx.text(
            "これは適定な運用記録です（信用・廃側判断ではありません）。 "
            "(An operational record — not a credit / workout decision.)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
        ),
        spacing="2",
        align="start",
        width="100%",
        padding=SPACE["3"],
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
    )


def _body() -> rx.Component:
    """The dialog body, switching on the servicing phase."""
    return rx.match(
        SaiseiUIState.servicing_phase,
        ("done", _outcome_card()),
        (
            "error",
            rx.callout(
                SaiseiUIState.servicing_error,
                icon="triangle-alert",
                color_scheme="red",
                variant="soft",
            ),
        ),
        # idle (default): the facility-id entry form + action bar.
        rx.cond(
            SaiseiUIState.servicing_running,
            rx.center(rx.spinner(size="3"), padding="32px"),
            rx.vstack(
                rx.text(
                    "ファシリティの運用状態を記録します。対象のローンIDを入力してください。 "
                    "(Record a facility's servicing state — enter its loan id.)",
                    style=TYPE["small"],
                    color=COLORS["text_muted"],
                ),
                rx.input(
                    value=SaiseiUIState.servicing_loan_id,
                    on_change=SaiseiUIState.set_servicing_loan_id,
                    placeholder="ローンID (e.g. L-1234567890123)",
                    size="3",
                    width="100%",
                    style=_FOCUSABLE,
                ),
                _action_bar(),
                spacing="3",
                width="100%",
            ),
        ),
    )


def servicing_dialog() -> rx.Component:
    """The 貸出管理 entry dialog (opened from the top-bar trigger)."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.hstack(
                    rx.icon("banknote", size=18, color=COLORS["chrome"]),
                    rx.dialog.title(
                        "貸出管理 (Loan servicing)",
                        style=TYPE["h2"],
                        margin="0",
                    ),
                    align="center",
                    width="100%",
                ),
                _body(),
                rx.hstack(
                    rx.spacer(),
                    rx.dialog.close(
                        rx.button(
                            "閉じる (Close)",
                            on_click=SaiseiUIState.close_servicing,
                            variant="soft",
                            color_scheme="gray",
                            size="2",
                            style=_FOCUSABLE,
                        ),
                    ),
                    width="100%",
                ),
                spacing="4",
                width="100%",
                align="start",
            ),
            style={"maxWidth": "460px"},
        ),
        open=SaiseiUIState.show_servicing,
    )
