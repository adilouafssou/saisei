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
    "COLORS_LIGHT",
    "COLORS_DARK",
    "THEME_CSS",
    "RADII",
    "SPACE",
    "SHADOW",
    "GRADIENT",
    "FONT",
    "TYPE",
    "FOCUS_RING",
    "BUBBLE_IN",
    "TABLE_STYLE",
    "Persona",
    "PERSONAS",
    "persona_for",
    "status_color",
    "classification_color",
    "ews_color",
    "score_color",
]

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

#: Core palette — LIGHT "regenerative finance" theme (default).
#:
#: Design thesis: the product heals SMEs through regenerative finance, so the
#: visual language is warm, light, and calm — not a dark trading terminal. The
#: surfaces are WARM paper (not clinical pure white, which reads cold/cheap),
#: the brand is a regenerative green (growth / healing) balanced by a trust-blue
#: secondary (institutional credibility). Restraint and whitespace over
#: flashiness — the premium, human feel of best-in-class product sites.
COLORS: dict[str, str] = {
    # Surfaces (light, warm, layered). Warm paper > pure white.
    "bg": "#fbfaf7",  # app background (warm paper)
    "surface": "#ffffff",  # cards / panels (clean white on paper)
    "surface_2": "#f4f2ec",  # raised elements, hover (warm sand)
    "surface_3": "#eae7df",  # active / selected
    "border": "#e3e0d8",  # hairline borders (warm)
    # Text (warm near-black ink; AA-contrast steps on paper).
    "text": "#1d2530",  # primary text (warm ink)
    "text_muted": "#5b6573",  # secondary text
    "text_faint": "#8b94a3",  # tertiary / captions
    # Brand — regenerative green + trust-blue secondary.
    "brand": "#1f8f6a",  # Saisei regenerative green
    "brand_soft": "#e3f3ec",  # brand-tinted surface (mint paper)
    "brand_2": "#2f6df0",  # trust blue (secondary / institutional)
    "brand_2_soft": "#e7eefe",  # blue-tinted surface
    # --- Semantic CHROME aliases (the color-usage RULE, encoded) -------------
    # Identity decision: blue-led chrome, green reserved for the brand mark and
    # POSITIVE / recovery states, on a warm-white canvas. Components should
    # reference these intent aliases instead of picking brand vs brand_2 ad hoc,
    # so the rule is enforced in one place:
    #   chrome      -> blue   (structural: section icons, headings accents, nav)
    #   chrome_soft -> blue tint (informational surfaces)
    #   positive    -> green  (brand mark, primary CTA, PASS / 正常先 / uplift)
    # Green is "earned" by good outcomes; do not paint neutral chrome green.
    "chrome": "#2f6df0",  # = brand_2 (blue) — structural chrome
    "chrome_soft": "#e7eefe",  # = brand_2_soft
    "positive": "#1f8f6a",  # = brand (green) — brand + positive states
    "positive_soft": "#e3f3ec",  # = brand_soft
    # Semantic — re-tuned for legibility on light paper.
    "pass": "#1f8f6a",  # PASS / healthy (regenerative green)
    "pass_soft": "#e3f3ec",  # pass-tinted surface
    "fail": "#d8453f",  # FAIL / blocker (clear, not neon)
    "fail_soft": "#fbe7e6",  # fail-tinted surface
    "warn": "#c8881a",  # needs attention (legible amber on paper)
    "warn_soft": "#fbf0da",  # warn-tinted surface
    "info": "#2f6df0",  # informational (trust blue)
}

