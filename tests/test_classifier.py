"""Tests for FSA classification logic.

Covers the five-category FSA Financial Inspection Manual (金融検査マニュアル)
classification and the 要管理先 special_attention sub-tier.

Verification checklist:
  [x] FsaClass has exactly five members.
  [x] 要管理先 is a sub-tier of 要注意先 (special_attention flag), not a 6th member.
  [x] requires_turnaround: True for 要注意先 and 破綻懸念先 only.
  [x] requires_workout: True for 実質破綻先 and 破綻先 only.
  [x] classify() → 正常先 (Normal).
  [x] classify() → 要注意先 (Needs Attention) on deficit / low TDB / moderate EWS.
  [x] classify() → 要注意先 + special_attention=True (要管理先 sub-tier) on deficit.
  [x] classify() → 破綻懸念先 (In Danger) on high EWS or deficit+mid EWS.
  [x] classify() → 実質破綻先 (De facto Bankrupt) on insolvency signal or extreme EWS.
  [x] classify() → 破綻先 (Bankrupt) on both hard insolvency signals.
  [x] Determinism: severe bands are reproducible across repeated calls.
"""

from __future__ import annotations

from typing import Any

from app.backend.nodes.ews_scoring import classify
from app.shared.models.classification import FsaClass

# ---------------------------------------------------------------------------
# Enum membership
# ---------------------------------------------------------------------------


def test_fsa_class_has_exactly_five_members() -> None:
    """The enum must have exactly the five FSA Manual categories."""
    assert set(FsaClass) == {
        FsaClass.SEIJOSAKI,
        FsaClass.YOCHUISAKI,
        FsaClass.HATAN_KENENSAKI,
        FsaClass.JISSHITSU_HATANSAKI,
        FsaClass.HATANSAKI,
    }


def test_fsa_class_values_are_correct_romanizations() -> None:
    """Values must use the correct romanized identifiers."""
    assert FsaClass.SEIJOSAKI.value == "seijosaki"
    assert FsaClass.YOCHUISAKI.value == "yochuisaki"
    assert FsaClass.HATAN_KENENSAKI.value == "hatan_kenensaki"
    assert FsaClass.JISSHITSU_HATANSAKI.value == "jisshitsu_hatansaki"
    assert FsaClass.HATANSAKI.value == "hatansaki"


def test_fsa_class_kanji_labels() -> None:
    """Kanji labels must match the FSA Manual exactly."""
    assert FsaClass.SEIJOSAKI.kanji == "正常先"
    assert FsaClass.YOCHUISAKI.kanji == "要注意先"
    assert FsaClass.HATAN_KENENSAKI.kanji == "破綻懸念先"
    assert FsaClass.JISSHITSU_HATANSAKI.kanji == "実質破綻先"
    assert FsaClass.HATANSAKI.kanji == "破綻先"


# ---------------------------------------------------------------------------
# 要管理先 sub-tier (special_attention flag — NOT a 6th enum member)
# ---------------------------------------------------------------------------


def test_yokanrisaki_is_not_a_top_level_enum_member() -> None:
    """要管理先 must NOT be a separate FsaClass member — it is a sub-tier of 要注意先."""
    values = {m.value for m in FsaClass}
    assert "yokanrisaki" not in values, (
        "要管理先 must be modelled as special_attention=True on 要注意先, "
        "not as a separate top-level FsaClass member."
    )


def test_yokanrisaki_sub_tier_via_special_attention_flag() -> None:
    """要注意先 with a working-capital deficit → special_attention=True (要管理先 sub-tier)."""
    cls, special_attention = classify(
        ews_score=20.0,
        working_capital_gap=-1_000_000,
        tdb_score=80,
    )
    assert cls is FsaClass.YOCHUISAKI
    assert special_attention is True, (
        "A 要注意先 borrower with a working-capital deficit must have "
        "special_attention=True (要管理先 sub-tier)."
    )


