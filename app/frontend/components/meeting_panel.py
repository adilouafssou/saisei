"""Meeting-room transcript panel (the creditor meeting, chat-style).

Renders ``SaiseiUIState.meeting_events`` as a stream of chat bubbles — one per
agent voice — with persona avatars, PASS/FAIL status chips, priority tags, and
fatal-blocker lists. When the graph pauses at the HITL interrupt, the panel's
footer becomes the banker's action bar (approve a strategy, request a revision,
or reject), including the banker-only commitment-flag toggles.

Display-only: every value comes from streamed node updates / the snapshot.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.state import MeetingEvent, SaiseiUIState
from app.frontend.theme import COLORS, PERSONAS, RADII, SHADOW

__all__ = ["meeting_panel"]


def _status_chip(status: rx.Var[str]) -> rx.Component:
    """Render a PASS/FAIL/decision chip with semantic coloring."""
    return rx.cond(
        status != "",
        rx.badge(
            status,
            variant="soft",
            color_scheme=rx.match(
                status,
                ("PASS", "green"),
                ("APPROVED", "green"),
                ("FAIL", "red"),
                ("REJECTED", "red"),
                ("REVISE", "amber"),
                ("NEEDS_HUMAN", "amber"),
                "gray",
            ),
            radius="full",
            high_contrast=True,
        ),
    )


def _priority_chip(priority: rx.Var[str]) -> rx.Component:
    """Render a P0/P1/P2 priority tag."""
    return rx.cond(
        priority != "",
        rx.badge(priority, variant="outline", color_scheme="gray", radius="full"),
    )


def _blocker_list(blockers: rx.Var[list[str]]) -> rx.Component:
    """Render the fatal blockers for a critic bubble."""
    return rx.cond(
        blockers.length() > 0,
        rx.vstack(
            rx.foreach(
                blockers,
                lambda b: rx.hstack(
                    rx.icon("circle-alert", size=14, color=COLORS["fail"]),
                    rx.text(b, size="1", color=COLORS["text_muted"]),
                    align="start",
                    spacing="2",
                ),
            ),
            spacing="2",
            margin_top="6px",
            padding="10px 12px",
            background=COLORS["fail_soft"],
            border=f"1px solid {COLORS['fail']}33",
            border_radius=RADII["sm"],
            width="100%",
        ),
    )


def _bubble(event: rx.Var[MeetingEvent]) -> rx.Component:
    """Render a single transcript bubble for one meeting event.

    The speaker key selects the persona identity (avatar, color, name) via a
    client-side ``rx.match`` over the known persona registry. Field access uses
    attribute syntax because ``event`` is a typed ``MeetingEvent`` var.
    """
    speaker = event.speaker

    accent = rx.match(
        speaker,
        *[(k, p.accent) for k, p in PERSONAS.items()],
        COLORS["text_muted"],
    )
    name_ja = rx.match(
        speaker,
        *[(k, p.name_ja) for k, p in PERSONAS.items()],
        "",
    )
    name_en = rx.match(
        speaker,
        *[(k, p.name_en) for k, p in PERSONAS.items()],
        "",
    )
    kanji = rx.match(
        speaker,
        *[(k, p.kanji) for k, p in PERSONAS.items()],
        "再",
    )
    icon = rx.match(
        speaker,
        *[(k, p.icon) for k, p in PERSONAS.items()],
        "activity",
    )
    accent_soft = rx.match(
        speaker,
        *[(k, p.accent_soft) for k, p in PERSONAS.items()],
        COLORS["surface_2"],
    )

    avatar = rx.box(
        rx.center(
            rx.text(
                kanji,
                style={"fontSize": "18px", "fontWeight": "700", "color": accent},
            ),
            width="100%",
            height="100%",
        ),
        rx.box(
            rx.icon(icon, size=12, color=COLORS["bg"]),
            position="absolute",
            bottom="-4px",
            right="-4px",
            padding="3px",
            background=accent,
            border_radius=RADII["pill"],
            border=f"2px solid {COLORS['surface']}",
            display="flex",
        ),
        position="relative",
        width="44px",
        height="44px",
        min_width="44px",
        background=accent_soft,
        border=f"1.5px solid {accent}55",
        border_radius=RADII["md"],
    )

    body = rx.vstack(
        rx.hstack(
            rx.text(name_ja, weight="bold", size="2", color=COLORS["text"]),
            rx.text(name_en, size="1", color=COLORS["text_faint"]),
            _priority_chip(event.priority),
            _status_chip(event.status),
            align="center",
            spacing="2",
            wrap="wrap",
        ),
        rx.cond(
            event.title != "",
            rx.text(event.title, size="2", color=COLORS["text_muted"]),
        ),
        rx.cond(
            event.body != "",
            rx.box(
                rx.markdown(event.body),
                font_size="13px",
                color=COLORS["text_muted"],
                width="100%",
                overflow_x="auto",
            ),
        ),
        _blocker_list(event.blockers),
        spacing="1",
        align="start",
        width="100%",
    )

    card = rx.box(
        body,
        padding="12px 14px",
        background=COLORS["surface"],
        border=f"1px solid {COLORS['border']}",
        border_left=f"3px solid {accent}",
        border_radius=RADII["md"],
        box_shadow=SHADOW["sm"],
        width="100%",
    )

    return rx.hstack(
        avatar,
        card,
        align="start",
        spacing="3",
        width="100%",
    )


def _typing_indicator() -> rx.Component:
    """Animated 'agent is working' row shown while the graph streams."""
    return rx.cond(
        SaiseiUIState.is_running,
        rx.hstack(
            rx.spinner(size="2"),
            rx.text(
                rx.cond(
                    SaiseiUIState.active_node != "",
                    "分析中: " + SaiseiUIState.active_node,
                    "診断を実行しています… (Working…)",
                ),
                size="1",
                color=COLORS["text_faint"],
            ),
            align="center",
            spacing="2",
            padding="4px 2px",
        ),
    )


def _commitment_toggles() -> rx.Component:
    """Banker-only commitment flags that clear the main_bank critic gates."""
    return rx.vstack(
        rx.text(
            "コミットメント確認 (Banker-only commitments)",
            size="1",
            weight="bold",
            color=COLORS["text_faint"],
        ),
        rx.hstack(
            rx.switch(
                checked=SaiseiUIState.yakuin_hoshu_cut,
                on_change=SaiseiUIState.toggle_yakuin_hoshu_cut,
                color_scheme="indigo",
            ),
            rx.text("役員報酬削減 (exec-comp cut)", size="1", color=COLORS["text_muted"]),
            align="center",
            spacing="2",
        ),
        rx.hstack(
            rx.switch(
                checked=SaiseiUIState.personal_asset_disposal,
                on_change=SaiseiUIState.toggle_personal_asset_disposal,
                color_scheme="indigo",
            ),
            rx.text("個人資産処分 (asset disposal)", size="1", color=COLORS["text_muted"]),
            align="center",
            spacing="2",
        ),
        spacing="2",
        align="start",
        width="100%",
        padding="10px 12px",
        background=COLORS["surface_2"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["sm"],
    )


def _strategy_card(strategy: rx.Var[dict[str, str]]) -> rx.Component:
    """Render one proposed strategy with an approve button."""
    return rx.box(
        rx.hstack(
            rx.vstack(
                rx.text(strategy["title"], weight="bold", size="2", color=COLORS["text"]),
                rx.text(strategy["rationale"], size="1", color=COLORS["text_muted"]),
                rx.text(
                    "期待経常利益改善: " + strategy["uplift"] + " / 年",
                    size="1",
                    weight="medium",
                    color=COLORS["pass"],
                ),
                spacing="1",
                align="start",
            ),
            rx.button(
                "承認",
                rx.icon("check", size=15),
                on_click=SaiseiUIState.approve(strategy["index"].to(int)),
                color_scheme="green",
                variant="solid",
                size="2",
            ),
            justify="between",
            align="center",
            width="100%",
        ),
        padding="12px",
        background=COLORS["surface_2"],
        border=f"1px solid {COLORS['border']}",
        border_radius=RADII["sm"],
        width="100%",
    )


def _strategy_choices() -> rx.Component:
    """Approve buttons for each proposed strategy."""
    return rx.vstack(
        rx.foreach(SaiseiUIState.strategies, _strategy_card),
        spacing="2",
        width="100%",
    )


def _action_bar() -> rx.Component:
    """The banker's HITL action bar, shown only while awaiting a decision."""
    return rx.cond(
        SaiseiUIState.awaiting_decision,
        rx.vstack(
            rx.divider(),
            rx.hstack(
                rx.icon("gavel", size=16, color=COLORS["brand"]),
                rx.text(
                    "担当者の決定 (Your decision)",
                    weight="bold",
                    size="2",
                    color=COLORS["text"],
                ),
                align="center",
                spacing="2",
            ),
            _commitment_toggles(),
            _strategy_choices(),
            rx.text_area(
                placeholder="修正依頼メモ (revision note)…",
                on_change=SaiseiUIState.set_revision_note_buffer,
                value=SaiseiUIState.revision_note_buffer,
                resize="vertical",
                size="2",
            ),
            rx.hstack(
                rx.button(
                    "修正依頼",
                    rx.icon("pencil", size=15),
                    on_click=SaiseiUIState.revise,
                    color_scheme="amber",
                    variant="soft",
                ),
                rx.button(
                    "却下",
                    rx.icon("x", size=15),
                    on_click=SaiseiUIState.reject,
                    color_scheme="red",
                    variant="soft",
                ),
                spacing="2",
                justify="end",
                width="100%",
            ),
            spacing="3",
            width="100%",
            padding="14px",
            background=COLORS["surface"],
            border=f"1px solid {COLORS['brand']}55",
            border_radius=RADII["md"],
            box_shadow=SHADOW["glow"],
        ),
    )