#: DARK palette — preserved so dark mode is not lost. Swap COLORS = COLORS_DARK
#: (and app.py appearance to "dark") to restore the original look.
COLORS_DARK: dict[str, str] = {
    "bg": "#0b1020",
    "surface": "#121a2e",
    "surface_2": "#1a2440",
    "surface_3": "#222e50",
    "border": "#2a3656",
    "text": "#eef2ff",
    "text_muted": "#9aa6c7",
    "text_faint": "#6b78a3",
    "brand": "#6d7cff",
    "brand_soft": "#1c2550",
    "brand_2": "#48b9ff",
    "brand_2_soft": "#0f2a3e",
    "pass": "#27c08a",
    "pass_soft": "#103a2c",
    "fail": "#ff5d6c",
    "fail_soft": "#3a1620",
    "warn": "#f4b740",
    "warn_soft": "#3a2e12",
    "info": "#48b9ff",
    "chrome": "#48b9ff",
    "chrome_soft": "#0f2a3e",
    "positive": "#27c08a",
    "positive_soft": "#103a2c",
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
#: Extended with larger steps (8/9) for the generous, breathing whitespace that
#: gives the workspace a premium, uncramped feel.
SPACE: dict[str, str] = {
    "1": "4px",
    "2": "8px",
    "3": "12px",
    "4": "16px",
    "5": "24px",
    "6": "32px",
    "7": "48px",
    "8": "64px",
    "9": "96px",
}

#: Elevation shadows — soft, low-alpha, warm/brand-tinted for LIGHT surfaces.
#: Heavy black shadows look like dirty smudges on paper; these use a warm ink
#: tint at low opacity for a clean, premium lift. The brand "glow" uses the
#: regenerative green so primary actions feel alive without shouting.
SHADOW: dict[str, str] = {
    "sm": "0 1px 2px rgba(29,37,48,0.06)",
    "md": "0 6px 20px rgba(29,37,48,0.08)",
    "lg": "0 16px 48px rgba(29,37,48,0.12)",
    "glow": "0 0 0 1px rgba(31,143,106,0.25), 0 8px 24px rgba(31,143,106,0.18)",
}

#: Brand gradients — the green->blue dual-brand signature. Green (regenerative
#: growth) flows into trust-blue (institutional credibility), giving the product
#: a premium, intentional identity instead of a flat single-color square. Use
#: ``GRADIENT["brand"]`` on the brand mark and primary CTA; ``GRADIENT["hero"]``
#: is a soft tinted wash for hero / header surfaces.
GRADIENT: dict[str, str] = {
    "brand": "linear-gradient(135deg, #1f8f6a 0%, #2f6df0 100%)",
    "brand_hover": "linear-gradient(135deg, #1b7e5d 0%, #2a61d6 100%)",
    "hero": ("linear-gradient(135deg, rgba(31,143,106,0.10) 0%, rgba(47,109,240,0.10) 100%)"),
}

#: Typography.
FONT: dict[str, str] = {
    "sans": (
        "'Inter', 'Hiragino Sans', 'Noto Sans JP', -apple-system, "
        "BlinkMacSystemFont, 'Segoe UI', sans-serif"
    ),
    "mono": "'JetBrains Mono', 'SFMono-Regular', ui-monospace, monospace",
}

#: Modular type scale — the single source of truth for sizing/weight/leading.
#: A deliberate scale (instead of ad-hoc per-component Radix ``size=``) is what
#: gives the UI a calm, consistent rhythm. Each entry is a CSS-ready dict that
#: can be splatted into a component ``style=``:
#:
#:     rx.heading("...", style=TYPE["h2"])
#:
#: Steps (1.250 "major third" ratio, tuned for a dense data UI):
#:   display → hero / page brand
#:   h1/h2/h3 → section headings
#:   body     → default reading size
#:   small    → secondary / metadata
#:   caption  → labels / captions (uppercase tracking handled per-use)
TYPE: dict[str, dict[str, str]] = {
    "display": {
        "fontSize": "34px",
        "fontWeight": "800",
        "lineHeight": "1.15",
        "letterSpacing": "-0.02em",
    },
    "h1": {
        "fontSize": "27px",
        "fontWeight": "700",
        "lineHeight": "1.2",
        "letterSpacing": "-0.015em",
    },
    "h2": {
        "fontSize": "21px",
        "fontWeight": "700",
        "lineHeight": "1.25",
        "letterSpacing": "-0.01em",
    },
    "h3": {"fontSize": "17px", "fontWeight": "600", "lineHeight": "1.3"},
    "body": {"fontSize": "14px", "fontWeight": "400", "lineHeight": "1.6"},
    "small": {"fontSize": "13px", "fontWeight": "400", "lineHeight": "1.5"},
    "caption": {
        "fontSize": "11px",
        "fontWeight": "600",
        "lineHeight": "1.4",
        "letterSpacing": "0.04em",
    },
}

#: Focus ring — a single accessible focus-visible outline used across
#: interactive elements (keyboard navigation / WCAG). Brand-tinted so focus is
#: clearly visible on warm paper without looking like an error state.
FOCUS_RING: str = "0 0 0 3px rgba(31,143,106,0.35)"

#: Shared table style — Radix tables otherwise inherit their own light-gray
#: theme tokens, which render as faint gray-on-paper and are hard to read. This
#: forces the Saisei ink color on cells, a warm raised header, and hairline
#: borders so every data table is legible on the light surface. Splat into a
#: table.root ``style=``:  rx.table.root(..., style=TABLE_STYLE).
TABLE_STYLE: dict[str, object] = {
    "color": COLORS["text"],
    "fontSize": "14px",
    "borderRadius": RADII["md"],
    "overflow": "hidden",
    "& th": {
        "color": COLORS["text"],
        "fontWeight": "600",
        "background": COLORS["surface_2"],
        "borderBottom": f"1px solid {COLORS['border']}",
    },
    "& td, & th[scope='row']": {
        "color": COLORS["text"],
        "borderColor": COLORS["border"],
    },
    "& tbody tr:hover": {"background": COLORS["surface_2"]},
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
        accent="#2f6df0",  # trust blue — the anchor creditor
        accent_soft="#e7eefe",
        tagline="Accountability · P1・役員責任・資産処分",
    ),
    "sub_bank": Persona(
        key="sub_bank",
        name_en="Syndicate Lender",
        name_ja="協調融資銀行",
        kanji="協",
        icon="scale",
        accent="#2aa3c4",  # teal — fairness across lenders
        accent_soft="#e2f4f8",
        tagline="Fairness · P2・プロラタ負担",
    ),
    "guarantor": Persona(
        key="guarantor",
        name_en="Credit Guarantee Corp.",
        name_ja="信用保証協会",
        kanji="保",
        icon="shield-check",
        accent="#c8881a",  # amber — compliance / guarantee risk
        accent_soft="#fbf0da",
        tagline="Compliance · P0・回復計画",
    ),
    "lead_arranger": Persona(
        key="lead_arranger",
        name_en="Lead Arranger (Chair)",
        name_ja="取りまとめ役",
        kanji="幹",
        icon="gavel",
        accent="#1f8f6a",  # regenerative green — the consolidating chair
        accent_soft="#e3f3ec",
        tagline="Torimatome · 負担分担の取りまとめ",
    ),
    "system": Persona(
        key="system",
        name_en="Saisei",
        name_ja="再生",
        kanji="再",
        icon="activity",
        accent="#8b94a3",  # muted — system narration
        accent_soft="#f4f2ec",
        tagline="Assessment narration",
    ),
    "banker": Persona(
        key="banker",
        name_en="You (Banker)",
        name_ja="担当者",
        kanji="君",
        icon="user-round",
        accent="#1d2530",  # ink — the human decider (visible on paper)
        accent_soft="#eae7df",
        tagline="The only real decider",
    ),
}


