"""Feature 8.1 — the Portfolio watchlist (Altitude 1, ephemeral projection).

The book-level view the meta-interface spec reserves as Altitude 1: a ranked
watchlist of the borrowers assessed THIS SESSION, worst-first, so a banker sees
who is deteriorating / who just crossed a threshold at a glance and can drill
into any of them. It is a governance-light VIEW — it renders only
``SaiseiUIState.portfolio_view_rows`` (an in-session projection, nothing
persisted at rest) and the deterministic deterioration ranking + EWS sparklines
from the charts toolkit (its third consumer).

Pure presentation, display-only: it computes no figure and writes no store. The
per-row sparkline / trend / colour are all prepared in state; this component
only renders them. Drilling into a borrower sets the TDB code and leaves the
watchlist — it never auto-runs an assessment (the run stays an explicit human
action).
"""

from __future__ import annotations

from typing import Any

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, FONT, RADII, SHADOW, SPACE, TYPE

__all__ = ["portfolio_panel"]

#: Sparkline viewBox (matches the geometry constants in state).
_SPARK_W = 120
_SPARK_H = 28

_FOCUSABLE: dict[str, dict[str, str]] = {
    "&:focus-visible": {"boxShadow": "0 0 0 3px rgba(31,143,106,0.35)", "outline": "none"}
}


def _loan_status_color(status: rx.Var[str]) -> rx.Var[str]:
    """Map a loan-lifecycle status kanji to a Radix badge colour scheme.

    A small, legible lifecycle palette so the banker reads a facility's phase at
    a glance across the unified book: origination phases are neutral/blue, a
    live/healthy facility is green, distress is amber, and terminal-bad states
    (謝絶 declined / 償却 written off) are red. Display-only mapping; it decides
    nothing. Any unrecognised value falls back to a neutral gray.
    """
    return rx.match(
        status,
        ("申込", "gray"),  # Applied
        ("審査中", "blue"),  # Under review
        ("承認", "blue"),  # Approved
        ("実行", "teal"),  # Disbursed
        ("正常", "green"),  # Performing
        ("条件変更", "amber"),  # Restructured
        ("管理回収", "amber"),  # Workout
        ("完済", "green"),  # Closed (repaid)
        ("謝絶", "red"),  # Declined
        ("償却", "red"),  # Written off
        "gray",
    )


def _sparkline(row: rx.Var[dict[str, str]]) -> rx.Component:
    """A compact EWS trend sparkline; red when rising (deteriorating)."""
    rising = row["trend"] == "up"
    stroke = rx.cond(rising, COLORS["fail"], COLORS["positive"])
    return rx.el.svg(
        rx.el.svg.polyline(
            points=row["spark_points"],
            fill="none",
            stroke=stroke,
            stroke_width="2",
            stroke_linecap="round",
            stroke_linejoin="round",
        ),
        view_box=f"0 0 {_SPARK_W} {_SPARK_H}",
        width=f"{_SPARK_W}px",
        height=f"{_SPARK_H}px",
        custom_attrs={"preserveAspectRatio": "none", "aria-hidden": "true"},
        style={"display": "block"},
    )