def _empty_state() -> rx.Component:
    """Friendly placeholder before the first assessment runs."""
    return rx.cond(
        ~SaiseiUIState.has_started,
        rx.center(
            rx.vstack(
                rx.icon("users-round", size=40, color=COLORS["text_faint"]),
                rx.text(
                    "債権者会議ルーム (Creditor Meeting Room)",
                    weight="bold",
                    color=COLORS["text_muted"],
                ),
                rx.text(
                    "TDBコードを入力して診断を開始すると、各債権者の議論がここに順に表示されます。",
                    size="1",
                    color=COLORS["text_faint"],
                    text_align="center",
                ),
                spacing="3",
                align="center",
                max_width="320px",
            ),
            min_height="260px",
            width="100%",
        ),
    )


def meeting_panel() -> rx.Component:
    """Render the creditor-meeting transcript + HITL action bar."""
    return rx.vstack(
        rx.hstack(
            rx.icon("messages-square", size=18, color=COLORS["brand"]),
            rx.heading("債権者会議 (Creditor Meeting)", size="4", color=COLORS["text"]),
            rx.spacer(),
            rx.cond(
                SaiseiUIState.revision_count > 0,
                rx.badge(
                    "Round " + SaiseiUIState.revision_count.to_string(),
                    variant="surface",
                    color_scheme="gray",
                ),
            ),
            align="center",
            width="100%",
        ),
        rx.divider(),
        _empty_state(),
        rx.vstack(
            rx.foreach(SaiseiUIState.meeting_events, _bubble),
            _typing_indicator(),
            spacing="3",
            width="100%",
        ),
        _action_bar(),
        spacing="3",
        width="100%",
        height="100%",
    )