def persona_for(key: str | None) -> Persona:
    """Return the :class:`Persona` for a backend key, falling back to system."""
    return PERSONAS.get(str(key or "system"), PERSONAS["system"])


# ---------------------------------------------------------------------------
# Runtime light/dark theming via CSS variables
# ---------------------------------------------------------------------------
#
# Reflex compiles component styles at build time, so a Python ``if dark`` cannot
# switch colors at runtime. Instead we publish every palette key as a CSS custom
# property and let the browser swap values when Radix toggles the color-mode
# class on <html>. This keeps the redesign a one-file change AND requires zero
# edits at the ~hundreds of ``COLORS[key]`` call sites: each now resolves to
# ``var(--saisei-key)``, a valid CSS value everywhere a hex was used before.
#
# Identity is preserved in BOTH modes: the green/blue ``chrome``/``positive``
# rule lives in both palettes, so dark mode is a surface change, not a rebrand.

#: The light palette is the authoritative source for variable NAMES. Both
#: palettes share the same keys (COLORS_DARK mirrors COLORS), so iterating the
#: light keys is sufficient.
_LIGHT_SOURCE: dict[str, str] = dict(COLORS)


def _css_vars(palette: dict[str, str]) -> str:
    """Render a palette as ``--saisei-<key>: <value>;`` declarations."""
    return "".join(f"--saisei-{k}:{v};" for k, v in palette.items())


