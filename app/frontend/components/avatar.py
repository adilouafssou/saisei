"""Persona avatar component.

Renders a chat-app-style avatar for an agent: a rounded square badge tinted with
the persona's accent color, showing the persona's kanji monogram, with the
Lucide icon as a small corner glyph. This is the visual element that lets a
banker identify each creditor voice at a glance in the meeting transcript.
"""

from __future__ import annotations

import reflex as rx

from app.frontend.theme import COLORS, RADII, Persona

__all__ = ["persona_avatar"]


def persona_avatar(persona: Persona, size: int = 44) -> rx.Component:
    """Render a persona avatar badge.

    Args:
        persona: The persona identity to render.
        size: Avatar edge length in pixels.

    Returns:
        A Reflex component.
    """
    glyph = max(16, int(size * 0.42))
    icon_size = max(10, int(size * 0.30))
    return rx.box(
        rx.center(
            rx.text(
                persona.kanji,
                style={
                    "fontSize": f"{glyph}px",
                    "fontWeight": "700",
                    "color": persona.accent,
                    "lineHeight": "1",
                },
            ),
            width="100%",
            height="100%",
        ),
        # Corner icon chip for a secondary, language-independent identity cue.
        rx.box(
            rx.icon(persona.icon, size=icon_size, color=COLORS["surface"]),
            position="absolute",
            bottom="-4px",
            right="-4px",
            padding="3px",
            background=persona.accent,
            border_radius=RADII["pill"],
            border=f"2px solid {COLORS['surface']}",
            display="flex",
        ),
        position="relative",
        width=f"{size}px",
        height=f"{size}px",
        min_width=f"{size}px",
        background=persona.accent_soft,
        border=f"1.5px solid {persona.accent}55",
        border_radius=RADII["md"],
        box_shadow=f"0 0 0 1px {persona.accent}22, 0 4px 12px rgba(29,37,48,0.10)",
    )
