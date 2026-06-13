"""Tests for the Keieisha Hosho (経営者保証) guarantee-release assessment node.

Covers:
- Release-eligible borrower (all three conditions met).
- Non-eligible borrower (conditions not met).
- Succession readiness (eligible vs not).
- Score is deterministic and within [0, 100].
- Ordered directives are populated when conditions fail.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.keieisha_hosho import assess_hosho_kaijo, keieisha_hosho_node
from app.backend.state import SaiseiState
from app.shared.models.accounting import TrialBalance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tb(
    period: str = "2026-03-31",
    uriage: int = 100_000_000,
    uriage_genka: int = 70_000_000,
    hanbaihi: int = 20_000_000,
    eigai_shueki: int = 500_000,
    eigai_hiyo: int = 300_000,
) -> TrialBalance:
    return TrialBalance(
        period=dt.date.fromisoformat(period),
        uriage=uriage,
        uriage_genka=uriage_genka,
        hanbaihi=hanbaihi,
        eigai_shueki=eigai_shueki,
        eigai_hiyo=eigai_hiyo,
    )


def _healthy_state() -> SaiseiState:
    """A borrower that should be release-eligible."""
    return SaiseiState(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        tdb_score=75,
        ews_score=20.0,
        working_capital_gap=5_000_000,  # positive
        shisanhyo=[_tb() for _ in range(12)],
        errors=[],
    )


def _distressed_state() -> SaiseiState:
    """A borrower that should NOT be release-eligible."""
    return SaiseiState(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        tdb_score=40,
        ews_score=75.0,
        working_capital_gap=-5_000_000,  # deficit
        shisanhyo=[
            _tb(eigai_shueki=100_000, eigai_hiyo=5_000_000)  # poor separation
            for _ in range(12)
        ],
        errors=["some error"],
    )


# ---------------------------------------------------------------------------
# assess_hosho_kaijo (pure function)
# ---------------------------------------------------------------------------


def test_assess_hosho_kaijo_eligible() -> None:
    """All three conditions met → high score, no directives.

    Updated for Fix 2: avg_uriage added as the operating-scale reference for
    the revised bunri proxy. Small non-op items relative to sales → bunri_met.
    (Old comment 'ratio > 1.0 → bunri met' removed; new logic is bidirectional.)
    """
    conditions = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=500_000.0,   # 0.5% of sales — small, clean
        avg_eigai_hiyo=300_000.0,     # 0.3% of sales — small, clean
        avg_uriage=100_000_000.0,     # 100M sales (operating scale reference)
        ews_score=20.0,
        working_capital_gap=5_000_000,
        tdb_score=75,
        error_count=0,
    )
    assert conditions.bunri_met is True
    assert conditions.zaimu_met is True
    assert conditions.kaiji_met is True
    assert conditions.bunri_score == 40.0
    assert conditions.zaimu_score == 35.0
    assert conditions.kaiji_score == 25.0
    assert conditions.ordered_directives == []


def test_assess_hosho_kaijo_not_eligible() -> None:
    """No conditions met → low score, all directives populated.

    Updated for Fix 2: avg_uriage added. High non-op expense (5% of sales)
    still correctly penalizes bunri score under the new bidirectional proxy.
    """
    conditions = assess_hosho_kaijo(
        shisanhyo_count=6,  # < 12 months
        avg_eigai_shueki=100_000.0,
        avg_eigai_hiyo=5_000_000.0,   # 5% of sales — abnormally high expense
        avg_uriage=100_000_000.0,
        ews_score=75.0,
        working_capital_gap=-5_000_000,
        tdb_score=None,
        error_count=2,
    )
    assert conditions.bunri_met is False
    assert conditions.zaimu_met is False
    assert conditions.kaiji_met is False
    assert conditions.bunri_score < 40.0
    assert conditions.zaimu_score == 0.0
    assert conditions.kaiji_score < 25.0
    assert len(conditions.ordered_directives) == 3


def test_assess_hosho_kaijo_partial_zaimu() -> None:
    """EWS strong but gap negative → partial zaimu score.

    Updated for Fix 2: avg_uriage added (clean non-op items → bunri unaffected).
    """
    conditions = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=500_000.0,
        avg_eigai_hiyo=300_000.0,
        avg_uriage=100_000_000.0,
        ews_score=20.0,  # strong
        working_capital_gap=-1_000_000,  # deficit
        tdb_score=75,
        error_count=0,
    )
    assert conditions.zaimu_met is False
    assert conditions.zaimu_score == 35.0 * 0.5


def test_assess_hosho_kaijo_score_in_range() -> None:
    """Score is always in [0, 100].

    Updated for Fix 2: avg_uriage added.
    """
    for ews in [0.0, 40.0, 70.0, 100.0]:
        for gap in [5_000_000, -5_000_000]:
            conditions = assess_hosho_kaijo(
                shisanhyo_count=12,
                avg_eigai_shueki=500_000.0,
                avg_eigai_hiyo=300_000.0,
                avg_uriage=100_000_000.0,
                ews_score=ews,
                working_capital_gap=gap,
                tdb_score=75,
                error_count=0,
            )
            total = conditions.bunri_score + conditions.zaimu_score + conditions.kaiji_score
            assert 0.0 <= total <= 100.0


def test_assess_hosho_kaijo_deterministic() -> None:
    """Same inputs always produce the same output.

    Updated for Fix 2: avg_uriage added.
    """
    kwargs = {
        "shisanhyo_count": 12,
        "avg_eigai_shueki": 500_000.0,
        "avg_eigai_hiyo": 300_000.0,
        "avg_uriage": 100_000_000.0,
        "ews_score": 30.0,
        "working_capital_gap": 1_000_000,
        "tdb_score": 70,
        "error_count": 0,
    }
    a = assess_hosho_kaijo(**kwargs)
    b = assess_hosho_kaijo(**kwargs)
    assert a.bunri_score == b.bunri_score
    assert a.zaimu_score == b.zaimu_score
    assert a.kaiji_score == b.kaiji_score


# ---------------------------------------------------------------------------
# keieisha_hosho_node (graph node)
# ---------------------------------------------------------------------------


def test_keieisha_hosho_node_eligible() -> None:
    """Healthy borrower → high score, eligible, succession ready."""
    state = _healthy_state()
    result = keieisha_hosho_node(state)
    assert "hosho_kaijo_score" in result
    assert "hosho_kaijo_conditions" in result
    assert "hosho_kaijo_eligible" in result
    assert "succession_ready" in result
    score = result["hosho_kaijo_score"]
    assert isinstance(score, float)
    assert 0.0 <= score <= 100.0
    # Healthy borrower should score well.
    assert score >= 60.0
    # All three conditions met → score 100 → eligible (>= 70 threshold).
    assert result["hosho_kaijo_eligible"] is True
    assert result["succession_ready"] is True


def test_keieisha_hosho_node_distressed() -> None:
    """Distressed borrower → low score, not eligible, not succession ready."""
    state = _distressed_state()
    result = keieisha_hosho_node(state)
    score = result["hosho_kaijo_score"]
    assert score < 60.0
    assert result["hosho_kaijo_eligible"] is False
    assert result["succession_ready"] is False


def test_keieisha_hosho_eligible_matches_threshold() -> None:
    """hosho_kaijo_eligible is exactly score >= HOSHO_ELIGIBLE_SCORE."""
    from app.shared.constants import HOSHO_ELIGIBLE_SCORE

    for state in (_healthy_state(), _distressed_state()):
        result = keieisha_hosho_node(state)
        expected = result["hosho_kaijo_score"] >= HOSHO_ELIGIBLE_SCORE
        assert result["hosho_kaijo_eligible"] is expected


def test_keieisha_hosho_node_no_shisanhyo() -> None:
    """No Shisanhyo → node still runs, returns defaults."""
    state = SaiseiState(
        tdb_code="1234567",
        ews_score=30.0,
        working_capital_gap=1_000_000,
        tdb_score=70,
    )
    result = keieisha_hosho_node(state)
    assert "hosho_kaijo_score" in result
    assert result["hosho_kaijo_score"] is not None


def test_keieisha_hosho_node_score_deterministic() -> None:
    """Same state always produces the same score."""
    state = _healthy_state()
    r1 = keieisha_hosho_node(state)
    r2 = keieisha_hosho_node(state)
    assert r1["hosho_kaijo_score"] == r2["hosho_kaijo_score"]
    assert r1["succession_ready"] == r2["succession_ready"]


# ---------------------------------------------------------------------------
# Fix 2: bunri proxy — FSA-correct bidirectional non-op penalty
# ---------------------------------------------------------------------------


def test_bunri_clean_firm_scores_well() -> None:
    """Clean firm with small non-op items both ways → bunri_met=True, full score.

    FSA intent: a firm with minimal non-operating items (no owner-loan interest,
    no owner-property rent) demonstrates good corporate/personal separation.
    """
    conditions = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=200_000.0,   # tiny non-op income (0.2% of sales)
        avg_eigai_hiyo=150_000.0,     # tiny non-op expense (0.15% of sales)
        avg_uriage=100_000_000.0,     # 100M sales
        ews_score=20.0,
        working_capital_gap=5_000_000,
        tdb_score=75,
        error_count=0,
    )
    assert conditions.bunri_met is True, (
        "Clean firm with tiny non-op items should have bunri_met=True"
    )
    assert conditions.bunri_score == 40.0, (
        "Clean firm should receive full bunri score (40 pts)"
    )


def test_bunri_high_non_op_expense_penalized() -> None:
    """High non-op expense (owner-loan interest) → bunri_met=False, reduced score.

    FSA red flag: abnormally high non-operating EXPENSE signals owner-loan
    interest being paid to the owner — a classic poor-separation indicator.
    """
    conditions = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=100_000.0,    # tiny income
        avg_eigai_hiyo=5_000_000.0,    # large expense (5% of sales) — red flag
        avg_uriage=100_000_000.0,
        ews_score=20.0,
        working_capital_gap=5_000_000,
        tdb_score=75,
        error_count=0,
    )
    assert conditions.bunri_met is False, (
        "High non-op expense should penalize bunri score (owner-loan interest red flag)"
    )
    assert conditions.bunri_score < 40.0, (
        "Bunri score must be reduced when non-op expense is abnormally high"
    )


def test_bunri_high_non_op_income_also_penalized() -> None:
    """High non-op income (owner renting property to company) → bunri_met=False.

    KEY assertion for Fix 2: the OLD proxy REWARDED high non-op income
    (ratio = income/expense >= 1.0 → full score). The FSA intent is the opposite:
    abnormally high non-operating INCOME is also a red flag for poor separation
    (e.g., owner renting personal property to the company, or company lending
    to the owner). Both directions must be penalized.
    """
    conditions = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=5_000_000.0,  # large income (5% of sales) — red flag
        avg_eigai_hiyo=100_000.0,      # tiny expense
        avg_uriage=100_000_000.0,
        ews_score=20.0,
        working_capital_gap=5_000_000,
        tdb_score=75,
        error_count=0,
    )
    assert conditions.bunri_met is False, (
        "High non-op INCOME must also penalize bunri score. "
        "The old proxy (income/expense >= 1.0 → full score) was inverted vs FSA intent. "
        "Large non-op income signals owner renting property to company or company "
        "lending to owner — both are poor-separation red flags."
    )
    assert conditions.bunri_score < 40.0, (
        "Bunri score must be reduced when non-op income is abnormally high"
    )


def test_bunri_symmetric_penalty() -> None:
    """Equal large non-op items in both directions → same penalty as one-sided.

    A firm with both high income AND high expense (e.g., owner both lends to
    and borrows from the company) is equally poorly separated.
    """
    # High expense only
    cond_expense = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=100_000.0,
        avg_eigai_hiyo=5_000_000.0,
        avg_uriage=100_000_000.0,
        ews_score=20.0,
        working_capital_gap=5_000_000,
        tdb_score=75,
        error_count=0,
    )
    # High income only (same magnitude)
    cond_income = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=5_000_000.0,
        avg_eigai_hiyo=100_000.0,
        avg_uriage=100_000_000.0,
        ews_score=20.0,
        working_capital_gap=5_000_000,
        tdb_score=75,
        error_count=0,
    )
    # Both should be penalized (not met)
    assert cond_expense.bunri_met is False
    assert cond_income.bunri_met is False
    # Scores should be equal (symmetric treatment)
    assert cond_expense.bunri_score == cond_income.bunri_score, (
        "High non-op expense and high non-op income of equal magnitude should "
        "produce the same bunri penalty (symmetric FSA treatment)"
    )


def test_bunri_directive_meaningful_japanese() -> None:
    """Bunri directive text is non-empty Japanese when condition is not met."""
    conditions = assess_hosho_kaijo(
        shisanhyo_count=12,
        avg_eigai_shueki=5_000_000.0,
        avg_eigai_hiyo=100_000.0,
        avg_uriage=100_000_000.0,
        ews_score=20.0,
        working_capital_gap=5_000_000,
        tdb_score=75,
        error_count=0,
    )
    assert conditions.bunri_met is False
    assert len(conditions.bunri_directive) > 10, "Directive must be non-trivial"
    # Must contain at least one Japanese character (CJK range U+4E00–U+9FFF)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in conditions.bunri_directive), (
        "Directive must contain Japanese text"
    )


def test_bunri_score_in_range() -> None:
    """Bunri score is always in [0, HOSHO_WEIGHT_BUNRI]."""
    from app.shared.constants import HOSHO_WEIGHT_BUNRI

    for shueki, hiyo in [
        (0.0, 0.0),
        (5_000_000.0, 0.0),
        (0.0, 5_000_000.0),
        (5_000_000.0, 5_000_000.0),
        (200_000.0, 150_000.0),
    ]:
        conditions = assess_hosho_kaijo(
            shisanhyo_count=12,
            avg_eigai_shueki=shueki,
            avg_eigai_hiyo=hiyo,
            avg_uriage=100_000_000.0,
            ews_score=20.0,
            working_capital_gap=5_000_000,
            tdb_score=75,
            error_count=0,
        )
        assert 0.0 <= conditions.bunri_score <= HOSHO_WEIGHT_BUNRI, (
            f"bunri_score={conditions.bunri_score} out of range for "
            f"shueki={shueki}, hiyo={hiyo}"
        )
