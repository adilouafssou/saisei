"""Deterministic numeric-preservation verifier for the Keikakusho polish step.

Feature 1 (LangSmith eval), slice 1: the **deterministic gate that must pass
before any LLM-as-judge ever runs**. Per the project's one inviolable rule, the
LLM may improve prose but must NEVER add, drop, or alter a number. This module
turns that rule into an executable, offline, dependency-free check.

Why a value-based check (not a string diff)
-------------------------------------------
``polish_keikakusho`` explicitly allows the LLM to improve readability, so it
may legitimately reformat a figure (e.g. ``\u00a5150,000,000`` -> ``150,000,000\u5186``
or ``\u00a5150\u767e\u4e07`` is NOT allowed because it changes the rendered value, but
``150,000,000\u5186`` keeps it). A naive string diff would false-positive on benign
reformatting and false-negative on a changed digit hidden by reformatting.

Instead we extract the **numeric value** of every yen figure from both texts
into a multiset and require the multisets to be equal:

- benign reformatting (``\u00a5`` -> ``\u5186``, comma changes) is tolerated, because the
  underlying integer value is unchanged;
- any added, dropped, duplicated, or altered figure changes the multiset and is
  caught loudly.

The verifier is pure and deterministic: same inputs -> same result, no network,
no LLM, no third-party dependencies (stdlib ``re`` / ``collections`` only).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

__all__ = [
    "NumericPreservationResult",
    "extract_yen_values",
    "check_numbers_preserved",
    "guard_polished_text",
]

# ---------------------------------------------------------------------------
# Yen-figure extraction
# ---------------------------------------------------------------------------

#: Matches a yen figure as rendered by ``app.shared.models.money.format_jpy``
#: (``\u00a5150,000,000`` / ``-\u00a5150,000,000``) and the common reformattings a
#: readability pass may produce: a trailing ``\u5186`` instead of a leading ``\u00a5``,
#: full-width ``\uffe5``, and figures with or without thousands separators.
#:
#: Group 1 = optional leading sign; group 2 = the digit/comma body.
#: A figure must carry a currency marker (\u00a5 / \uffe5 / \u5186) so we never sweep up
#: incidental integers (years, list indices, percentages) as money.
_YEN_PATTERN = re.compile(
    r"(-?)"  # optional minus sign
    r"(?:\u00a5|\uffe5)?"  # optional leading yen sign (half/full width)
    r"(\d{1,3}(?:,\d{3})+|\d+)"  # digits, optionally comma-grouped
    r"\s*\u5186?"  # optional trailing \u5186
)

#: A currency marker must be present somewhere in the match for it to count as
#: a yen figure (prevents matching bare integers like a year or a count).
_CURRENCY_MARKERS = ("\u00a5", "\uffe5", "\u5186")


def extract_yen_values(text: str) -> list[int]:
    """Extract every yen figure from ``text`` as a signed integer value.

    Only tokens carrying a currency marker (``\u00a5``, ``\uffe5``, or ``\u5186``) are
    treated as money, so incidental integers (years, indices, percentages) are
    ignored. Thousands separators and the currency marker are stripped; the sign
    is preserved.

    Args:
        text: The Markdown (or any) text to scan.

    Returns:
        A list of signed integer yen values, in order of appearance.
    """
    values: list[int] = []
    for match in _YEN_PATTERN.finditer(text):
        token = match.group(0)
        if not any(marker in token for marker in _CURRENCY_MARKERS):
            # No currency marker -> not a yen figure (e.g. a bare year). Skip.
            continue
        sign = -1 if match.group(1) == "-" else 1
        digits = match.group(2).replace(",", "")
        if not digits:
            continue
        values.append(sign * int(digits))
    return values


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumericPreservationResult:
    """Outcome of a numeric-preservation check.

    Attributes:
        preserved: True iff the polished text contains exactly the same multiset
            of yen values as the original (no figure added, dropped, duplicated,
            or altered).
        original_values: The yen values found in the original draft.
        polished_values: The yen values found in the polished draft.
        missing: Values present in the original but missing/reduced in the
            polished text (dropped or altered figures).
        added: Values present in the polished text but absent/extra vs. the
            original (hallucinated or duplicated figures).
    """

    preserved: bool
    original_values: list[int] = field(default_factory=list)
    polished_values: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    added: list[int] = field(default_factory=list)

    def reason(self) -> str:
        """Return a human-readable explanation (empty when preserved)."""
        if self.preserved:
            return ""
        parts: list[str] = []
        if self.missing:
            parts.append(f"dropped/altered figures: {sorted(self.missing)}")
        if self.added:
            parts.append(f"added/hallucinated figures: {sorted(self.added)}")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Check + guard
# ---------------------------------------------------------------------------


def check_numbers_preserved(original: str, polished: str) -> NumericPreservationResult:
    """Check that ``polished`` preserves every yen figure in ``original``.

    Pure and deterministic. Compares the multiset of yen *values* (not literal
    strings) so benign reformatting is tolerated while any added, dropped,
    duplicated, or altered figure is reported.

    Args:
        original: The deterministic Keikakusho draft (the source of truth).
        polished: The LLM-polished draft to verify.

    Returns:
        A :class:`NumericPreservationResult` describing the outcome.
    """
    original_values = extract_yen_values(original)
    polished_values = extract_yen_values(polished)

    original_counts = Counter(original_values)
    polished_counts = Counter(polished_values)

    # Multiset difference both ways.
    missing_counter = original_counts - polished_counts
    added_counter = polished_counts - original_counts

    missing = list(missing_counter.elements())
    added = list(added_counter.elements())
    preserved = not missing and not added

    return NumericPreservationResult(
        preserved=preserved,
        original_values=original_values,
        polished_values=polished_values,
        missing=missing,
        added=added,
    )


def guard_polished_text(original: str, polished: str) -> tuple[str, NumericPreservationResult]:
    """Return the polished text only if it preserves every figure, else the original.

    This is the fail-safe gate for the polish step: a readability pass must never
    be allowed to change a number in a regulated credit document. When the check
    fails, the deterministic ``original`` is returned unchanged so the workflow
    keeps a correct, auditable document (mirroring the best-effort contract of
    ``polish_keikakusho``: the polish never breaks the workflow, and now it can
    never silently corrupt a figure either).

    Args:
        original: The deterministic Keikakusho draft (source of truth).
        polished: The LLM-polished candidate.

    Returns:
        A tuple of (text_to_use, result). ``text_to_use`` is ``polished`` when
        the check passes, otherwise ``original``.
    """
    result = check_numbers_preserved(original, polished)
    return (polished if result.preserved else original), result