#: Global stylesheet injected once via ``rx.App(style=...)`` /
#: ``add_page(on_load=...)``; see ``app.py``. Light values live on ``:root`` so
#: they are the default even before hydration; the ``.dark`` class (set by Radix
#: ``rx.color_mode``) overrides them. ``color-scheme`` hints native form
#: controls/scrollbars so they match the surface in each mode.
THEME_CSS: str = (
    ":root{color-scheme:light;" + _css_vars(_LIGHT_SOURCE) + "}"
    ".dark{color-scheme:dark;" + _css_vars(COLORS_DARK) + "}"
    # Meeting-room bubble entrance: a soft fade + upward slide so each streamed
    # transcript event animates in as it lands, giving the creditor meeting a
    # live, turn-by-turn feel instead of a static all-at-once render. Honour the
    # OS "reduce motion" setting for accessibility.
    "@keyframes saisei-bubble-in{"
    "from{opacity:0;transform:translateY(8px);}"
    "to{opacity:1;transform:translateY(0);}"
    "}"
    "@keyframes saisei-dots{"
    "0%,80%,100%{opacity:0.25;}40%{opacity:1;}"
    "}"
    # Feature 5 recovery-marker pulse: the halo on the EWS<40 crossing month
    # gently expands + fades, drawing the eye to the recovery point. Honour
    # reduce-motion below.
    "@keyframes saisei-recovery-pulse{"
    "0%{transform:scale(0.8);opacity:0.45;}"
    "70%{transform:scale(1.9);opacity:0;}"
    "100%{transform:scale(0.8);opacity:0;}"
    "}"
    ".saisei-recovery-pulse{"
    "transform-box:fill-box;transform-origin:center;"
    "animation:saisei-recovery-pulse 2s ease-out infinite;"
    "}"
    # Feature 5 chart hover tooltips (pure CSS, no state round-trip): each
    # month's tooltip elements (class saisei-chart-tip) are hidden by default
    # and revealed when their group (saisei-chart-hover) is hovered. The wide
    # invisible band inside the group is the cursor target.
    ".saisei-chart-tip{opacity:0;transition:opacity 0.12s ease-out;"
    "pointer-events:none;}"
    ".saisei-chart-hover:hover .saisei-chart-tip{opacity:1;}"
    "@media (prefers-reduced-motion: reduce){"
    ".saisei-bubble-in{animation:none !important;}"
    ".saisei-recovery-pulse{animation:none !important;}"
    ".saisei-float{animation:none !important;}"
    ".saisei-summon{animation:none !important;}"
    "}"
    # Saisei companion (the summonable 再生の精 / spirit). The dock orb gently
    # bobs (float) and its halo breathes (aura) so it reads as a living entity
    # the banker can summon; the chat window scales up from the orb on open
    # (summon). All three are honoured by the reduce-motion query above, and the
    # orb remains a fully-functional, predictably-anchored button without them.
    "@keyframes saisei-float{"
    "0%,100%{transform:translateY(0);}50%{transform:translateY(-6px);}"
    "}"
    "@keyframes saisei-aura{"
    "0%,100%{opacity:0.35;transform:scale(1);}"
    "50%{opacity:0.6;transform:scale(1.12);}"
    "}"
    "@keyframes saisei-summon{"
    "from{opacity:0;transform:translateY(12px) scale(0.92);}"
    "to{opacity:1;transform:translateY(0) scale(1);}"
    "}"
    ".saisei-float{animation:saisei-float 3.2s ease-in-out infinite;}"
    ".saisei-aura{animation:saisei-aura 3.2s ease-in-out infinite;}"
    ".saisei-summon{animation:saisei-summon 0.28s ease-out both;}"
    # Print isolation for the Keikakusho PDF (正式版) export. When the banker
    # triggers window.print(), hide everything except the element tagged
    # ``saisei-print-region`` (the rendered document) so the saved PDF is the
    # plan only, with no app chrome. Reset positioning/colors so the document
    # prints clean on white paper regardless of theme.
    "@media print{"
    "body *{visibility:hidden !important;}"
    ".saisei-print-region, .saisei-print-region *{visibility:visible !important;}"
    ".saisei-print-region{position:absolute !important;left:0 !important;"
    "top:0 !important;width:100% !important;box-shadow:none !important;"
    "border:none !important;background:#ffffff !important;color:#000000 !important;}"
    "}"
)

#: Entrance-animation style for a meeting bubble. Splat into the bubble wrapper
#: ``style=`` (and add the ``class_name="saisei-bubble-in"`` so the reduce-motion
#: media query can disable it). The animation is purely cosmetic.
BUBBLE_IN: dict[str, str] = {
    "animation": "saisei-bubble-in 0.35s ease-out both",
}

