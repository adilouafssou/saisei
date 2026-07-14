"""融資組成 (origination) entry — start a NEW facility from the UI.

The origination counterpart to the assessment entry: a banker enters an
applicant's TDB code, the deterministic origination graph runs to the 稟議
credit-decision pause, and the grounded, advisory recommendation (承認 / 謝絶
+ a provisional 融資上限) is surfaced for the banker to act on. The banker — not
the model — decides: 承認 drives APPROVED → DISBURSED (実行); 謝絶 records DECLINED.

Pure presentation, display-only: every value shown is read from
``SaiseiUIState`` (the graph + checkpointer machinery lives in the state
handlers). Driving the graph, the recommendation, and the HITL-gated decision
all happen in the backend; this component only renders them and transports the
banker's click.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, FONT, RADII, SPACE, TYPE

__all__ = ["origination_dialog", "origination_trigger_button"]

_FOCUSABLE: dict[str, dict[str, str]] = rx.Var.create(
    {"&:focus-visible": {"boxShadow": "0 0 0 3px rgba(31,143,106,0.35)", "outline": "none"}}
)


def origination_trigger_button() -> rx.Component:
    """The top-bar button that opens the 融資組成 (new facility) dialog."""
    return rx.button(
        rx.icon("file-plus-2", size=16),
        "融資組成",
        on_click=SaiseiUIState.open_origination,
        variant="soft",
        color_scheme="gray",
        size="2",
        style=_FOCUSABLE,
    )


def _recommendation_card() -> rx.Component:
    """The advisory recommendation surfaced at the 稟議 pause (display-only)."""
    approve = SaiseiUIState.origination_recommendation == "approve"
    return rx.vstack(
        rx.hstack(
            rx.text(
                "推奨 (Recommendation)",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            rx.spacer(),
            rx.cond(
                SaiseiUIState.origination_grounded == "yes",
                rx.badge(
                    "根拠あり (grounded)",
                    color_scheme="green",
                    variant="soft",
                    radius="full",
                ),
                rx.badge(
                    "未検証 (unverified)",
                    color_scheme="amber",
                    variant="soft",
                    radius="full",
                ),
            ),
            align="center",
            width="100%",
        ),
        rx.badge(
            rx.cond(approve, "承認推奨 (Approve)", "謝絶推奨 (Decline)"),
            color_scheme=rx.cond(approve, "green", "red"),
            variant="soft",
            size="2",
            radius="full",
        ),
        rx.hstack(
            rx.text(
                "融資上限 (Facility ceiling)",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            rx.spacer(),
            rx.text(
                SaiseiUIState.origination_max_facility,
                style={
                    "fontFamily": FONT["mono"],
                    "fontVariantNumeric": "tabular-nums",
                    "fontWeight": "700",
                },
                color=COLORS["text"],
            ),
            align="center",
            width="100%",
        ),
        rx.text(
            SaiseiUIState.origination_reason,
            style=TYPE["small"],
            color=COLORS["text_muted"],
        ),
        _debt_capacity_block(),
        _coverage_block(),
        rx.text(
            "これは助言です。最終的な信用判断は担当者にあります。 "
            "(Advisory only — the credit decision is yours.)",
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


#: Colour scheme + bilingual label per debt-service-capacity band (!1). Mirrors
#: the grounded/unverified badge idiom: green within capacity, amber a stretch,
#: red over capacity. Display-only — the band feeds no gate, route, or decision.
_CAPACITY_SCHEME: dict[str, str] = rx.Var.create(
    {
        "within_capacity": "green",
        "stretch": "amber",
        "over_capacity": "red",
    }
)
_CAPACITY_LABEL: dict[str, str] = rx.Var.create(
    {
        "within_capacity": "返済余力内 (within capacity)",
        "stretch": "余力上限 (stretch)",
        "over_capacity": "余力超過 (over capacity)",
    }
)


def _debt_capacity_block() -> rx.Component:
    """The advisory debt-service-capacity annotation on the facility ceiling.

    Surfaces the deterministic check (!1) that tests the size-anchored 融資上限
    against the firm's demonstrated 経常利益: a colour-coded band chip plus the
    implied annual debt service vs the prudent service ceiling, and a bilingual
    reason. Renders nothing when no band is present (e.g. a DECLINE with a 0
    ceiling). Display-only — it computes no figure and decides nothing.
    """
    return rx.cond(
        SaiseiUIState.origination_capacity_band != "",
        rx.vstack(
            rx.hstack(
                rx.text(
                    "返済余力 (Debt-service capacity)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                rx.spacer(),
                rx.badge(
                    _CAPACITY_LABEL[SaiseiUIState.origination_capacity_band],
                    color_scheme=_CAPACITY_SCHEME[SaiseiUIState.origination_capacity_band],
                    variant="soft",
                    radius="full",
                ),
                align="center",
                width="100%",
            ),
            rx.hstack(
                rx.text(
                    "想定年間返済額 (Annual debt service)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                rx.spacer(),
                rx.text(
                    SaiseiUIState.origination_capacity_debt_service,
                    style={
                        "fontFamily": FONT["mono"],
                        "fontVariantNumeric": "tabular-nums",
                    },
                    color=COLORS["text"],
                ),
                align="center",
                width="100%",
            ),
            rx.hstack(
                rx.text(
                    "健全返済余力 (Prudent ceiling)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                rx.spacer(),
                rx.text(
                    SaiseiUIState.origination_capacity_ceiling,
                    style={
                        "fontFamily": FONT["mono"],
                        "fontVariantNumeric": "tabular-nums",
                    },
                    color=COLORS["text"],
                ),
                align="center",
                width="100%",
            ),
            rx.text(
                SaiseiUIState.origination_capacity_reason,
                style=TYPE["caption"],
                color=COLORS["text_muted"],
            ),
            spacing="2",
            align="start",
            width="100%",
            padding=SPACE["2"],
            background=COLORS["surface_2"],
            border=f"1px solid {COLORS['border']}",
            border_radius=RADII["md"],
        ),
    )


#: Colour scheme + bilingual label per collateral/guarantee coverage band
#: (breadth #6). Mirrors the capacity-band idiom: green well covered, amber a
#: partial cushion, red materially unsecured. Display-only — the band feeds no
#: gate, route, or decision.
_COVERAGE_SCHEME: dict[str, str] = rx.Var.create(
    {
        "well_covered": "green",
        "partial": "amber",
        "uncovered": "red",
    }
)
_COVERAGE_LABEL: dict[str, str] = rx.Var.create(
    {
        "well_covered": "保全十分 (well covered)",
        "partial": "一部保全 (partial)",
        "uncovered": "保全不足 (uncovered)",
    }
)


def _coverage_block() -> rx.Component:
    """The advisory collateral/guarantee coverage annotation on the facility.

    The balance-sheet twin of the debt-capacity block: it tests the secured +
    guaranteed value (担保・保証) against the proposed 融資上限, surfacing a
    colour-coded band chip plus the covered amount and the uncovered (clean-risk)
    tail, and a bilingual reason. Renders nothing when no band is present (e.g. a
    DECLINE with a 0 ceiling). Display-only — it computes no figure and decides
    nothing.
    """
    return rx.cond(
        SaiseiUIState.origination_coverage_band != "",
        rx.vstack(
            rx.hstack(
                rx.text(
                    "担保・保証カバー (Collateral / guarantee coverage)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                rx.spacer(),
                rx.badge(
                    _COVERAGE_LABEL[SaiseiUIState.origination_coverage_band],
                    color_scheme=_COVERAGE_SCHEME[SaiseiUIState.origination_coverage_band],
                    variant="soft",
                    radius="full",
                ),
                align="center",
                width="100%",
            ),
            rx.hstack(
                rx.text(
                    "カバー額 (Covered amount)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                rx.spacer(),
                rx.text(
                    SaiseiUIState.origination_coverage_covered,
                    style={
                        "fontFamily": FONT["mono"],
                        "fontVariantNumeric": "tabular-nums",
                    },
                    color=COLORS["text"],
                ),
                align="center",
                width="100%",
            ),
            rx.hstack(
                rx.text(
                    "無担保部分 (Uncovered)",
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                rx.spacer(),
                rx.text(
                    SaiseiUIState.origination_coverage_uncovered,
                    style={
                        "fontFamily": FONT["mono"],
                        "fontVariantNumeric": "tabular-nums",
                    },
                    color=COLORS["text"],
                ),
                align="center",
                width="100%",
            ),
            rx.text(
                SaiseiUIState.origination_coverage_reason,
                style=TYPE["caption"],
                color=COLORS["text_muted"],
            ),
            spacing="2",
            align="start",
            width="100%",
            padding=SPACE["2"],
            background=COLORS["surface_2"],
            border=f"1px solid {COLORS['border']}",
            border_radius=RADII["md"],
        ),
    )


def _decision_bar() -> rx.Component:
    """The 承認 / 謝絶 credit-decision buttons (the banker decides)."""
    return rx.hstack(
        rx.button(
            rx.icon("circle-check", size=16),
            "承認 (Approve)",
            on_click=SaiseiUIState.decide_origination("approve"),
            disabled=SaiseiUIState.origination_running,
            color_scheme="grass",
            size="2",
            style=_FOCUSABLE,
        ),
        rx.button(
            rx.icon("circle-x", size=16),
            "謝絶 (Decline)",
            on_click=SaiseiUIState.decide_origination("decline"),
            disabled=SaiseiUIState.origination_running,
            color_scheme="red",
            variant="soft",
            size="2",
            style=_FOCUSABLE,
        ),
        spacing="3",
        width="100%",
    )


def _outcome_card() -> rx.Component:
    """The terminal record after the banker decides (実行 / 謝絶)."""
    disbursed = SaiseiUIState.origination_phase == "approved"
    return rx.vstack(
        rx.hstack(
            rx.icon(
                rx.cond(disbursed, "badge-check", "ban"),
                size=18,
                color=rx.cond(disbursed, COLORS["positive"], COLORS["fail"]),
            ),
            rx.text(
                rx.cond(
                    disbursed,
                    "承認・実行 (Approved & disbursed)",
                    "謝絶 (Declined)",
                ),
                style=TYPE["small"],
                color=COLORS["text"],
                font_weight="700",
            ),
            rx.spacer(),
            rx.cond(
                SaiseiUIState.origination_loan_status != "",
                rx.badge(
                    SaiseiUIState.origination_loan_status,
                    color_scheme=rx.cond(disbursed, "teal", "red"),
                    variant="soft",
                    radius="full",
                ),
            ),
            align="center",
            width="100%",
        ),
        rx.text(
            rx.cond(
                disbursed,
                "新規ファシリティを実行しました。ウォッチリストに表示されます。 "
                "(Facility disbursed; it now appears in the watchlist.)",
                "本件は謝絶として記録されました。 (Recorded as declined.)",
            ),
            style=TYPE["caption"],
            color=COLORS["text_muted"],
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
    """The dialog body, switching on the origination phase."""
    return rx.match(
        SaiseiUIState.origination_phase,
        (
            "reviewing",
            rx.cond(
                SaiseiUIState.origination_running,
                rx.center(rx.spinner(size="3"), padding="32px"),
                rx.vstack(_recommendation_card(), _decision_bar(), spacing="4", width="100%"),
            ),
        ),
        ("approved", _outcome_card()),
        ("declined", _outcome_card()),
        (
            "error",
            rx.callout(
                SaiseiUIState.origination_error,
                icon="triangle-alert",
                color_scheme="red",
                variant="soft",
            ),
        ),
        # idle (default): the applicant-code entry form.
        rx.vstack(
            rx.text(
                "新規融資の申込を開始します。申込企業のTDBコードを入力してください。 "
                "(Start a new facility application — enter the applicant's TDB code.)",
                style=TYPE["small"],
                color=COLORS["text_muted"],
            ),
            rx.input(
                value=SaiseiUIState.origination_code,
                on_change=SaiseiUIState.set_origination_code,
                placeholder="TDB企業コード (7 digits)",
                max_length=7,
                size="3",
                width="100%",
                style=_FOCUSABLE,
            ),
            rx.text(
                "担保・保証（任意・助言用） (Collateral / guarantee — optional, advisory)",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            rx.hstack(
                rx.input(
                    value=SaiseiUIState.origination_collateral_input,
                    on_change=SaiseiUIState.set_origination_collateral,
                    placeholder="担保評価額 (collateral, 円)",
                    size="2",
                    width="100%",
                    style=_FOCUSABLE,
                ),
                rx.input(
                    value=SaiseiUIState.origination_guarantee_input,
                    on_change=SaiseiUIState.set_origination_guarantee,
                    placeholder="保証カバー額 (guarantee, 円)",
                    size="2",
                    width="100%",
                    style=_FOCUSABLE,
                ),
                spacing="2",
                width="100%",
            ),
            rx.button(
                rx.cond(
                    SaiseiUIState.origination_running,
                    rx.spinner(size="2"),
                    rx.icon("play", size=16),
                ),
                "稟議へ (To credit review)",
                on_click=SaiseiUIState.start_origination,
                disabled=SaiseiUIState.origination_running | ~SaiseiUIState.origination_code_valid,
                color_scheme="grass",
                size="3",
                width="100%",
                style=_FOCUSABLE,
            ),
            spacing="3",
            width="100%",
        ),
    )


def origination_dialog() -> rx.Component:
    """The 融資組成 entry dialog (opened from the top-bar trigger)."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.hstack(
                    rx.icon("file-plus-2", size=18, color=COLORS["chrome"]),
                    rx.dialog.title(
                        "融資組成 (New facility origination)",
                        style=TYPE["h2"],
                        margin="0",
                    ),
                    rx.spacer(),
                    rx.cond(
                        SaiseiUIState.origination_company != "",
                        rx.badge(
                            SaiseiUIState.origination_company,
                            color_scheme="gray",
                            variant="soft",
                            radius="full",
                        ),
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
                            on_click=SaiseiUIState.close_origination,
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
        open=SaiseiUIState.show_origination,
    )
