"""Case-file dashboard: the facts column of the meeting room.

Shows the company header and a grid of metric cards (FSA classification, EWS
score, working-capital gap, guarantee-release score, succession readiness),
plus the deterministic burden-sharing table once the meeting has consolidated.

Uses the product-grade :mod:`app.frontend.components.data_display` primitives so
every table reads as a financial instrument (tabular mono numerics, right-
aligned + sign-coloured money, refined headers) rather than a hand-made grid.
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
from app.frontend.theme import COLORS, FONT, RADII, SHADOW, TYPE

__all__ = ["ews_dashboard", "burden_table", "threshold_panel"]


def _metric(
    label: str,
    value: rx.Var[str] | str,
    *,
    accent: rx.Var[str] | str = COLORS["text"],
    icon: str = "circle",
) -> rx.Component:
    """Render a single metric card with a tabular-numeric value.

    The value uses a fluid ``clamp()`` size (applied uniformly to every metric
    card so the grid stays in visual harmony) and is allowed to shrink and wrap
    inside the card rather than overflow: a long money string such as
    ``-¥123,456,789`` (資金繰り) would otherwise burst the fixed-width card. The
    short values (EWS / score) read at the top of the clamp range, so the
    downsize is invisible for them and only the long figures relax.
    """
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.icon(icon, size=14, color=COLORS["text_faint"]),
                rx.text(label, style=TYPE["caption"], color=COLORS["text_faint"]),
                align="center",
                spacing="2",
            ),
            rx.heading(
                value,
                style={
                    # Fluid metric-value size: shrinks for long figures, caps at
                    # ~h2 for short ones. Uniform across all cards for harmony.
                    "fontSize": "clamp(18px, 2.2vw, 24px)",
                    "fontWeight": "700",
                    "lineHeight": "1.2",
                    "letterSpacing": "-0.01em",
                    "color": accent,
                    "fontFamily": FONT["mono"],
                    "fontVariantNumeric": "tabular-nums",
                    "fontFeatureSettings": "'tnum' 1",
                    # Contain long money strings inside the card.
                    "maxWidth": "100%",
                    "overflowWrap": "anywhere",
                    "wordBreak": "break-word",
                },
            ),
            spacing="3",
            align="start",
            width="100%",
            min_width="0",
        ),
        padding="18px 20px",
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        box_shadow=SHADOW["sm"],
        flex="1 1 180px",
        min_width=["150px", "180px"],
        # Allow the flex child to shrink below its content width so the value
        # wraps instead of forcing the card (and the row) to overflow.
        style={"minWidth": "0"},
        overflow="hidden",
    )


def _classification_accent() -> rx.Var[str]:
    """Color the classification card by severity (five FSA categories)."""
    return rx.match(
        SaiseiUIState.fsa_kanji,
        ("正常先", COLORS["pass"]),
        ("要注意先", COLORS["warn"]),
        ("破綻懸念先", COLORS["fail"]),
        ("実質破綻先", COLORS["fail"]),
        ("破綻先", COLORS["fail"]),
        COLORS["text_muted"],
    )


def _loan_status_accent() -> rx.Var[str]:
    """Color the loan-status card by lifecycle severity.

    正常 (Performing) reads healthy; 条件変更 (Restructured) is a caution; the
    distressed/terminal states (管理回収 Workout, 償却 Written off) read as a
    failure. Other lifecycle states (申込 / 審査中 / ...) stay neutral. Maps the
    already-derived kanji label; computes nothing.
    """
    return rx.match(
        SaiseiUIState.loan_status_kanji,
        ("正常", COLORS["pass"]),
        ("完済", COLORS["pass"]),
        ("条件変更", COLORS["warn"]),
        ("管理回収", COLORS["fail"]),
        ("償却", COLORS["fail"]),
        COLORS["text_muted"],
    )


def _ews_signal_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """Render one EWS signal's contribution as a labelled bar (points / weight).

    The bar width encodes how much of this signal's weight ceiling the borrower
    'used' (points / weight); wider + redder = more deterioration on this axis.
    Display-only: it visualises an already-computed deterministic contribution.
    """
    return rx.vstack(
        rx.hstack(
            rx.text(row["label"], style=TYPE["small"], color=COLORS["text"]),
            rx.spacer(),
            rx.text(
                row["points"] + " / " + row["weight"] + "点",
                style={
                    "fontFamily": FONT["mono"],
                    "fontSize": "12px",
                    "fontVariantNumeric": "tabular-nums",
                    "color": COLORS["text_muted"],
                },
            ),
            align="center",
            width="100%",
        ),
        rx.box(
            rx.box(
                width=row["fill_pct"] + "%",
                height="100%",
                background=COLORS["warn"],
                border_radius=RADII["pill"],
            ),
            width="100%",
            height="6px",
            background=COLORS["surface_3"],
            border_radius=RADII["pill"],
            overflow="hidden",
        ),
        spacing="1",
        width="100%",
    )


def ews_explain_panel() -> rx.Component:
    """Render the collapsible EWS score breakdown (Feature 7 explainability).

    Shows each weighted signal's contribution so the banker (or an examiner) can
    see exactly which deterioration drove the score, instead of an opaque number.
    The contributions sum to the EWS score by construction. Collapses to nothing
    until a breakdown exists, so pre-run / insufficient-history views are clean.
    """
    return rx.cond(
        SaiseiUIState.ews_breakdown_rows.length() > 0,
        rx.accordion.root(
            rx.accordion.item(
                header=rx.text(
                    "EWS内訳 (Score breakdown)",
                    style=TYPE["caption"],
                    color=COLORS["text_muted"],
                ),
                content=rx.vstack(
                    rx.foreach(SaiseiUIState.ews_breakdown_rows, _ews_signal_row),
                    spacing="3",
                    width="100%",
                    padding_top="8px",
                ),
            ),
            collapsible=True,
            type="single",
            variant="ghost",
            width="100%",
        ),
    )


def _hosho_pillar_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """Render one Hosho Kaijo pillar: met badge, contribution bar, directive.

    Higher score = condition closer to satisfied = greener bar. A met pillar
    shows a green check; an unmet one shows its actionable directive so the
    banker knows exactly what must change to release the guarantee.
    """
    met = row["met"] == "yes"
    return rx.vstack(
        rx.hstack(
            rx.icon(
                rx.cond(met, "circle-check", "circle-dot"),
                size=14,
                color=rx.cond(met, COLORS["pass"], COLORS["warn"]),
            ),
            rx.text(row["label"], style=TYPE["small"], color=COLORS["text"]),
            rx.spacer(),
            rx.text(
                row["score"] + " / " + row["weight"] + "点",
                style={
                    "fontFamily": FONT["mono"],
                    "fontSize": "12px",
                    "fontVariantNumeric": "tabular-nums",
                    "color": COLORS["text_muted"],
                },
            ),
            align="center",
            width="100%",
        ),
        rx.box(
            rx.box(
                width=row["fill_pct"] + "%",
                height="100%",
                background=rx.cond(met, COLORS["pass"], COLORS["warn"]),
                border_radius=RADII["pill"],
            ),
            width="100%",
            height="6px",
            background=COLORS["surface_3"],
            border_radius=RADII["pill"],
            overflow="hidden",
        ),
        rx.cond(
            ~met & (row["directive"] != ""),
            rx.text(
                row["directive"],
                style={"fontSize": "12px", "lineHeight": "1.5"},
                color=COLORS["text_muted"],
            ),
        ),
        spacing="1",
        width="100%",
    )


def hosho_explain_panel() -> rx.Component:
    """Render the collapsible guarantee-release (Hosho Kaijo) basis (Feature 7).

    The 保証解除 score is a weighted sum of three FSA-guideline pillars
    (法人個人分離 / 財務基盤 / 情報開示). Showing only the number is opaque; this
    exposes each pillar's contribution and, for unmet ones, the actionable
    directive — so the banker sees what the borrower must change to release the
    personal guarantee. Collapses to nothing until a breakdown exists.
    """
    return rx.cond(
        SaiseiUIState.hosho_pillar_rows.length() > 0,
        rx.accordion.root(
            rx.accordion.item(
                header=rx.text(
                    "保証解除の内訳 (Guarantee-release basis)",
                    style=TYPE["caption"],
                    color=COLORS["text_muted"],
                ),
                content=rx.vstack(
                    rx.foreach(SaiseiUIState.hosho_pillar_rows, _hosho_pillar_row),
                    spacing="4",
                    width="100%",
                    padding_top="8px",
                ),
            ),
            collapsible=True,
            type="single",
            variant="ghost",
            width="100%",
        ),
    )


def ews_dashboard() -> rx.Component:
    """Render the case-file header + metric grid."""
    return rx.vstack(
        rx.hstack(
            rx.icon("building-2", size=20, color=COLORS["chrome"]),
            rx.heading(
                rx.cond(SaiseiUIState.company_name != "", SaiseiUIState.company_name, "—"),
                size="6",
                color=COLORS["text"],
            ),
            rx.spacer(),
            # Feature 7: export the deterministic per-classification explainability
            # report as Word (.docx, the format banks and FSA examiners exchange).
            # Shown only once a borrower has been classified. Word is always
            # available; PDF is offered additionally when a CJK font is
            # configured (FSA examiners archive PDF).
            rx.cond(
                SaiseiUIState.has_explainability_report,
                rx.hstack(
                    rx.button(
                        rx.icon("file-down", size=14),
                        "説明レポート (Word)",
                        on_click=SaiseiUIState.download_explainability_docx,
                        color_scheme="gray",
                        variant="soft",
                        size="1",
                    ),
                    rx.cond(
                        SaiseiUIState.pdf_export_available,
                        rx.button(
                            rx.icon("file-down", size=14),
                            "説明レポート (PDF)",
                            on_click=SaiseiUIState.download_explainability_pdf,
                            color_scheme="gray",
                            variant="soft",
                            size="1",
                        ),
                    ),
                    spacing="2",
                    align="center",
                ),
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
                accent=SaiseiUIState.ews_accent,
                icon="activity",
            ),
            _metric(
                "資金繰り (Shikin Kuri)",
                SaiseiUIState.working_capital_gap_display,
                icon="banknote",
            ),
            # Loan-lifecycle: shown only when a facility is attached to the run.
            # Surfaces the current loan status (e.g. 管理回収 / Workout) and the
            # deterministic loan-loss provision (貸倒引当金) the spine persists --
            # including on the terminal workout path, which has no HITL payload.
            rx.cond(
                SaiseiUIState.loan_status_kanji != "",
                _metric(
                    "融資ステータス (Loan)",
                    SaiseiUIState.loan_status_kanji,
                    accent=_loan_status_accent(),
                    icon="landmark",
                ),
            ),
            rx.cond(
                (SaiseiUIState.loan_status_kanji != "")
                & (SaiseiUIState.loan_provision_display != "\u2014"),
                _metric(
                    "貸倒引当金 (Provision)",
                    SaiseiUIState.loan_provision_display,
                    icon="shield-alert",
                ),
            ),
            _metric(
                "保証解除 (Hosho Kaijo)",
                SaiseiUIState.hosho_kaijo_score.to_string(),
                accent=SaiseiUIState.hosho_accent,
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
            gap="16px",
            wrap="wrap",
            width="100%",
        ),
        rx.cond(
            SaiseiUIState.classification_reason != "",
            rx.hstack(
                rx.icon("info", size=13, color=COLORS["text_faint"]),
                rx.text(
                    "区分根拠: " + SaiseiUIState.classification_reason,
                    style=TYPE["small"],
                    color=COLORS["text_muted"],
                ),
                align="center",
                spacing="2",
                width="100%",
            ),
        ),
        ews_explain_panel(),
        hosho_explain_panel(),
        rx.cond(
            SaiseiUIState.error != "",
            rx.callout(
                SaiseiUIState.error,
                color_scheme="red",
                icon="triangle_alert",
                width="100%",
            ),
        ),
        spacing="5",
        width="100%",
    )


def _burden_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.table.row(
        rx.table.row_header_cell(row["persona"]),
        num_cell(row["share"]),
        num_cell(row["grace"]),
        num_cell(row["haircut"]),
        money_cell(row["new_money"], color_by_sign=False),
        rx.table.cell(rx.badge(row["allocation"], variant="soft", color_scheme="gray")),
    )


def burden_table() -> rx.Component:
    """Render the deterministic per-lender burden-sharing table."""
    return rx.cond(
        SaiseiUIState.burden_rows.length() > 0,
        rx.vstack(
            rx.hstack(
                section_title("split", "負担分担表 (Burden-Sharing)"),
                rx.spacer(),
                rx.cond(
                    SaiseiUIState.burden_share_basis != "",
                    rx.badge(
                        rx.match(
                            SaiseiUIState.burden_share_basis,
                            ("stake_based", "実残高ベース (stake-based)"),
                            ("heuristic_proxy", "推定ベース (proxy)"),
                            "—",
                        ),
                        variant="soft",
                        color_scheme=rx.cond(
                            SaiseiUIState.burden_share_basis == "stake_based",
                            "grass",
                            "amber",
                        ),
                        radius="full",
                        size="1",
                    ),
                ),
                align="center",
                width="100%",
            ),
            data_table(
                [
                    "貸出人",
                    "負担比率",
                    "猶予",
                    "ヘアカット",
                    "新規融資",
                    "配分方式",
                ],
                SaiseiUIState.burden_rows,
                _burden_row,
                size="1",
            ),
            spacing="2",
            width="100%",
        ),
    )


def _calibration_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """Render one per-band-distance calibration row, highlighting the rec."""
    return rx.table.row(
        rx.table.row_header_cell(row["band_distance"]),
        num_cell(row["total"]),
        num_cell(row["labelled"]),
        num_cell(row["precision"]),
        rx.table.cell(
            rx.cond(
                row["recommended"] == "yes",
                rx.badge("recommended", variant="soft", color_scheme="grass"),
                rx.cond(
                    row["meets_target"] == "yes",
                    rx.badge("meets", variant="soft", color_scheme="gray"),
                    rx.text("—", color=COLORS["text_faint"]),
                ),
            )
        ),
    )


def threshold_panel() -> rx.Component:
    """Render the advisory reconciliation-threshold calibration panel.

    Display-only: it reads the backend analysis of the captured
    ``reconciliation_outcomes`` corpus and renders it, exactly like the
    burden-sharing table. It never edits ``RECONCILIATION_BAND_DISTANCE``.
    Renders only when captured outcomes produced calibration rows, so existing
    runs (no reconciliation) look unchanged.
    """
    return rx.cond(
        SaiseiUIState.calibration_rows.length() > 0,
        rx.vstack(
            section_title("sliders-horizontal", "閾値校正 (Threshold Calibration)"),
            data_table(
                [
                    "乖離 (dist)",
                    "件数",
                    "判定済",
                    "精度",
                    "推奨",
                ],
                SaiseiUIState.calibration_rows,
                _calibration_row,
                size="1",
            ),
            rx.cond(
                SaiseiUIState.calibration_recommendation != "",
                rx.callout(
                    "推奨値 (recommended RECONCILIATION_BAND_DISTANCE): "
                    + SaiseiUIState.calibration_recommendation,
                    icon="lightbulb",
                    color_scheme="grass",
                    width="100%",
                ),
                rx.callout(
                    "十分な証拠がないため、現在の値を維持します。 "
                    "(Insufficient evidence; keep the current threshold.)",
                    icon="info",
                    color_scheme="gray",
                    width="100%",
                ),
            ),
            rx.text(
                SaiseiUIState.calibration_rationale,
                style=TYPE["small"],
                color=COLORS["text_muted"],
            ),
            spacing="2",
            width="100%",
        ),
    )