def test_yochuisaki_without_deficit_has_no_special_attention() -> None:
    """要注意先 without a deficit → special_attention=False (not 要管理先)."""
    cls, special_attention = classify(
        ews_score=45.0,
        working_capital_gap=0,
        tdb_score=80,
    )
    assert cls is FsaClass.YOCHUISAKI
    assert special_attention is False


# ---------------------------------------------------------------------------
# requires_turnaround property
# ---------------------------------------------------------------------------


def test_requires_turnaround_true_for_yochuisaki_and_hatan_kenensaki() -> None:
    """requires_turnaround must be True for 要注意先 and 破綻懸念先 only."""
    assert FsaClass.YOCHUISAKI.requires_turnaround is True
    assert FsaClass.HATAN_KENENSAKI.requires_turnaround is True


def test_requires_turnaround_false_for_other_categories() -> None:
    """requires_turnaround must be False for 正常先, 実質破綻先, 破綻先."""
    assert FsaClass.SEIJOSAKI.requires_turnaround is False
    assert FsaClass.JISSHITSU_HATANSAKI.requires_turnaround is False
    assert FsaClass.HATANSAKI.requires_turnaround is False


# ---------------------------------------------------------------------------
# requires_workout property
# ---------------------------------------------------------------------------


def test_requires_workout_true_for_bankrupt_bands() -> None:
    """requires_workout must be True for 実質破綻先 and 破綻先."""
    assert FsaClass.JISSHITSU_HATANSAKI.requires_workout is True
    assert FsaClass.HATANSAKI.requires_workout is True


def test_requires_workout_false_for_non_bankrupt_bands() -> None:
    """requires_workout must be False for 正常先, 要注意先, 破綻懸念先."""
    assert FsaClass.SEIJOSAKI.requires_workout is False
    assert FsaClass.YOCHUISAKI.requires_workout is False
    assert FsaClass.HATAN_KENENSAKI.requires_workout is False


# ---------------------------------------------------------------------------
# classify() — 正常先 (Normal)
# ---------------------------------------------------------------------------


def test_classify_normal() -> None:
    cls, sa = classify(ews_score=10.0, working_capital_gap=5_000_000, tdb_score=80)
    assert cls is FsaClass.SEIJOSAKI
    assert sa is False


def test_classify_normal_no_signals() -> None:
    cls, sa = classify(ews_score=None, working_capital_gap=None, tdb_score=None)
    assert cls is FsaClass.SEIJOSAKI
    assert sa is False


# ---------------------------------------------------------------------------
# classify() — 要注意先 (Needs Attention)
# ---------------------------------------------------------------------------


def test_classify_needs_attention_on_deficit() -> None:
    cls, _ = classify(ews_score=20.0, working_capital_gap=-1_000_000, tdb_score=80)
    assert cls is FsaClass.YOCHUISAKI


def test_classify_needs_attention_on_low_tdb() -> None:
    cls, sa = classify(ews_score=10.0, working_capital_gap=1_000_000, tdb_score=50)
    assert cls is FsaClass.YOCHUISAKI
    assert sa is False  # no deficit → not 要管理先


def test_classify_needs_attention_on_moderate_ews() -> None:
    cls, sa = classify(ews_score=45.0, working_capital_gap=0, tdb_score=80)
    assert cls is FsaClass.YOCHUISAKI
    assert sa is False


# ---------------------------------------------------------------------------
# classify() — 破綻懸念先 (In Danger of Bankruptcy)
# ---------------------------------------------------------------------------


def test_classify_in_danger_on_high_ews() -> None:
    cls, sa = classify(ews_score=75.0, working_capital_gap=0, tdb_score=80)
    assert cls is FsaClass.HATAN_KENENSAKI
    assert sa is False


def test_classify_in_danger_on_deficit_and_mid_ews() -> None:
    cls, sa = classify(ews_score=45.0, working_capital_gap=-1, tdb_score=80)
    assert cls is FsaClass.HATAN_KENENSAKI
    assert sa is False


def test_classify_in_danger_at_ews_doubtful_boundary() -> None:
    """EWS exactly at EWS_DOUBTFUL (70) → 破綻懸念先."""
    cls, _ = classify(ews_score=70.0, working_capital_gap=0, tdb_score=80)
    assert cls is FsaClass.HATAN_KENENSAKI


