"""Verifier for Japanese-accounting negative money in the Shisanhyo parser.

Japanese trial balances denote losses with the 三角 markers △ (U+25B3) /
▲ (U+25B2) or with accounting parentheses, e.g. ``(1,000)``. Before the fix,
``_parse_money`` only understood a leading ASCII '-', so a loss cell parsed as
non-numeric and was silently dropped (and a loss in a mandatory field skipped
the whole row). This pins the parsed sign, fully offline.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.backend.tools.shisanhyo_parser import _parse_money, parse_shisanhyo


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("\u25b31,000", -1000),  # △1,000
        ("\u25b2 1,000", -1000),  # ▲ 1,000 (with space)
        ("(1,000)", -1000),  # accounting parentheses
        ("(5,000,000)", -5_000_000),
        ("\u00a5\u25b3250", -250),  # ¥△250 (yen sign + marker)
        ("-1,000", -1000),  # plain ASCII minus still works
        ("1,000", 1000),  # positive unaffected
    ],
)
def test_parse_money_japanese_negatives(cell: str, expected: int) -> None:
    warnings: list[str] = []
    assert _parse_money(cell, "keijo_rieki", 2, warnings) == expected
    assert warnings == []


def test_negative_loss_row_is_kept_not_dropped() -> None:
    """A CSV row whose SG&A is a △ figure must parse, not be skipped."""
    csv_bytes = (
        "period,uriage,uriage_genka,hanbaihi,eigai_shueki,eigai_hiyo\n"
        "2025-04,1000,800,\u25b3500,0,0\n"
    ).encode("utf-8")
    parsed = parse_shisanhyo(csv_bytes, "tb.csv")
    assert len(parsed.rows) == 1
    row = parsed.rows[0]
    assert row.period == dt.date(2025, 4, 30)
    assert int(row.hanbaihi) == -500
    # 粗利 200 − 販売費(△500) = 営業利益 700; no non-op items -> keijo 700.
    assert row.eigyo_rieki == 700


def test_fractional_japanese_negative_still_rejected() -> None:
    """The strict no-fraction contract holds after sign normalisation."""
    warnings: list[str] = []
    assert _parse_money("\u25b31,000.5", "uriage", 2, warnings) is None
    assert warnings  # a warning was surfaced