def _row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """One watchlist borrower row: name, EWS, class, sparkline, drill-in."""
    crossed = row["crossed"] == "yes"
    return rx.table.row(
        rx.table.row_header_cell(
            rx.vstack(
                rx.text(
                    row["company_name"],
                    style=TYPE["small"],
                    color=COLORS["text"],
                    font_weight="600",
                ),
                rx.text(
                    "TDB " + row["tdb_code"],
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                spacing="0",
                align="start",
            ),
        ),
        rx.table.cell(
            rx.text(
                row["ews"],
                style={
                    "fontFamily": FONT["mono"],
                    "fontVariantNumeric": "tabular-nums",
                    "fontWeight": "700",
                },
                color=row["ews_color"],
            ),
        ),
        rx.table.cell(
            rx.badge(row["fsa_kanji"], variant="soft", color_scheme="gray", radius="full"),
        ),
        rx.table.cell(
            rx.cond(
                row["loan_status"] != "",
                rx.badge(
                    row["loan_status"],
                    variant="soft",
                    color_scheme=_loan_status_color(row["loan_status"]),
                    radius="full",
                ),
                rx.text("—", style=TYPE["caption"], color=COLORS["text_faint"]),
            ),
        ),
        rx.table.cell(_sparkline(row)),
        rx.table.cell(
            rx.text(
                row["updated_at"],
                style=TYPE["caption"],
                color=COLORS["text_faint"],
                no_of_lines=1,
            ),
        ),
        rx.table.cell(
            rx.cond(
                crossed,
                rx.badge(
                    rx.hstack(
                        rx.icon("trending-up", size=12),
                        rx.text("閾値超過"),
                        align="center",
                        spacing="1",
                    ),
                    color_scheme="red",
                    variant="soft",
                    radius="full",
                ),
            ),
        ),
        rx.table.cell(
            rx.button(
                rx.icon("arrow-right", size=14),
                "開く",
                on_click=SaiseiUIState.open_borrower_from_portfolio(row["tdb_code"]),
                variant="soft",
                color_scheme="gray",
                size="1",
                style=_FOCUSABLE,
            ),
        ),
    )


#: Accent token NAME -> COLORS key for the distribution segments. The toolkit
#: returns on-brand token names (not hex) so segments stay theme/dark-mode aware;
#: this resolves them to the design-system colours in one place.
_BAND_COLORS: dict[str, str] = {
    "positive": COLORS["positive"],
    "warn": COLORS["warn"],
    "chrome": COLORS["chrome"],
    "fail": COLORS["fail"],
}


def _band_color(accent: rx.Var[str]) -> rx.Var[str]:
    """Resolve a toolkit accent token name to its design-system colour."""
    return rx.match(
        accent,
        ("positive", _BAND_COLORS["positive"]),
        ("warn", _BAND_COLORS["warn"]),
        ("chrome", _BAND_COLORS["chrome"]),
        ("fail", _BAND_COLORS["fail"]),
        COLORS["border"],
    )


def _distribution_segment(band: rx.Var[dict[str, str]]) -> rx.Component:
    """One coloured segment of the stacked book-distribution bar.

    Width is the band's share of the book (``width_pct``); zero-count bands
    collapse to nothing. Carries an ``aria-label`` so the bar is legible to
    assistive tech (the bar is otherwise opaque).
    """
    return rx.box(
        width=band["width_pct"] + "%",
        height="100%",
        background=_band_color(band["accent"]),
        custom_attrs={"aria-label": band["label"] + ": " + band["count"]},
        title=band["label"] + " " + band["count"] + "社",
    )


def _distribution_legend_item(band: rx.Var[dict[str, str]]) -> rx.Component:
    """A legend chip: a colour dot + band label + borrower count."""
    return rx.hstack(
        rx.box(
            width="10px",
            height="10px",
            min_width="10px",
            border_radius=RADII["pill"],
            background=_band_color(band["accent"]),
        ),
        rx.text(band["label"], style=TYPE["caption"], color=COLORS["text_muted"]),
        rx.text(
            band["count"],
            style={
                "fontFamily": FONT["mono"],
                "fontVariantNumeric": "tabular-nums",
                "fontWeight": "700",
            },
            color=COLORS["text"],
        ),
        align="center",
        spacing="1",
    )


def _distribution_overview() -> rx.Component:
    """Book-level EWS-band distribution: a stacked bar + legend (Feature 9 §7).

    The Altitude-1 "where does the book sit?" overview — one glance shows how the
    assessed borrowers split across the four FSA health bands (正常 → 実質破綻).
    Display-only: it renders the deterministic ``portfolio_distribution`` tally;
    it bins nothing and computes no figure here.
    """
    return rx.vstack(
        rx.text(
            "権バンド分布 (Book by EWS band)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
        ),
        rx.box(
            rx.hstack(
                rx.foreach(SaiseiUIState.portfolio_distribution, _distribution_segment),
                spacing="0",
                width="100%",
                height="100%",
            ),
            role="img",
            aria_label="Portfolio distribution across FSA health bands",
            width="100%",
            height="14px",
            border_radius=RADII["pill"],
            overflow="hidden",
            background=COLORS["surface_2"],
            border=f"1px solid {COLORS['border']}",
        ),
        rx.flex(
            rx.foreach(SaiseiUIState.portfolio_distribution, _distribution_legend_item),
            gap="16px",
            wrap="wrap",
            width="100%",
        ),
        spacing="2",
        align="start",
        width="100%",
        padding=[SPACE["3"], SPACE["3"], SPACE["4"]],
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        box_shadow=SHADOW["sm"],
    )


def _credit_distribution_overview(
    title: str,
    aria: str,
    distribution: rx.Var[list[dict[str, str]]],
    count: rx.Var[Any],
) -> rx.Component:
    """A book-level origination credit-signal band bar (stacked bar + legend).

    The origination twin of ``_distribution_overview`` for the two ADVISORY
    credit lenses (返済余力 / 担保・保証): it reuses the SAME segment / legend
    helpers and chrome, only swapping the title and the distribution var, so the
    two lenses render byte-identically to the EWS band bar. Display-only: it
    renders a deterministic distribution tally and computes no figure.
    """
    return rx.vstack(
        rx.hstack(
            rx.text(title, style=TYPE["caption"], color=COLORS["text_faint"]),
            rx.spacer(),
            rx.text(
                count.to_string() + "社",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            align="center",
            width="100%",
        ),
        rx.box(
            rx.hstack(
                rx.foreach(distribution, _distribution_segment),
                spacing="0",
                width="100%",
                height="100%",
            ),
            role="img",
            aria_label=aria,
            width="100%",
            height="14px",
            border_radius=RADII["pill"],
            overflow="hidden",
            background=COLORS["surface_2"],
            border=f"1px solid {COLORS['border']}",
        ),
        rx.flex(
            rx.foreach(distribution, _distribution_legend_item),
            gap="16px",
            wrap="wrap",
            width="100%",
        ),
        spacing="2",
        align="start",
        width="100%",
    )


def _band_chip(label: rx.Var[str], accent: rx.Var[str]) -> rx.Component:
    """A small colour-coded band chip: a dot + the localized band label.

    Reuses ``_band_color`` so a row's capacity / coverage chip matches the
    accent of its segment in the distribution bar above. A missing band maps to
    a neutral em-dash label.
    """
    return rx.hstack(
        rx.box(
            width="8px",
            height="8px",
            min_width="8px",
            border_radius=RADII["pill"],
            background=_band_color(accent),
        ),
        rx.text(label, style=TYPE["small"], color=COLORS["text"]),
        align="center",
        spacing="2",
    )


def _book_row(row: rx.Var[dict[str, str]]) -> rx.Component:
    """One originated-facility row: name, recommendation, the two bands, drill-in.

    The origination twin of the watchlist ``_row``: it shows a facility taken to
    the 稟議 gate this session with its two ADVISORY credit-lens bands side by
    side, and drills back into the borrower (the SAME seam the watchlist uses).
    Display-only render of already-mapped strings.
    """
    return rx.table.row(
        rx.table.row_header_cell(
            rx.vstack(
                rx.text(
                    row["company"],
                    style=TYPE["small"],
                    color=COLORS["text"],
                    font_weight="600",
                ),
                rx.text(
                    "TDB " + row["tdb_code"],
                    style=TYPE["caption"],
                    color=COLORS["text_faint"],
                ),
                spacing="0",
                align="start",
            ),
        ),
        rx.table.cell(
            rx.badge(
                row["recommendation_label"],
                variant="soft",
                color_scheme=rx.match(
                    row["recommendation_accent"],
                    ("positive", "green"),
                    ("fail", "red"),
                    "gray",
                ),
                radius="full",
            ),
        ),
        rx.table.cell(_band_chip(row["capacity_label"], row["capacity_accent"])),
        rx.table.cell(_band_chip(row["coverage_label"], row["coverage_accent"])),
        rx.table.cell(
            rx.button(
                rx.icon("arrow-right", size=14),
                "開く",
                on_click=SaiseiUIState.open_borrower_from_portfolio(row["tdb_code"]),
                variant="soft",
                color_scheme="gray",
                size="1",
                style=_FOCUSABLE,
            ),
        ),
    )


def _book_table() -> rx.Component:
    """Per-facility table of this session's originated book (worst-first).

    The row-level companion to the two roll-up bars: the bars answer "how does
    the book split?", this answers "which facility sits where?". Renders
    ``origination_book_view_rows`` (already mapped + worst-first ordered);
    display-only.
    """
    return rx.box(
        rx.table.root(
            rx.table.header(
                rx.table.row(
                    rx.table.column_header_cell("実行先 (Facility)"),
                    rx.table.column_header_cell("稟議 (Decision)"),
                    rx.table.column_header_cell("返済余力 (Capacity)"),
                    rx.table.column_header_cell("担保・保証 (Coverage)"),
                    rx.table.column_header_cell(""),
                )
            ),
            rx.table.body(rx.foreach(SaiseiUIState.origination_book_view_rows, _book_row)),
            variant="surface",
            size="2",
            width="100%",
        ),
        width="100%",
        overflow_x="auto",
    )


def _origination_rollup() -> rx.Component:
    """Book-level roll-up of the two ADVISORY origination credit lenses.

    The 稟議-gate companion to the EWS band bar: once a facility is taken to the
    origination gate this session, this shows how the freshly-originated book
    splits across debt-service capacity (返済余力) and collateral/guarantee
    coverage (担保・保証). The two lenses are independent — an over-capacity
    facility can still be well covered — so both bars are shown. Display-only:
    it renders the deterministic ``origination_*_distribution`` tallies from the
    origination book; it bins nothing and computes no figure here.
    """
    return rx.vstack(
        rx.text(
            "今期実行分の与信シグナル (Originated book — credit signals)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
        ),
        _credit_distribution_overview(
            "返済余力分布 (Debt-service capacity)",
            "Originated book across debt-service-capacity bands",
            SaiseiUIState.origination_capacity_distribution,
            SaiseiUIState.origination_book_count,
        ),
        rx.divider(),
        _credit_distribution_overview(
            "担保・保証分布 (Collateral / guarantee coverage)",
            "Originated book across collateral/guarantee coverage bands",
            SaiseiUIState.origination_coverage_distribution,
            SaiseiUIState.origination_book_count,
        ),
        rx.divider(),
        _book_table(),
        spacing="3",
        align="start",
        width="100%",
        padding=[SPACE["3"], SPACE["3"], SPACE["4"]],
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["lg"],
        box_shadow=SHADOW["sm"],
    )


def _filter_control() -> rx.Component:
    """A segmented deterioration filter: all / crossed / distressed.

    Display-only: it only narrows which already-captured borrowers are shown
    (via ``portfolio_filter``); it never edits a figure or ordering rule.
    """
    return rx.hstack(
        rx.icon("list-filter", size=14, color=COLORS["text_faint"]),
        rx.segmented_control.root(
            rx.segmented_control.item("すべて (All)", value="all"),
            rx.segmented_control.item("閾値超過 (Crossed)", value="crossed"),
            rx.segmented_control.item("要注意以上 (Distressed)", value="distressed"),
            value=SaiseiUIState.portfolio_filter,
            on_change=SaiseiUIState.set_portfolio_filter,
            size="1",
        ),
        align="center",
        spacing="2",
    )


def _no_match_state() -> rx.Component:
    """Shown when the book has borrowers but the active filter matches none."""
    return rx.vstack(
        rx.icon("filter-x", size=24, color=COLORS["text_faint"]),
        rx.text(
            "このフィルターに一致する借り入れ先はありません。 (No borrowers match this filter.)",
            style=TYPE["small"],
            color=COLORS["text_muted"],
            text_align="center",
        ),
        spacing="2",
        align="center",
        width="100%",
        padding="32px 16px",
    )


def _empty_state() -> rx.Component:
    return rx.vstack(
        rx.icon("layers", size=28, color=COLORS["text_faint"]),
        rx.text(
            "このセッションで診断した借り入れ先がここに一覧表示されます。",
            style=TYPE["small"],
            color=COLORS["text_muted"],
            text_align="center",
        ),
        rx.text(
            "悪化順に並び、閾値を超えた先が上位に表示されます。診断を実行してください。 "
            "(Borrowers you assess this session appear here, worst-first. "
            "Nothing is stored at rest.)",
            style=TYPE["caption"],
            color=COLORS["text_faint"],
            text_align="center",
        ),
        spacing="2",
        align="center",
        width="100%",
        padding="48px 16px",
    )


def portfolio_panel() -> rx.Component:
    """Render the Altitude-1 Portfolio watchlist (ephemeral session projection)."""
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.button(
                    rx.icon("arrow-left", size=16),
                    "ワークスペースへ",
                    on_click=SaiseiUIState.close_portfolio,
                    variant="soft",
                    color_scheme="gray",
                    size="2",
                    style=_FOCUSABLE,
                ),
                rx.spacer(),
                rx.cond(
                    SaiseiUIState.portfolio_crossed_count > 0,
                    rx.badge(
                        SaiseiUIState.portfolio_crossed_count.to_string() + "件 閾値超過",
                        color_scheme="red",
                        variant="soft",
                        radius="full",
                        size="2",
                    ),
                ),
                rx.badge(
                    SaiseiUIState.portfolio_count.to_string() + "社",
                    color_scheme="gray",
                    variant="soft",
                    radius="full",
                    size="2",
                ),
                align="center",
                width="100%",
                spacing="2",
            ),
            rx.hstack(
                rx.icon("layers", size=18, color=COLORS["chrome"]),
                rx.heading(
                    "ポートフォリオ・ウォッチリスト (Portfolio watchlist)",
                    style=TYPE["h2"],
                    color=COLORS["text"],
                ),
                align="center",
                spacing="2",
            ),
            rx.text(
                "今セッションで診断した借り入れ先を悪化順に表示（永続保存なし）。 "
                "(This session's assessed borrowers, ranked by deterioration; "
                "nothing persisted at rest.)",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            rx.cond(
                SaiseiUIState.portfolio_count > 0,
                _distribution_overview(),
            ),
            rx.cond(
                SaiseiUIState.origination_book_count > 0,
                _origination_rollup(),
            ),
            rx.cond(
                SaiseiUIState.portfolio_count > 0,
                rx.hstack(
                    _filter_control(),
                    rx.spacer(),
                    rx.text(
                        SaiseiUIState.portfolio_filtered_count.to_string()
                        + " / "
                        + SaiseiUIState.portfolio_count.to_string()
                        + "社表示",
                        style=TYPE["caption"],
                        color=COLORS["text_faint"],
                    ),
                    align="center",
                    width="100%",
                ),
            ),
            rx.cond(
                SaiseiUIState.portfolio_count > 0,
                rx.cond(
                    SaiseiUIState.portfolio_filtered_count > 0,
                    rx.box(
                        rx.table.root(
                            rx.table.header(
                                rx.table.row(
                                    rx.table.column_header_cell("借り入れ先 (Borrower)"),
                                    rx.table.column_header_cell("EWS"),
                                    rx.table.column_header_cell("区分 (Class)"),
                                    rx.table.column_header_cell("状態 (Status)"),
                                    rx.table.column_header_cell("推移 (Trend)"),
                                    rx.table.column_header_cell("更新 (Updated)"),
                                    rx.table.column_header_cell(""),
                                    rx.table.column_header_cell(""),
                                )
                            ),
                            rx.table.body(rx.foreach(SaiseiUIState.portfolio_view_rows, _row)),
                            variant="surface",
                            size="2",
                            width="100%",
                        ),
                        width="100%",
                        overflow_x="auto",
                    ),
                    _no_match_state(),
                ),
                _empty_state(),
            ),
            spacing="4",
            width="100%",
            align="start",
            padding=[SPACE["4"], SPACE["5"], SPACE["6"]],
        ),
        background=COLORS["bg"],
        min_height="100vh",
        width="100%",
        max_width="100vw",
        overflow_x="hidden",
    )