# ---------------------------------------------------------------------------
# classify() — 実質破綻先 (De facto Bankrupt)
# ---------------------------------------------------------------------------


def test_classify_de_facto_bankrupt_on_is_insolvent() -> None:
    """is_insolvent=True (without negative net_worth) → 実質破綻先."""
    cls, sa = classify(
        ews_score=30.0,
        working_capital_gap=0,
        tdb_score=80,
        is_insolvent=True,
        net_worth=1_000_000,  # positive net worth → not 破綻先
    )
    assert cls is FsaClass.JISSHITSU_HATANSAKI
    assert sa is False


def test_classify_de_facto_bankrupt_on_negative_net_worth() -> None:
    """net_worth < 0 (without is_insolvent=True) → 実質破綻先."""
    cls, sa = classify(
        ews_score=30.0,
        working_capital_gap=0,
        tdb_score=80,
        is_insolvent=None,
        net_worth=-1,
    )
    assert cls is FsaClass.JISSHITSU_HATANSAKI
    assert sa is False


def test_classify_de_facto_bankrupt_on_extreme_ews() -> None:
    """EWS >= EWS_DANGER (85) → 実質破綻先 even without explicit insolvency signal."""
    cls, sa = classify(
        ews_score=90.0,
        working_capital_gap=0,
        tdb_score=80,
        is_insolvent=None,
        net_worth=None,
    )
    assert cls is FsaClass.JISSHITSU_HATANSAKI
    assert sa is False


def test_classify_de_facto_bankrupt_at_ews_danger_boundary() -> None:
    """EWS exactly at EWS_DANGER (85) → 実質破綻先."""
    cls, _ = classify(ews_score=85.0, working_capital_gap=0, tdb_score=80)
    assert cls is FsaClass.JISSHITSU_HATANSAKI


# ---------------------------------------------------------------------------
# classify() — 破綻先 (Bankrupt)
# ---------------------------------------------------------------------------


def test_classify_bankrupt_on_both_hard_signals() -> None:
    """is_insolvent=True AND net_worth < 0 → 破綻先 (most severe)."""
    cls, sa = classify(
        ews_score=30.0,
        working_capital_gap=0,
        tdb_score=80,
        is_insolvent=True,
        net_worth=-5_000_000,
    )
    assert cls is FsaClass.HATANSAKI
    assert sa is False


def test_classify_bankrupt_overrides_extreme_ews() -> None:
    """Both hard signals present → 破綻先 even with extreme EWS."""
    cls, _ = classify(
        ews_score=95.0,
        working_capital_gap=-10_000_000,
        tdb_score=20,
        is_insolvent=True,
        net_worth=-100_000_000,
    )
    assert cls is FsaClass.HATANSAKI


# ---------------------------------------------------------------------------
# Determinism: severe bands are reproducible
# ---------------------------------------------------------------------------


def test_severe_bands_are_deterministic() -> None:
    """Calling classify() twice with identical inputs must return identical results."""
    kwargs: dict[str, Any] = {
        "ews_score": 90.0,
        "working_capital_gap": -5_000_000,
        "tdb_score": 30,
        "is_insolvent": True,
        "net_worth": -10_000_000,
    }
    result_a = classify(**kwargs)
    result_b = classify(**kwargs)
    assert result_a == result_b, "classify() must be deterministic (same inputs → same output)"


def test_de_facto_bankrupt_is_deterministic() -> None:
    """実質破綻先 band is reproducible across repeated calls."""
    for _ in range(5):
        cls, sa = classify(
            ews_score=88.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=None,
            net_worth=None,
        )
        assert cls is FsaClass.JISSHITSU_HATANSAKI
        assert sa is False


def test_bankrupt_is_deterministic() -> None:
    """破綻先 band is reproducible across repeated calls."""
    for _ in range(5):
        cls, sa = classify(
            ews_score=50.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=True,
            net_worth=-1,
        )
        assert cls is FsaClass.HATANSAKI
        assert sa is False
