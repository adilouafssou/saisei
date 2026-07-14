"""Verifier for money_sign — the colour rule behind money_cell (data_display).

No CI here, so this pins the financial-table colour contract on plain strings
(no Reflex runtime): negatives read as a loss, genuine positive figures as
healthy, and ¥0 / the "—" not-assessed placeholder / empty stay NEUTRAL — never
painted positive green, which is the bug this fixes.
"""

from __future__ import annotations

import pytest
from app.frontend.components.data_display import money_sign
from app.shared.models.money import format_jpy


@pytest.mark.parametrize(
    "amount",
    [-1, -1000, -5_000_000, -150_000_000],
)
def test_negative_figures_are_negative(amount: int) -> None:
    assert money_sign(format_jpy(amount)) == "negative"


@pytest.mark.parametrize(
    "amount",
    [1, 1000, 5_000_000, 150_000_000],
)
def test_positive_figures_are_positive(amount: int) -> None:
    assert money_sign(format_jpy(amount)) == "positive"


def test_zero_is_neutral_not_positive() -> None:
    """¥0 must be neutral — zero is not a positive (green) outcome."""
    assert money_sign(format_jpy(0)) == "neutral"


@pytest.mark.parametrize("placeholder", ["\u2014", "", "   ", "未評価"])
def test_non_figure_placeholders_are_neutral(placeholder: str) -> None:
    """The not-assessed em-dash / empty / label placeholders stay neutral."""
    assert money_sign(placeholder) == "neutral"


def test_zero_with_decimals_style_still_neutral() -> None:
    """A '¥0' with grouping artefacts is still neutral (only digit is 0)."""
    assert money_sign("\u00a50") == "neutral"
    assert money_sign("0\u5186") == "neutral"


def test_trailing_yen_form_is_classified() -> None:
    """A reformatted '1,000円' (trailing 円) is still a positive figure."""
    assert money_sign("1,000\u5186") == "positive"
    assert money_sign("-1,000\u5186") == "negative"
