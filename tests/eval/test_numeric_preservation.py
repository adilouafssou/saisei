"""Offline tests for the Keikakusho numeric-preservation verifier.

Feature 1 (LangSmith eval), slice 1. CI-gated, deterministic, no network.

The verifier is the gate that enforces the project's one inviolable rule on the
LLM polish step: prose may change, numbers may not. These tests drive it against
the REAL ``render_keikakusho`` output so the figures under test are the actual
ones the product renders, not hand-invented strings.
"""

from __future__ import annotations

import datetime as dt

from app.backend.analysis.numeric_preservation import (
    check_numbers_preserved,
    extract_yen_values,
    guard_polished_text,
)
from app.backend.nodes.kaizen_generation import render_keikakusho
from app.backend.state import Strategy
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass

# ---------------------------------------------------------------------------
# Shared fixture: a real rendered Keikakusho draft
# ---------------------------------------------------------------------------


def _draft() -> str:
    """Render a real Keikakusho draft with known, distinct figures."""
    latest = TrialBalance(
        period=dt.date(2025, 5, 31),
        uriage=138_000_000,
        uriage_genka=115_000_000,
        hanbaihi=21_000_000,
    )
    strategy = Strategy(
        title="原価低減（COGS reduction）",
        rationale="仕入先の多角化と歩留まり改善により原価を削減する。",
        expected_keijo_uplift=5_000_000,
    )
    return render_keikakusho(
        company_name="愛知精密製作所株式会社",
        hojin_bango="1234567890123",
        fsa_kanji=FsaClass.YOCHUISAKI.kanji,
        latest=latest,
        strategy=strategy,
        working_capital_gap=-1_000_000,
    )


# ---------------------------------------------------------------------------
# extract_yen_values
# ---------------------------------------------------------------------------


class TestExtractYenValues:
    def test_extracts_formatted_yen(self) -> None:
        vals = extract_yen_values("売上 ¥138,000,000 原価 ¥115,000,000")
        assert vals == [138_000_000, 115_000_000]

    def test_extracts_negative_yen(self) -> None:
        vals = extract_yen_values("資金繰りギャップ: -¥1,000,000")
        assert vals == [-1_000_000]

    def test_tolerates_trailing_en_marker(self) -> None:
        # A readability pass may rewrite ¥150,000,000 as 150,000,000円.
        assert extract_yen_values("150,000,000円") == [150_000_000]

    def test_tolerates_fullwidth_yen(self) -> None:
        assert extract_yen_values("￥150,000,000") == [150_000_000]

    def test_ignores_bare_integers_without_currency_marker(self) -> None:
        # Years, list indices, employee counts, percentages must NOT count.
        text = "2025年 84人 3% 項目1. 四半期"
        assert extract_yen_values(text) == []

    def test_draft_contains_expected_figures(self) -> None:
        vals = extract_yen_values(_draft())
        # keijo_rieki = 138M - 115M - 21M = 2_000_000 is rendered too.
        assert 138_000_000 in vals
        assert 115_000_000 in vals
        assert 21_000_000 in vals
        assert 2_000_000 in vals
        assert 5_000_000 in vals
        assert -1_000_000 in vals


# ---------------------------------------------------------------------------
# check_numbers_preserved
# ---------------------------------------------------------------------------


class TestCheckNumbersPreserved:
    def test_identity_is_preserved(self) -> None:
        draft = _draft()
        result = check_numbers_preserved(draft, draft)
        assert result.preserved
        assert result.missing == []
        assert result.added == []
        assert result.reason() == ""

    def test_benign_reformatting_is_preserved(self) -> None:
        # Rewrite ¥N as N円 on known figures: the rendered VALUES are unchanged,
        # so the value-based check must still pass.
        draft = _draft()
        polished = draft.replace("¥138,000,000", "138,000,000円")
        polished = polished.replace("¥115,000,000", "115,000,000円")
        result = check_numbers_preserved(draft, polished)
        assert result.preserved, result.reason()

    def test_prose_change_without_figure_change_is_preserved(self) -> None:
        draft = _draft()
        polished = draft.replace(
            "## 3. 実行計画（Action plan）",
            "## 3. 実行計画（Action plan / 実施ロードマップ）",
        )
        assert polished != draft
        result = check_numbers_preserved(draft, polished)
        assert result.preserved

    def test_dropped_figure_is_caught(self) -> None:
        draft = _draft()
        # Remove the working-capital gap figure entirely.
        polished = draft.replace("-¥1,000,000", "—")
        result = check_numbers_preserved(draft, polished)
        assert not result.preserved
        assert -1_000_000 in result.missing
        assert "dropped" in result.reason()

    def test_altered_figure_is_caught(self) -> None:
        draft = _draft()
        # Change 138,000,000 -> 188,000,000 (a hallucinated digit).
        polished = draft.replace("¥138,000,000", "¥188,000,000")
        result = check_numbers_preserved(draft, polished)
        assert not result.preserved
        assert 138_000_000 in result.missing
        assert 188_000_000 in result.added

    def test_hallucinated_extra_figure_is_caught(self) -> None:
        draft = _draft()
        polished = draft + "\n補足: 追加融資 ¥99,000,000 を希望。"
        result = check_numbers_preserved(draft, polished)
        assert not result.preserved
        assert 99_000_000 in result.added
        assert "added" in result.reason()

    def test_duplicated_figure_is_caught(self) -> None:
        draft = _draft()
        # Duplicate an existing figure -> multiset count differs.
        polished = draft + "\n再掲: ¥5,000,000"
        result = check_numbers_preserved(draft, polished)
        assert not result.preserved
        assert 5_000_000 in result.added


# ---------------------------------------------------------------------------
# guard_polished_text
# ---------------------------------------------------------------------------


class TestGuardPolishedText:
    def test_returns_polished_when_preserved(self) -> None:
        draft = _draft()
        polished = draft.replace(
            "銀行と四半期ごとにレビューを実施する。",
            "銀行と四半期ごとに進捗レビューを実施する。",
        )
        text, result = guard_polished_text(draft, polished)
        assert result.preserved
        assert text == polished

    def test_falls_back_to_original_when_figure_altered(self) -> None:
        draft = _draft()
        polished = draft.replace("¥115,000,000", "¥125,000,000")
        text, result = guard_polished_text(draft, polished)
        assert not result.preserved
        # Fail-safe: the deterministic original is returned, NOT the bad polish.
        assert text == draft

    def test_guard_is_deterministic(self) -> None:
        draft = _draft()
        polished = draft.replace("¥21,000,000", "¥31,000,000")
        a = guard_polished_text(draft, polished)
        b = guard_polished_text(draft, polished)
        assert a[0] == b[0]
        assert a[1] == b[1]