# Rebind COLORS so every existing ``COLORS[key]`` reference resolves to the CSS
# variable (which the browser fills from :root or .dark). The original hex maps
# remain available as ``COLORS_LIGHT`` / ``COLORS_DARK`` for the var generator
# above and for any place that needs a concrete value (e.g. gradients).
COLORS_LIGHT: dict[str, str] = dict(COLORS)
COLORS = {k: f"var(--saisei-{k})" for k in _LIGHT_SOURCE}

# TABLE_STYLE and FOCUS_RING were built from the original (hex) COLORS before the
# rebind above, so they captured light-only values and would NOT follow a theme
# switch. Rebuild TABLE_STYLE against the var-based COLORS so data tables flip
# with the rest of the UI. (FOCUS_RING is a brand-green ring that reads fine in
# both modes, so it is left as-is.)
TABLE_STYLE = {
    "color": COLORS["text"],
    "fontSize": "14px",
    "borderRadius": RADII["md"],
    "overflow": "hidden",
    "& th": {
        "color": COLORS["text"],
        "fontWeight": "600",
        "background": COLORS["surface_2"],
        "borderBottom": f"1px solid {COLORS['border']}",
    },
    "& td, & th[scope='row']": {
        "color": COLORS["text"],
        "borderColor": COLORS["border"],
    },
    "& tbody tr:hover": {"background": COLORS["surface_2"]},
}


def status_color(status: str) -> str:
    """Map a critic verdict string to a semantic color."""
    s = str(status).upper()
    if s == "PASS":
        return COLORS["pass"]
    if s == "FAIL":
        return COLORS["fail"]
    return COLORS["warn"]


#: Five-stop health gradient, ordered worst -> best. A continuous score is
#: snapped to the nearest stop so a banker reads health as colour instantly:
#: deep red (critical) -> orange -> amber -> lime -> regenerative green (healthy).
#: These are concrete hex values (not CSS vars) because they are interpolated
#: by value at call sites and must resolve to a paintable colour in both modes;
#: the palette reads well on warm paper and on the dark surface alike.
_HEALTH_STOPS: tuple[str, ...] = (
    "#d8453f",  # critical (deep red)
    "#e8703a",  # poor (orange)
    "#c8881a",  # caution (amber)
    "#6fae3d",  # improving (lime)
    "#1f8f6a",  # healthy (regenerative green)
)


def _gradient_pick(fraction: float) -> str:
    """Return the health-stop colour for ``fraction`` in [0, 1] (0=worst)."""
    f = max(0.0, min(1.0, fraction))
    idx = int(round(f * (len(_HEALTH_STOPS) - 1)))
    return _HEALTH_STOPS[idx]


def ews_color(score: float, *, vmin: float = 0.0, vmax: float = 100.0) -> str:
    """Map an EWS score to a health colour (HIGHER score = WORSE = redder).

    The EWS rises as the borrower deteriorates, so the gradient is inverted: a
    low score paints green (healthy) and a high score paints red (critical),
    letting the banker grasp severity at a glance instead of reading the digits.
    Display-only: it colours a value state already produced, it computes nothing.
    """
    span = vmax - vmin if vmax > vmin else 1.0
    norm = (float(score) - vmin) / span
    return _gradient_pick(1.0 - norm)  # invert: high EWS -> red


def score_color(score: float, *, vmin: float = 0.0, vmax: float = 100.0) -> str:
    """Map a 0-100 "higher is better" score (e.g. guarantee-release) to a colour.

    Unlike :func:`ews_color`, a high score is GOOD here, so the gradient is not
    inverted: low paints red, high paints regenerative green. Display-only.
    """
    span = vmax - vmin if vmax > vmin else 1.0
    norm = (float(score) - vmin) / span
    return _gradient_pick(norm)


def classification_color(kanji: str) -> str:
    """Map an FSA classification kanji label to a semantic color.

    Five-category mapping (金融検査マニュアル):
        正常先 (Normal)          → green
        要注意先 (Needs Attention) → amber
        破綻懸念先 (In Danger)    → orange-red (fail)
        実質破綻先 (De facto Bankrupt) → deep red (fail)
        破綻先 (Bankrupt)        → deep red (fail)
    Unknown → muted.

    Display-only: never computes a verdict or number.
    """
    if kanji == "正常先":
        return COLORS["pass"]
    if kanji == "要注意先":
        return COLORS["warn"]
    if kanji in ("破綻懸念先", "実質破綻先", "破綻先"):
        return COLORS["fail"]
    return COLORS["text_muted"]
