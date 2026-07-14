"""Borrower workspace tab navigation (Feature 9 §5.4 — the meta-interface).

The meta-interface splits the borrower "case file" out of one infinite scroll
into four altitude-2 tabs — Assessment / Meeting / Plan / Audit — so the banker
sees one job's worth of UI at a time. This module is the tab BAR only; the
panels themselves are assembled in ``app.frontend.pages.index`` from the
already-existing components (this feature only re-homes them, it rewrites none).

Pure presentation, display-only (Feature 9 §2 / §11):
- It reads ``SaiseiUIState.effective_tab`` (the banker's explicit pick, else the
  phase-implied tab) to know which tab is active, and calls ``set_active_tab``
  on click. It never computes a figure, a verdict, or a route.
- Accessibility: each tab is a real keyboard-focusable button with
  ``role="tab"`` + ``aria-selected``; callers wrap their panel in a
  ``role="tabpanel"`` container. The shared ``FOCUS_RING`` gives a visible
  focus-visible outline (mirrors the recovery-chart a11y pass).
- Brand rule: the active tab uses the blue ``chrome`` token (structural nav);
  green ``positive`` is reserved for the brand mark and positive states, so the
  nav is never painted green.
- Mobile containment: the bar scrolls horizontally and never overflows the
  viewport (reuses the ``overflow_x:auto`` / ``max_width:100vw`` discipline of
  ``_app_body``).

The ``active_tab`` string set is kept enum-like so Phase 2 can map each tab 1:1
onto a borrower route segment without a rewrite (Feature 9 §6).
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import COLORS, FOCUS_RING, RADII, SPACE, TYPE

__all__ = ["workspace_tabs", "TABS"]

#: The four borrower tabs, in display order: (key, Japanese, English, icon).
#: ``key`` matches the ``SaiseiUIState._VALID_TABS`` set and maps 1:1 onto the
#: Phase-2 route segment (Feature 9 §6), so this is the single source of order.
TABS: tuple[tuple[str, str, str, str], ...] = (
    ("assessment", "診断", "Assessment", "stethoscope"),
    ("meeting", "会議", "Meeting", "users"),
    ("plan", "計画", "Plan", "file-text"),
    ("audit", "監査", "Audit", "scroll-text"),
)


def _status_dot(tab_key: str) -> rx.Component:
    """A small dot when a tab has live or new content.

    Display-only, derived from ``phase`` / ``keikakusho_draft`` (never written):
    - Meeting shows a dot while the creditor meeting / decision is in flight.
    - Plan shows a dot once a Keikakusho draft exists.
    Returns an empty fragment for tabs with nothing to flag.
    """
    if tab_key == "meeting":
        show = (SaiseiUIState.phase == "meeting") | (SaiseiUIState.phase == "awaiting_decision")
        color = COLORS["chrome"]
    elif tab_key == "plan":
        show = SaiseiUIState.keikakusho_draft != ""
        color = COLORS["positive"]
    else:
        return rx.fragment()
    return rx.cond(
        show,
        rx.box(
            width="7px",
            height="7px",
            min_width="7px",
            border_radius=RADII["pill"],
            background=color,
        ),
    )


def _tab_button(key: str, label_ja: str, label_en: str, icon: str) -> rx.Component:
    """One tab control: a real, keyboard-activatable <button> branded blue active.

    Rendered as ``rx.el.button`` (a native ``<button>``) rather than a focusable
    ``<div>`` so Enter/Space activate it for free (a ``role="tab"`` div with
    ``on_click`` is announced but NOT keyboard-activatable by default) — this is
    the spec §5.4 requirement that each tab be a real keyboard-focusable
    control. The native button chrome (border / background / inherited font) is
    reset so the visual is identical to the prior box-based tab.
    """
    active = SaiseiUIState.effective_tab == key
    return rx.el.button(
        rx.hstack(
            rx.icon(
                icon,
                size=15,
                color=rx.cond(active, COLORS["chrome"], COLORS["text_faint"]),
            ),
            rx.text(
                label_ja,
                style=TYPE["small"],
                color=rx.cond(active, COLORS["text"], COLORS["text_muted"]),
                font_weight=rx.cond(active, "700", "600"),
                white_space="nowrap",
            ),
            _status_dot(key),
            align="center",
            spacing="2",
        ),
        on_click=SaiseiUIState.set_active_tab(key),
        type="button",
        role="tab",
        aria_selected=rx.cond(active, "true", "false"),
        cursor="pointer",
        padding="10px 16px",
        white_space="nowrap",
        # Reset native button chrome so the <button> looks exactly like the
        # previous box-based tab (no UA border/background, inherit the font).
        border="none",
        background=rx.cond(active, COLORS["chrome_soft"], "transparent"),
        border_bottom=rx.cond(
            active,
            f"2px solid {COLORS['chrome']}",
            "2px solid transparent",
        ),
        border_top_left_radius=RADII["sm"],
        border_top_right_radius=RADII["sm"],
        font_family="inherit",
        transition="background 0.15s ease-out, border-color 0.15s ease-out",
        style={
            "&:hover": {"background": COLORS["surface_2"]},
            "&:focus-visible": {"boxShadow": FOCUS_RING, "outline": "none"},
        },
    )


def workspace_tabs() -> rx.Component:
    """Render the borrower workspace tab bar (Assessment/Meeting/Plan/Audit).

    Display-only nav: reads ``effective_tab`` for the active state and calls
    ``set_active_tab`` on click. The bar scrolls horizontally on narrow
    viewports and carries ``role="tablist"`` for assistive tech.
    """
    return rx.box(
        rx.hstack(
            *[_tab_button(key, ja, en, icon) for key, ja, en, icon in TABS],
            spacing="1",
            align="end",
            width="max-content",
        ),
        role="tablist",
        aria_label="借り入れワークスペース (Borrower workspace)",
        width="100%",
        max_width="100vw",
        overflow_x="auto",
        padding=[f"0 {SPACE['4']}", f"0 {SPACE['4']}", f"0 {SPACE['6']}"],
        background=COLORS["surface"],
        border_bottom=f"1px solid {COLORS['border']}",
    )
