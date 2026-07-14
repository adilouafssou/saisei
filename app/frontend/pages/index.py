"""Saisei main page — the creditor-meeting rehearsal room.

A production-grade borrower workspace organised by ALTITUDE-2 TABS
(Feature 9 — the meta-interface) instead of one infinite scroll:
- A sticky top bar with the brand, the TDB-code input, and the run button.
- A phase stepper showing the run lifecycle.
- A borrower workspace split into four tabs:
    • Assessment (診断) — demo picker, EWS dashboard, threshold panel, Shisanhyo
      table + upload, feasibility notes.
    • Meeting (会議) — the live creditor-meeting transcript + inline HITL bar,
      and the lead-arranger burden-sharing table.
    • Plan (計画) — the recovery chart and the Keikakusho draft + PDF/Word/Excel
      exports (this panel stays mounted so the print region is always reachable).
    • Audit (監査) — the Feature 7 immutable audit trail for this thread.

The tabs are pure presentation: every value shown is still read display-only
from ``SaiseiUIState`` (no backend / graph / state-schema change). The active
tab follows ``SaiseiUIState.effective_tab`` (banker's pick, else phase-implied).

This module is the canonical location under ``app.frontend.pages.index``.
The legacy path ``saisei_ui.saisei_ui`` re-exports from here.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.components.audit_panel import audit_panel, loan_ledger_panel
from app.frontend.components.ews_dashboard import (
    burden_table,
    ews_dashboard,
    threshold_panel,
)
from app.frontend.components.feasibility_panel import feasibility_panel
from app.frontend.components.meeting_panel import meeting_panel
from app.frontend.components.origination_panel import (
    origination_dialog,
    origination_trigger_button,
)
from app.frontend.components.pnl_bridge import pnl_bridge
from app.frontend.components.portfolio_panel import portfolio_panel
from app.frontend.components.recovery_chart import recovery_chart
from app.frontend.components.saisei_companion import saisei_companion
from app.frontend.components.servicing_panel import (
    servicing_dialog,
    servicing_trigger_button,
)
from app.frontend.components.shisanhyo_table import shisanhyo_table
from app.frontend.components.shisanhyo_upload import shisanhyo_upload_dialog
from app.frontend.components.workspace_tabs import workspace_tabs
from app.frontend.state import SaiseiUIState
from app.frontend.theme import (
    COLORS,
    FOCUS_RING,
    FONT,
    GRADIENT,
    RADII,
    SHADOW,
    SPACE,
    THEME_CSS,
    TYPE,
)

__all__ = ["index"]

#: Shared focus-visible style for interactive controls (keyboard a11y / WCAG).
_FOCUSABLE: dict[str, dict[str, str]] = {
    "&:focus-visible": {"boxShadow": FOCUS_RING, "outline": "none"}
}

#: Bundled demo companies, surfaced as a discoverable picker next to the case
#: file. Each entry is (TDB code, short scenario label) and maps 1:1 to a
#: bundled fixture wired into the MockDataProvider index maps
#: (app/backend/tools/tdb_api.py + core_banking.py). Display-only: clicking a
#: chip just sets ``tdb_code`` via ``set_tdb_code`` — the banker still presses
#: 診断実行 to run. Kept here (not in state) because it is static demo metadata.
_DEMO_COMPANIES: list[tuple[str, str]] = [
    ("1234567", "製造業 / 要注意 (distressed)"),
    ("2000001", "正常先 (Normal)"),
    ("3000001", "要注意先 (Needs Attention)"),
    ("4000001", "破綻懸念先 (In Danger)"),
    ("5000001", "要管理先・資金繰り (WC deficit / HITL)"),
    ("6000001", "卸売業・薄利 (thin margin / over-capacity)"),
]


def _demo_picker() -> rx.Component:
    """Render a compact, discoverable picker of the bundled demo companies.

    Display-only: each chip sets ``tdb_code`` (the banker then runs the
    assessment). Makes the multiple bundled scenarios discoverable instead of
    leaving the single default code as the only obvious input.
    """
    return rx.vstack(
        rx.hstack(
            rx.icon("flask-conical", size=14, color=COLORS["text_faint"]),
            rx.text(
                "デモ企業 (Demo companies)",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            align="center",
            spacing="2",
        ),
        rx.flex(
            *[
                rx.button(
                    rx.text(label),
                    on_click=SaiseiUIState.set_tdb_code(code),
                    variant=rx.cond(SaiseiUIState.tdb_code == code, "solid", "soft"),
                    color_scheme=rx.cond(SaiseiUIState.tdb_code == code, "grass", "gray"),
                    size="1",
                    radius="full",
                    style=_FOCUSABLE,
                    disabled=SaiseiUIState.is_running,
                )
                for code, label in _DEMO_COMPANIES
            ],
            gap="8px",
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


def _brand() -> rx.Component:
    return rx.hstack(
        rx.box(
            rx.center(
                rx.text(
                    "再",
                    style={"fontSize": "22px", "fontWeight": "800", "color": "#ffffff"},
                ),
                width="100%",
                height="100%",
            ),
            width="40px",
            height="40px",
            background=GRADIENT["brand"],
            border_radius=RADII["md"],
            box_shadow=SHADOW["glow"],
        ),
        rx.vstack(
            rx.heading("Saisei 再生", style=TYPE["h2"], color=COLORS["text"]),
            rx.text(
                "経営改善プラットフォーム",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            spacing="0",
            align="start",
        ),
        align="center",
        spacing="3",
    )


def _phase_chip() -> rx.Component:
    """A status pill reflecting the current lifecycle phase."""
    return rx.badge(
        rx.match(
            SaiseiUIState.phase,
            ("idle", "待機中"),
            ("assessing", "診断中…"),
            ("meeting", "会議中…"),
            ("awaiting_decision", "決定待ち"),
            ("drafting", "計画書作成中…"),
            ("done", "完了"),
            ("error", "エラー"),
            "待機中",
        ),
        variant="soft",
        color_scheme=rx.match(
            SaiseiUIState.phase,
            ("awaiting_decision", "grass"),
            ("done", "green"),
            ("error", "red"),
            "gray",
        ),
        radius="full",
        size="2",
    )


def _top_bar() -> rx.Component:
    return rx.box(
        rx.hstack(
            _brand(),
            rx.spacer(),
            rx.hstack(
                _phase_chip(),
                origination_trigger_button(),
                servicing_trigger_button(),
                rx.button(
                    rx.icon("layers", size=16),
                    rx.cond(
                        SaiseiUIState.portfolio_count > 0,
                        rx.badge(
                            SaiseiUIState.portfolio_count.to_string(),
                            color_scheme="gray",
                            variant="soft",
                            radius="full",
                            size="1",
                        ),
                        rx.fragment(),
                    ),
                    on_click=SaiseiUIState.open_portfolio,
                    variant="soft",
                    color_scheme="gray",
                    size="2",
                    style=_FOCUSABLE,
                ),
                rx.color_mode.button(
                    variant="soft",
                    color_scheme="gray",
                    size="2",
                    style=_FOCUSABLE,
                ),
                rx.input(
                    value=SaiseiUIState.tdb_code,
                    on_change=SaiseiUIState.set_tdb_code,
                    placeholder="TDB企業コード (7 digits)",
                    max_length=7,
                    width=["100%", "100%", "200px"],
                    size="3",
                    style=_FOCUSABLE,
                ),
                rx.button(
                    rx.cond(
                        SaiseiUIState.is_running,
                        rx.spinner(size="2"),
                        rx.icon("play", size=16),
                    ),
                    "診断実行",
                    on_click=SaiseiUIState.run_assessment,
                    disabled=SaiseiUIState.is_running | ~SaiseiUIState.code_valid,
                    color_scheme="grass",
                    size="3",
                    width=["100%", "100%", "auto"],
                    style=_FOCUSABLE,
                ),
                align="center",
                spacing="3",
                width=["100%", "100%", "auto"],
                wrap="wrap",
            ),
            align="center",
            width="100%",
            wrap="wrap",
            spacing="3",
        ),
        position="sticky",
        top="0",
        z_index="10",
        padding=["12px 16px", "12px 16px", "16px 32px"],
        background=COLORS["surface"],
        border_bottom=f"1px solid {COLORS['border']}",
        width="100%",
        box_shadow=SHADOW["sm"],
    )


def _panel(*children: rx.Component) -> rx.Component:
    """A column wrapper card used inside a tab panel."""
    return rx.box(
        rx.vstack(
            *children,
            spacing="5",
            style={"gap": [SPACE["5"], SPACE["6"], SPACE["7"]]},
            width="100%",
        ),
        padding=[SPACE["4"], SPACE["5"], SPACE["6"]],
        background=COLORS["bg"],
        width="100%",
        min_width="0",
    )


def _keikakusho_block() -> rx.Component:
    """The Keikakusho draft block with PDF/Word/Excel exports.

    Carries the ``saisei-print-region`` class on the rendered document so the
    PDF (正式版) export isolates exactly this block. Lives in the Plan tab, whose
    panel is kept mounted (see ``_plan_panel``) so printing works regardless of
    which tab is active (Feature 9 §5.6).
    """
    return rx.cond(
        SaiseiUIState.keikakusho_draft != "",
        rx.vstack(
            rx.hstack(
                rx.icon("file-text", size=16, color=COLORS["chrome"]),
                rx.heading(
                    "経営改善計画書 (Keikakusho)",
                    size="4",
                    color=COLORS["text"],
                ),
                rx.spacer(),
                rx.button(
                    rx.icon("printer", size=14),
                    "PDF出力",
                    on_click=SaiseiUIState.print_keikakusho,
                    color_scheme="grass",
                    variant="solid",
                    size="1",
                    style=_FOCUSABLE,
                ),
                rx.button(
                    rx.icon("file-down", size=14),
                    "Word出力",
                    on_click=SaiseiUIState.download_keikakusho_docx,
                    color_scheme="gray",
                    variant="soft",
                    size="1",
                    style=_FOCUSABLE,
                ),
                rx.cond(
                    SaiseiUIState.has_recovery_projection,
                    rx.button(
                        rx.icon("sheet", size=14),
                        "Excel出力",
                        on_click=SaiseiUIState.download_recovery_xlsx,
                        color_scheme="grass",
                        variant="soft",
                        size="1",
                        style=_FOCUSABLE,
                    ),
                ),
                align="center",
                spacing="2",
                width="100%",
            ),
            rx.box(
                rx.markdown(SaiseiUIState.keikakusho_draft),
                class_name="saisei-print-region",
                padding=SPACE["5"],
                background=COLORS["surface"],
                border=f"1px solid {COLORS['border']}",
                border_radius=RADII["lg"],
                box_shadow=SHADOW["sm"],
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
    )


def _assessment_panel() -> rx.Component:
    """Assessment (診断) tab: the deterministic case-file build."""
    return rx.box(
        _panel(
            _demo_picker(),
            ews_dashboard(),
            threshold_panel(),
            rx.hstack(
                shisanhyo_upload_dialog(),
                rx.spacer(),
                align="center",
                width="100%",
            ),
            shisanhyo_table(),
            feasibility_panel(),
        ),
        role="tabpanel",
        width="100%",
        display=rx.cond(SaiseiUIState.effective_tab == "assessment", "block", "none"),
    )


def _meeting_tab_panel() -> rx.Component:
    """Meeting (会議) tab: the live transcript + the burden-sharing table."""
    return rx.box(
        rx.box(
            meeting_panel(),
            padding=[SPACE["4"], SPACE["5"], SPACE["6"]],
            background=COLORS["surface_2"],
            width="100%",
        ),
        rx.box(
            burden_table(),
            padding=[SPACE["4"], SPACE["5"], SPACE["6"]],
            background=COLORS["bg"],
            width="100%",
        ),
        role="tabpanel",
        width="100%",
        display=rx.cond(SaiseiUIState.effective_tab == "meeting", "block", "none"),
    )


def _plan_panel() -> rx.Component:
    """Plan (計画) tab: recovery chart + Keikakusho draft & exports.

    IMPORTANT (Feature 9 §5.6): this panel is kept MOUNTED at all times and only
    hidden via ``display`` when inactive, so the ``saisei-print-region`` inside
    ``_keikakusho_block`` stays in the DOM — ``window.print()`` therefore works
    whether or not the Plan tab is the active one. Do NOT switch this to an
    ``rx.cond`` that unmounts the panel, or the PDF export will silently break.
    """
    return rx.box(
        _panel(
            recovery_chart(),
            pnl_bridge(),
            _keikakusho_block(),
        ),
        role="tabpanel",
        width="100%",
        display=rx.cond(SaiseiUIState.effective_tab == "plan", "block", "none"),
    )


def _audit_tab_panel() -> rx.Component:
    """Audit (監査) tab: the Feature 7 immutable audit trail + the loan ledger."""
    return rx.box(
        _panel(audit_panel(), loan_ledger_panel()),
        role="tabpanel",
        width="100%",
        display=rx.cond(SaiseiUIState.effective_tab == "audit", "block", "none"),
    )


def _borrower_workspace() -> rx.Component:
    """The tabbed borrower workspace: the tab bar + the four panels.

    Each panel renders its existing components unchanged and self-hides via
    ``display`` based on ``effective_tab``; the Plan panel stays mounted so the
    print region is always reachable (Feature 9 §5.6).
    """
    return rx.box(
        workspace_tabs(),
        _assessment_panel(),
        _meeting_tab_panel(),
        _plan_panel(),
        _audit_tab_panel(),
        width="100%",
    )


def _gate_screen() -> rx.Component:
    """Password screen shown when SAISEI_DEMO_PASSWORD is set and not unlocked.

    Lets a public demo URL be usable only by people you give the password to.
    The configured password is validated server-side (see SaiseiUIState); only
    the unlocked boolean is sent to the client.
    """
    return rx.center(
        rx.vstack(
            rx.box(
                rx.center(
                    rx.text(
                        "再",
                        style={"fontSize": "30px", "fontWeight": "800", "color": "#ffffff"},
                    ),
                    width="100%",
                    height="100%",
                ),
                width="64px",
                height="64px",
                background=GRADIENT["brand"],
                border_radius=RADII["lg"],
                box_shadow=SHADOW["glow"],
            ),
            rx.heading("Saisei 再生", style=TYPE["h1"], color=COLORS["text"]),
            rx.text(
                "このデモはパスワードで保護されています。 (This demo is password-protected.)",
                style=TYPE["small"],
                color=COLORS["text_muted"],
                text_align="center",
            ),
            rx.input(
                value=SaiseiUIState.gate_input,
                on_change=SaiseiUIState.set_gate_input,
                placeholder="パスワード (password)",
                type="password",
                size="3",
                width="100%",
                style=_FOCUSABLE,
            ),
            rx.cond(
                SaiseiUIState.gate_error != "",
                rx.text(SaiseiUIState.gate_error, style=TYPE["small"], color=COLORS["fail"]),
            ),
            rx.button(
                "アクセス (Enter)",
                on_click=SaiseiUIState.submit_gate,
                color_scheme="grass",
                size="3",
                width="100%",
                style=_FOCUSABLE,
            ),
            spacing="4",
            align="center",
            width="100%",
            max_width="360px",
            padding=SPACE["6"],
            background=COLORS["surface"],
            border=f"1px solid {COLORS['border']}",
            border_radius=RADII["xl"],
            box_shadow=SHADOW["lg"],
        ),
        min_height="100vh",
        width="100%",
        background=COLORS["bg"],
        padding=SPACE["4"],
    )


def _phase_stepper() -> rx.Component:
    """Horizontal progress stepper showing the run's lifecycle stage.

    Renders the five stages (診断 → 会議 → 決定 → 作成 → 完了) and highlights the
    current one from ``SaiseiUIState.phase_index``. Hidden before the first run
    (idle) and on error, so it only appears while a run is in flight or done.
    Display-only.
    """
    steps = [
        ("診断", "Assess", 1),
        ("会議", "Meeting", 2),
        ("決定", "Decision", 3),
        ("作成", "Drafting", 4),
        ("完了", "Done", 5),
    ]

    def _step(label_ja: str, label_en: str, idx: int) -> rx.Component:
        active = SaiseiUIState.phase_index == idx
        done = SaiseiUIState.phase_index > idx
        dot_color = rx.cond(active | done, COLORS["chrome"], COLORS["border"])
        return rx.hstack(
            rx.box(
                rx.cond(
                    done,
                    rx.icon("check", size=12, color=COLORS["surface"]),
                    rx.box(
                        width="6px",
                        height="6px",
                        border_radius=RADII["pill"],
                        background=rx.cond(active, COLORS["surface"], COLORS["text_faint"]),
                    ),
                ),
                display="flex",
                align_items="center",
                justify_content="center",
                width="20px",
                height="20px",
                min_width="20px",
                border_radius=RADII["pill"],
                background=dot_color,
            ),
            rx.text(
                label_ja,
                style=TYPE["caption"],
                color=rx.cond(active, COLORS["text"], COLORS["text_faint"]),
                font_weight=rx.cond(active, "700", "600"),
            ),
            align="center",
            spacing="2",
        )

    return rx.cond(
        SaiseiUIState.has_started & (SaiseiUIState.phase != "error"),
        rx.hstack(
            *[
                rx.fragment(
                    _step(ja, en, idx),
                    rx.cond(
                        idx < 5,
                        rx.box(
                            height="1px",
                            flex="1",
                            min_width="12px",
                            background=rx.cond(
                                SaiseiUIState.phase_index > idx,
                                COLORS["chrome"],
                                COLORS["border"],
                            ),
                        ),
                    ),
                )
                for ja, en, idx in steps
            ],
            align="center",
            spacing="3",
            width="100%",
            padding=["8px 16px", "8px 16px", "10px 32px"],
            background=COLORS["surface"],
            border_bottom=f"1px solid {COLORS['border']}",
            overflow_x="auto",
        ),
    )


def _rail_item(
    *,
    icon: str,
    label_ja: str,
    label_en: str,
    altitude: str,
    on_click,
    badge: rx.Component | None = None,
    context: rx.Component | None = None,
) -> rx.Component:
    """One altitude entry in the left rail (icon + bilingual label + a11y state).

    Highlights when ``SaiseiUIState.active_altitude`` matches ``altitude`` so
    exactly one item reads as current. ``aria-current="page"`` is set on the
    active item for assistive tech; the whole row is a focusable button.
    Display-only navigation chrome.
    """
    active = SaiseiUIState.active_altitude == altitude
    return rx.box(
        rx.hstack(
            rx.box(
                rx.icon(icon, size=18),
                rx.cond(badge is not None, badge, rx.fragment()),
                position="relative",
                display="flex",
                align_items="center",
                justify_content="center",
            ),
            rx.vstack(
                rx.text(
                    label_ja,
                    style=TYPE["small"],
                    font_weight=rx.cond(active, "700", "600"),
                    color=rx.cond(active, COLORS["text"], COLORS["text_muted"]),
                ),
                rx.text(label_en, style=TYPE["caption"], color=COLORS["text_faint"]),
                rx.cond(context is not None, context, rx.fragment()),
                spacing="0",
                align="start",
            ),
            align="center",
            spacing="3",
            width="100%",
        ),
        on_click=on_click,
        role="link",
        aria_current=rx.cond(active, "page", ""),
        tab_index=0,
        cursor="pointer",
        width="100%",
        padding=[SPACE["2"], SPACE["2"], SPACE["3"]],
        border_radius=RADII["md"],
        border=rx.cond(
            active,
            f"1px solid {COLORS['border']}",
            "1px solid transparent",
        ),
        background=rx.cond(active, COLORS["surface_2"], "transparent"),
        box_shadow=rx.cond(active, SHADOW["sm"], "none"),
        color=rx.cond(active, COLORS["chrome"], COLORS["text_faint"]),
        transition="background 120ms ease, border-color 120ms ease",
        style={
            "&:hover": {"background": COLORS["surface_2"]},
            **_FOCUSABLE,
        },
    )


def _left_rail() -> rx.Component:
    """The persistent left rail — the altitude navigator (Feature 9 §6, Phase 2).

    Ties the three altitudes together: Portfolio (book) ↔ Borrower (case) ↔
    Examiner (audit). It replaces top-bar-only chrome with a persistent
    navigator now that a second altitude (Portfolio) exists to navigate between;
    the borrower tabs remain the in-page nav of the borrower altitude.

    Responsive: a vertical rail from the ``md`` breakpoint, hidden on mobile
    (``display:none``) where the existing top bar carries the Portfolio
    affordance — so the rail is purely additive and never breaks small screens.
    Display-only.
    """
    portfolio_badge = rx.cond(
        SaiseiUIState.portfolio_count > 0,
        rx.badge(
            SaiseiUIState.portfolio_count.to_string(),
            color_scheme=rx.cond(SaiseiUIState.portfolio_crossed_count > 0, "red", "gray"),
            variant="solid",
            radius="full",
            size="1",
            position="absolute",
            top="-8px",
            right="-10px",
        ),
        rx.fragment(),
    )
    borrower_context = rx.cond(
        SaiseiUIState.company_name != "",
        rx.text(
            SaiseiUIState.company_name,
            style=TYPE["caption"],
            color=COLORS["text_muted"],
            font_weight="600",
            no_of_lines=1,
        ),
        rx.fragment(),
    )
    return rx.box(
        rx.vstack(
            _brand(),
            rx.box(height=SPACE["2"]),
            _rail_item(
                icon="layers",
                label_ja="ポートフォリオ",
                label_en="Portfolio · book",
                altitude="portfolio",
                on_click=SaiseiUIState.open_portfolio,
                badge=portfolio_badge,
            ),
            _rail_item(
                icon="briefcase",
                label_ja="借り手",
                label_en="Borrower · case",
                altitude="borrower",
                on_click=SaiseiUIState.close_portfolio,
                context=borrower_context,
            ),
            _rail_item(
                icon="scroll-text",
                label_ja="監査",
                label_en="Examiner · audit",
                altitude="examiner",
                on_click=SaiseiUIState.open_examiner,
            ),
            spacing="2",
            align="stretch",
            width="100%",
        ),
        role="navigation",
        aria_label="Altitude navigator",
        display=["none", "none", "flex"],
        flex_direction="column",
        position="sticky",
        top="0",
        align_self="flex-start",
        height="100vh",
        min_width="220px",
        width="220px",
        padding=SPACE["4"],
        background=COLORS["surface"],
        border_right=f"1px solid {COLORS['border']}",
    )


def _app_body() -> rx.Component:
    """The full Saisei workspace (left rail + top bar + tabbed workspace).

    Phase 2 (Feature 9 §6): a persistent left rail is the altitude navigator on
    desktop, with the main column to its right. The main column shows the
    Altitude-1 Portfolio watchlist instead of the borrower workspace when
    ``show_portfolio`` is set; the top bar persists in both so a mobile banker
    (where the rail is hidden) can still navigate back. Display-only toggle.
    """
    main_column = rx.box(
        _top_bar(),
        rx.cond(
            SaiseiUIState.show_portfolio,
            portfolio_panel(),
            rx.box(
                _phase_stepper(),
                _borrower_workspace(),
                width="100%",
            ),
        ),
        flex="1",
        min_width="0",
        width="100%",
    )
    return rx.box(
        _left_rail(),
        main_column,
        display="flex",
        align_items="stretch",
        background=COLORS["bg"],
        min_height="100vh",
        width="100%",
        max_width="100vw",
        overflow_x="hidden",
        style={"fontFamily": FONT["sans"], "color": COLORS["text"]},
    )


def index() -> rx.Component:
    """Render the page: the demo gate, or the app once unlocked / ungated."""
    return rx.box(
        rx.el.style(THEME_CSS),
        rx.cond(SaiseiUIState.show_app, _app_body(), _gate_screen()),
        # The summonable advisory companion floats over the workspace once the
        # app is unlocked (it is gated behind show_app so it never appears on
        # the password screen). Non-modal: it overlays without blocking the page.
        rx.cond(SaiseiUIState.show_app, saisei_companion(), rx.fragment()),
        # The 融資組成 (new facility) entry dialog — opened from the top-bar
        # trigger; renders nothing until ``show_origination`` is set.
        rx.cond(SaiseiUIState.show_app, origination_dialog(), rx.fragment()),
        # The 貸出管理 (servicing) entry dialog — opened from the top-bar trigger;
        # renders nothing until ``show_servicing`` is set.
        rx.cond(SaiseiUIState.show_app, servicing_dialog(), rx.fragment()),
        style={"fontFamily": FONT["sans"]},
    )
