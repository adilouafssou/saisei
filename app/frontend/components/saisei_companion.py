"""Saisei companion — the summonable floating co-pilot (再生の精 / Saisei spirit).

A round, gently-floating brand orb anchored to the bottom-right of the
workspace. Clicking it *summons* a floating, non-modal chat window that scales
up from the orb, while the banker keeps full access to the page beneath it
(the window is a fixed overlay, not a modal — nothing behind it is blocked).
Clicking again dismisses it; the entity can be re-summoned at any time.

Design stance (innovative, but professional and accessible)
----------------------------------------------------------
- **A living entity, not a chrome button.** The orb bobs and its halo breathes,
  evoking a small summoned spirit — on-brand for 再生 (regeneration). The whimsy
  is layered ON TOP of a boringly reliable affordance.
- **Convenient for older users too.** The orb is a fixed, predictable anchor
  with a large tap target, a real text label (not icon-only), full keyboard
  focus, ``aria`` labels, and an explicit close button; all motion is disabled
  under ``prefers-reduced-motion``. The window is dockable and never covers the
  case data it sits beside.
- **Display-only.** The companion is advisory and READ-ONLY; nothing here moves
  a gate, route, figure, or verdict. Answers are grounded server-side and each
  one shows whether it is fully attributable or carries unverified commentary.

This module is the canonical location under
``app.frontend.components.saisei_companion``.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import SaiseiUIState
from app.frontend.theme import (
    COLORS,
    FOCUS_RING,
    GRADIENT,
    RADII,
    SHADOW,
    SPACE,
    TYPE,
)

__all__ = ["saisei_companion"]

#: Shared focus-visible style (keyboard a11y / WCAG), matching the page chrome.
_FOCUSABLE: dict[str, dict[str, str]] = {
    "&:focus-visible": {"boxShadow": FOCUS_RING, "outline": "none"}
}

#: Suggested questions surfaced as one-tap chips on first open, so the banker
#: (especially a first-time / older user) discovers what the entity can do
#: without typing. Each maps to a real intent in the backend agent.
_SUGGESTIONS: list[str] = [
    "この案件に類似する過去事例は？ (Similar past cases?)",
    "なぜこの区分になりましたか？ (Why this classification?)",
    "主要な数値を要約して (Summarise the key figures)",
]


def _orb(size: int = 60) -> rx.Component:
    """The summonable brand orb: a breathing halo behind a floating 再 monogram.

    It is a real ``<button>`` (focusable, labelled) so it works without motion
    or a pointer; the float/aura animations are pure delight on top.
    """
    return rx.box(
        # Breathing halo (purely decorative; sits behind the orb).
        rx.box(
            position="absolute",
            inset="-6px",
            border_radius=RADII["pill"],
            background=GRADIENT["brand"],
            filter="blur(10px)",
            class_name="saisei-aura",
            aria_hidden="true",
        ),
        # The orb face.
        rx.center(
            rx.cond(
                SaiseiUIState.companion_open,
                rx.icon("x", size=24, color="#ffffff"),
                rx.text(
                    "再",
                    style={
                        "fontSize": "26px",
                        "fontWeight": "800",
                        "color": "#ffffff",
                        "lineHeight": "1",
                    },
                ),
            ),
            position="relative",
            width=f"{size}px",
            height=f"{size}px",
            border_radius=RADII["pill"],
            background=GRADIENT["brand"],
            box_shadow=SHADOW["glow"],
            border="2px solid rgba(255,255,255,0.65)",
        ),
        class_name="saisei-float",
        position="relative",
    )


def _dock() -> rx.Component:
    """The always-present dock: the orb button + a small text label.

    The label makes the affordance legible to first-time / older users (an orb
    alone is ambiguous). The whole dock is the click target and is keyboard
    focusable.
    """
    return rx.box(
        rx.vstack(
            _orb(),
            rx.cond(
                ~SaiseiUIState.companion_open,
                rx.box(
                    rx.text(
                        "再生に聞く",
                        style=TYPE["caption"],
                        color=COLORS["text"],
                        font_weight="700",
                    ),
                    rx.text(
                        "Ask Saisei",
                        style={"fontSize": "10px", "lineHeight": "1.2"},
                        color=COLORS["text_faint"],
                    ),
                    padding=["4px", "4px", "6px 10px"],
                    background=COLORS["surface"],
                    border=f"1px solid {COLORS['border']}",
                    border_radius=RADII["pill"],
                    box_shadow=SHADOW["sm"],
                    text_align="center",
                ),
                rx.fragment(),
            ),
            spacing="2",
            align="center",
        ),
        on_click=SaiseiUIState.toggle_companion,
        role="button",
        tab_index=0,
        aria_label="Saisei 再生 コパイロットを開く / Open the Saisei advisory companion",
        aria_expanded=rx.cond(SaiseiUIState.companion_open, "true", "false"),
        cursor="pointer",
        style=_FOCUSABLE,
    )


def _turn_bubble(turn: dict[str, str]) -> rx.Component:
    """Render one chat turn (banker right-aligned ink, companion left brand).

    Companion turns carry a small provenance chip: GROUNDED (every claim is
    attributable) or UNVERIFIED (the answer contains 【未検証】 commentary), so
    the banker always sees the attribution status at a glance.
    """
    is_banker = turn["role"] == "banker"
    return rx.box(
        rx.cond(
            ~is_banker,
            rx.hstack(
                rx.cond(
                    turn["status"] == "grounded",
                    rx.badge(
                        "接地済 grounded",
                        color_scheme="grass",
                        variant="soft",
                        radius="full",
                        size="1",
                    ),
                    rx.badge(
                        "未検証 unverified",
                        color_scheme="amber",
                        variant="soft",
                        radius="full",
                        size="1",
                    ),
                ),
                spacing="2",
                align="center",
                margin_bottom="4px",
            ),
            rx.fragment(),
        ),
        rx.box(
            rx.text(
                turn["text"],
                style={**TYPE["small"], "whiteSpace": "pre-wrap"},
                color=rx.cond(is_banker, "#ffffff", COLORS["text"]),
            ),
            padding=[SPACE["2"], SPACE["3"]],
            background=rx.cond(is_banker, COLORS["chrome"], COLORS["surface_2"]),
            border=rx.cond(is_banker, "none", f"1px solid {COLORS['border']}"),
            border_radius=RADII["lg"],
            max_width="86%",
        ),
        display="flex",
        flex_direction="column",
        align_items=rx.cond(is_banker, "flex-end", "flex-start"),
        width="100%",
        class_name="saisei-bubble-in",
        style={"animation": "saisei-bubble-in 0.3s ease-out both"},
    )


def _suggestions() -> rx.Component:
    """One-tap starter chips, shown only before the first question."""
    return rx.cond(
        ~SaiseiUIState.companion_has_turns,
        rx.vstack(
            rx.text(
                "例えばこんな質問ができます (Try asking)",
                style=TYPE["caption"],
                color=COLORS["text_faint"],
            ),
            *[
                rx.button(
                    rx.text(s, style={"whiteSpace": "normal", "textAlign": "left"}),
                    on_click=[
                        SaiseiUIState.set_companion_input(s),
                        SaiseiUIState.ask_companion,
                    ],
                    variant="soft",
                    color_scheme="gray",
                    size="1",
                    radius="large",
                    width="100%",
                    style={
                        "justifyContent": "flex-start",
                        "height": "auto",
                        "padding": "8px 12px",
                        **_FOCUSABLE,
                    },
                )
                for s in _SUGGESTIONS
            ],
            spacing="2",
            align="start",
            width="100%",
        ),
        rx.fragment(),
    )


def _chat_window() -> rx.Component:
    """The floating, non-modal chat window summoned above the dock."""
    return rx.cond(
        SaiseiUIState.companion_open,
        rx.box(
            # Header.
            rx.hstack(
                rx.box(
                    rx.center(
                        rx.text(
                            "再",
                            style={
                                "fontSize": "16px",
                                "fontWeight": "800",
                                "color": "#ffffff",
                                "lineHeight": "1",
                            },
                        ),
                        width="100%",
                        height="100%",
                    ),
                    width="32px",
                    height="32px",
                    min_width="32px",
                    background=GRADIENT["brand"],
                    border_radius=RADII["pill"],
                ),
                rx.vstack(
                    rx.text(
                        "再生 コパイロット",
                        style=TYPE["h3"],
                        color=COLORS["text"],
                    ),
                    rx.text(
                        "助言専用 ・ 読み取りのみ (Advisory · read-only)",
                        style={"fontSize": "10px", "lineHeight": "1.2"},
                        color=COLORS["text_faint"],
                    ),
                    spacing="0",
                    align="start",
                ),
                rx.spacer(),
                rx.cond(
                    SaiseiUIState.companion_has_turns,
                    rx.button(
                        rx.icon("eraser", size=14),
                        on_click=SaiseiUIState.clear_companion,
                        variant="ghost",
                        color_scheme="gray",
                        size="1",
                        aria_label="会話を消去 / Clear conversation",
                        style=_FOCUSABLE,
                    ),
                    rx.fragment(),
                ),
                rx.button(
                    rx.icon("x", size=16),
                    on_click=SaiseiUIState.toggle_companion,
                    variant="ghost",
                    color_scheme="gray",
                    size="1",
                    aria_label="閉じる / Dismiss companion",
                    style=_FOCUSABLE,
                ),
                align="center",
                spacing="2",
                width="100%",
                padding=[SPACE["3"], SPACE["3"], SPACE["4"]],
                border_bottom=f"1px solid {COLORS['border']}",
            ),
            # Transcript (scrollable).
            rx.box(
                rx.vstack(
                    _suggestions(),
                    rx.foreach(SaiseiUIState.companion_turns, _turn_bubble),
                    rx.cond(
                        SaiseiUIState.companion_thinking,
                        rx.hstack(
                            rx.spinner(size="1"),
                            rx.text(
                                "考えています… (thinking…)",
                                style=TYPE["caption"],
                                color=COLORS["text_faint"],
                            ),
                            spacing="2",
                            align="center",
                        ),
                        rx.fragment(),
                    ),
                    spacing="3",
                    width="100%",
                    align="start",
                ),
                padding=SPACE["3"],
                overflow_y="auto",
                flex="1",
                width="100%",
            ),
            # Composer.
            rx.hstack(
                rx.input(
                    value=SaiseiUIState.companion_input,
                    on_change=SaiseiUIState.set_companion_input,
                    placeholder="案件について質問… (Ask about this case…)",
                    # Enter sends (convenient for every user; the send button
                    # remains for pointer-only / discoverability). Reflex maps
                    # the special "Enter" key to the handler.
                    on_key_down=SaiseiUIState.companion_key_down,
                    size="2",
                    width="100%",
                    style=_FOCUSABLE,
                ),
                rx.button(
                    rx.icon("send-horizontal", size=16),
                    on_click=SaiseiUIState.ask_companion,
                    disabled=(SaiseiUIState.companion_input == "")
                    | SaiseiUIState.companion_thinking,
                    color_scheme="grass",
                    size="2",
                    aria_label="送信 / Send",
                    style=_FOCUSABLE,
                ),
                spacing="2",
                align="center",
                width="100%",
                padding=[SPACE["3"], SPACE["3"], SPACE["4"]],
                border_top=f"1px solid {COLORS['border']}",
            ),
            # Window shell.
            class_name="saisei-summon",
            display="flex",
            flex_direction="column",
            position="absolute",
            bottom="88px",
            right="0",
            width=["min(92vw, 380px)", "min(92vw, 380px)", "380px"],
            height="min(70vh, 560px)",
            background=COLORS["surface"],
            border=f"1px solid {COLORS['border']}",
            border_radius=RADII["xl"],
            box_shadow=SHADOW["lg"],
            overflow="hidden",
            role="dialog",
            aria_label="Saisei 再生 コパイロット / Saisei advisory companion",
        ),
        rx.fragment(),
    )


def saisei_companion() -> rx.Component:
    """Render the summonable floating companion (dock orb + chat window).

    Fixed to the bottom-right so it is a predictable anchor on every screen, at
    a high ``z-index`` so it floats over the workspace, but as a non-modal
    overlay so the banker keeps full access to the page beneath it. Returns one
    fixed container holding the always-present dock and the conditionally-shown
    window.
    """
    return rx.box(
        _chat_window(),
        _dock(),
        position="fixed",
        bottom=[SPACE["4"], SPACE["5"], SPACE["6"]],
        right=[SPACE["4"], SPACE["5"], SPACE["6"]],
        z_index="50",
        display="flex",
        flex_direction="column",
        align_items="flex-end",
    )
