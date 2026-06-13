"""Saisei design system: tokens, persona identity, and shared visual helpers.

This module is the single source of truth for the frontend's visual language so
components stay consistent and a redesign is a one-file change. It defines:

- ``COLORS`` / ``RADII`` / ``SPACE`` / ``SHADOW`` — design tokens.
- ``Persona`` — the identity of each agent that "speaks" in the meeting room
  (a distinct icon, accent color, role label, and kanji), so a banker can
  recognise each creditor voice at a glance — like an avatar in a chat app.
- ``PERSONAS`` — the registry keyed by the backend ``persona`` string emitted in
  ``CriticFeedback`` (main_bank / sub_bank / guarantor) plus the chair
  (lead_arranger) and the system narrator.
- ``status_color`` / ``classification_color`` — deterministic color mapping for
  PASS/FAIL verdicts and FSA classifications.

The UI is display-only; nothing here computes a verdict or a number.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "COLORS",
    "RADII",
    "SPACE",
    "SHADOW",
    "FONT",
    "Persona",
    "PERSONAS",
    "persona_for",
    "status_color",
    "classification_color",
]

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

#: Core palette. A calm, institutional "private-bank" feel: deep indigo ink,
#: warm paper surfaces, and a restrained accent set. Dark-first.
COLORS: dict[str, str] = {
    # Surfaces (dark, layered).
    "bg": "#0b1020",  # app background (deep navy ink)
    "surface": "#121a2e",  # cards / panels
    "surface_2": "#1a2440",  # raised elements, hover
    "surface_3": "#222e50",  # active / selected
    "border": "#2a3656",  # hairline borders
    # Text.
    "text": "#eef2ff",  # primary text
    "text_muted": "#9aa6c7",  # secondary text
    "text_faint": "#6b78a3",  # tertiary / captions
    # Brand.
    "brand": "#6d7cff",  # Saisei indigo
    "brand_soft": "#1c2550",  # brand-tinted surface
    # Semantic.
    "pass": "#27c08a",  # PASS / healthy
    "pass_soft": "#103a2c",  # pass-tinted surface
    "fail": "#ff5d6c",  # FAIL / blocker
    "fail_soft": "#3a1620",  # fail-tinted surface
    "warn": "#f4b740",  # needs attention
    "warn_soft": "#3a2e12",  # warn-tinted surface
    "info": "#48b9ff",  # informational
}

#: Border radii.
RADII: dict[str, str] = {
    "sm": "8px",
    "md": "12px",
    "lg": "18px",
    "xl": "24px",
    "pill": "999px",
}

#: Spacing scale (Radix-compatible string sizes also used directly in props).
SPACE: dict[str, str] = {
    "1": "4px",
    "2": "8px",
    "3": "12px",
    "4": "16px",
    "5": "24px",
    "6": "32px",
    "7": "48px",
}

#: Elevation shadows.
SHADOW: dict[str, str] = {
    "sm": "0 1px 2px rgba(0,0,0,0.30)",
    "md": "0 6px 20px rgba(0,0,0,0.35)",
    "lg": "0 16px 48px rgba(0,0,0,0.45)",
    "glow": "0 0 0 1px rgba(109,124,255,0.35), 0 8px 30px rgba(109,124,255,0.25)",
}

#: Typography.
FONT: dict[str, str] = {
    "sans": (
        "'Inter', 'Hiragino Sans', 'Noto Sans JP', -apple-system, "
        "BlinkMacSystemFont, 'Segoe UI', sans-serif"
    ),
    "mono": "'JetBrains Mono', 'SFMono-Regular', ui-monospace, monospace",
}


# ---------------------------------------------------------------------------
# Persona identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    """Visual + textual identity for an agent that speaks in the meeting room.

    Attributes:
        key: Stable identifier matching the backend ``persona`` string.
        name_en: English role label.
        name_ja: Japanese role label (主幹事銀行, etc.).
        kanji: One/two-character monogram shown inside the avatar.
        icon: Lucide icon name (rendered as a secondary glyph / fallback).
        accent: Hex accent color that brands this persona everywhere.
        accent_soft: Tinted surface color for this persona's bubble.
        tagline: Short description of the persona's concern (P0/P1/P2 lens).
    """

    key: str
    name_en: str
    name_ja: str
    kanji: str
    icon: str
    accent: str
    accent_soft: str
    tagline: str


#: Registry of all speaking agents. Keys match backend ``persona`` strings
#: (from ``CriticFeedback``) plus the chair and the system narrator. Each has a
#: distinct color + monogram so the banker identifies the voice instantly.
PERSONAS: dict[str, Persona] = {
    "main_bank": Persona(
        key="main_bank",
        name_en="Lead Bank",
        name_ja="主幹事銀行",
        kanji="主",
        icon="landmark",
        accent="#6d7cff",  # indigo — the anchor creditor
        accent_soft="#1c2550",
        tagline="Accountability · P1・役員責任・資産処分",
    ),
    "sub_bank": Persona(
        key="sub_bank",
        name_en="Syndicate Lender",
        name_ja="協調融資銀行",
        kanji="協",
        icon="scale",
        accent="#48b9ff",  # sky — fairness across lenders
        accent_soft="#0f2a3e",
        tagline="Fairness · P2・プロラタ負担",
    ),
    "guarantor": Persona(
        key="guarantor",
        name_en="Credit Guarantee Corp.",
        name_ja="信用保証協会",
        kanji="保",
        icon="shield-check",
        accent="#f4b740",  # amber — compliance / guarantee risk
        accent_soft="#3a2e12",
        tagline="Compliance · P0・回復計画",
    ),
    "lead_arranger": Persona(
        key="lead_arranger",
        name_en="Lead Arranger (Chair)",
        name_ja="取りまとめ役",
        kanji="幹",
        icon="gavel",
        accent="#27c08a",  # green — the consolidating chair
        accent_soft="#103a2c",
        tagline="Torimatome · 負担分担の取りまとめ",
    ),
    "system": Persona(
        key="system",
        name_en="Saisei Engine",
        name_ja="再生エンジン",
        kanji="再",
        icon="activity",
        accent="#9aa6c7",  # muted — system narration
        accent_soft="#1a2440",
        tagline="Assessment narration",
    ),
    "banker": Persona(
        key="banker",
        name_en="You (Banker)",
        name_ja="担当者",
        kanji="君",
        icon="user-round",
        accent="#eef2ff",  # bright — the human decider
        accent_soft="#222e50",
        tagline="The only real decider",
    ),
}


def persona_for(key: str | None) -> Persona:
    """Return the :class:`Persona` for a backend key, falling back to system."""
    return PERSONAS.get(str(key or "system"), PERSONAS["system"])


def status_color(status: str) -> str:
    """Map a critic verdict string to a semantic color."""
    s = str(status).upper()
    if s == "PASS":
        return COLORS["pass"]
    if s == "FAIL":
        return COLORS["fail"]
    return COLORS["warn"]


def classification_color(kanji: str) -> str:
    """Map an FSA classification kanji label to a semantic color.

    正常 (Normal) → green, 要注意 (Substandard) → amber,
    要管理 (Doubtful) → red. Unknown → muted.
    """
    if kanji == "正常":
        return COLORS["pass"]
    if kanji == "要注意":
        return COLORS["warn"]
    if kanji == "要管理":
        return COLORS["fail"]
    return COLORS["text_muted"]
