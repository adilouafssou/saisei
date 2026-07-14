"""MR #2 feasibility_critic — determinism-parity tests (re-posed formula).

The feasibility critic is an ADVISORY-ONLY upstream pre-screen. These tests are
the verifier proving the deterministic spine is untouched and the node is
offline-safe:

- achievability band + score are deterministic and reproducible;
- the new multi-factor formula (uplift_ratio, wc_stress, rate_stress,
  settle_stress, industry adjustment) is monotone and auditable;
- with NO LLM configured, the advisory text is empty (offline fallback);
- advisory_grounded is False offline;
- reconciliation_required is False offline (no LLM -> no-op);
- the node emits feasibility_notes + reconciliation fields and never a gate field;
- the three deterministic critics + lead_arranger produce byte-identical
  verdicts/routing/burden whether or not feasibility_notes are present, because
  nothing downstream reads them.

If a later step lets feasibility influence a verdict/route/figure, one of these
assertions must fail — that is the guardrail.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.critics.feasibility import (
    assess_feasibility,
    band_ordinal,
    feasibility_critic_node,
    is_advisory_grounded,
    llm_band_from_score,
)
from app.backend.nodes.critics.guarantor import guarantor_critic_node
from app.backend.nodes.critics.main_bank import main_bank_critic_node
from app.backend.nodes.critics.sub_bank import sub_bank_critic_node
from app.backend.nodes.lead_arranger import lead_arranger_node
from app.backend.state import SaiseiState, Strategy
from app.backend.tools.boj_macro import RatePoint, SettlementMetrics
from app.shared.constants import FEASIBILITY_HIGH_FLOOR, FEASIBILITY_MEDIUM_FLOOR
from app.shared.models.accounting import TrialBalance
from app.shared.settings import Settings


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="test", expected_keijo_uplift=uplift)


def _full_strategies() -> list[Strategy]:
    return [
        _strategy("price", 43_920_000),
        _strategy("cogs", 30_960_000),
        _strategy("sga", 14_100_000),
        _strategy("wc", 5_000_000),
    ]


def _tb(uriage: int) -> TrialBalance:
    """Minimal trial balance with the sales figure the proxy reads."""
    return TrialBalance(
        period=dt.date(2024, 1, 31),
        uriage=uriage,
        uriage_genka=uriage // 2,
        hanbaihi=uriage // 5,
        eigai_shueki=0,
        eigai_hiyo=0,
    )


def _mock_rate_curve(bps: int = 60) -> list[RatePoint]:
    """Return a single-point rate curve with the given basis points."""
    return [RatePoint(as_of=dt.date(2026, 3, 31), policy_rate_bps=bps)]


def _mock_metrics(receivable_days: int = 95, payable_days: int = 45) -> SettlementMetrics:
    return SettlementMetrics(
        t_plus_1_liquidity_ratio=0.82,
        t_plus_2_liquidity_ratio=0.74,
        receivable_days=receivable_days,
        payable_days=payable_days,
    )


#: No-LLM settings: empty api_key/model -> offline fallback.
_OFFLINE = Settings(llm_api_key="", llm_model="")


# ---------------------------------------------------------------------------
# Band ordinal and llm_band_from_score helpers.
# ---------------------------------------------------------------------------


def test_band_ordinal_mapping() -> None:
    """band_ordinal returns 0/1/2 for low/medium/high."""
    assert band_ordinal("low") == 0
    assert band_ordinal("medium") == 1
    assert band_ordinal("high") == 2
    assert band_ordinal("unknown") == 1  # default


def test_llm_band_from_score_thresholds() -> None:
    """llm_band_from_score uses the same thresholds as the floor formula."""
    assert llm_band_from_score(FEASIBILITY_HIGH_FLOOR) == "high"
    assert llm_band_from_score(FEASIBILITY_HIGH_FLOOR - 0.01) == "medium"
    assert llm_band_from_score(FEASIBILITY_MEDIUM_FLOOR) == "medium"
    assert llm_band_from_score(FEASIBILITY_MEDIUM_FLOOR - 0.01) == "low"
    assert llm_band_from_score(0.0) == "low"
    assert llm_band_from_score(100.0) == "high"


# ---------------------------------------------------------------------------
# Multi-factor formula: determinism and monotonicity.
# ---------------------------------------------------------------------------


def test_achievability_band_is_deterministic() -> None:
    """Same inputs always yield the same band and score (MR #2 formula)."""
    s = _strategy("price", 5_000_000)
    a = assess_feasibility(s, monthly_sales=100_000_000)
    b = assess_feasibility(s, monthly_sales=100_000_000)
    assert a == b
    assert a.advisory == ""
    assert a.advisory_grounded is False


def test_achievability_bands_cover_low_medium_high() -> None:
    """The multi-factor formula separates small/mid/large uplifts into 3 bands.

    With no WC deficit, no rate stress, and no settlement stress, the formula
    reduces to the uplift-ratio component only:
        score = 100 - FEASIBILITY_WEIGHT_UPLIFT * ratio * 100

    Band thresholds (score-based):
        score >= FEASIBILITY_HIGH_FLOOR   (65) -> 'high'
        score >= FEASIBILITY_MEDIUM_FLOOR (35) -> 'medium'
        score <  FEASIBILITY_MEDIUM_FLOOR (35) -> 'low'

    With FEASIBILITY_WEIGHT_UPLIFT = 1.5:
        ratio = 0.01 (1%)   -> score = 100 - 1.5 = 98.5  -> 'high'
        ratio = 0.30 (30%)  -> score = 100 - 45  = 55.0  -> 'medium'
        ratio = 0.50 (50%)  -> score = 100 - 75  = 25.0  -> 'low'
    """
    monthly = 10_000_000  # annual sales = 120,000,000
    # No stress signals -> score driven by uplift ratio alone.
    # ratio = 1.2M / 120M = 1% -> score = 98.5 -> 'high'
    high = assess_feasibility(_strategy("a", 1_200_000), monthly)
    # ratio = 36M / 120M = 30% -> score = 55.0 -> 'medium'
    medium = assess_feasibility(_strategy("b", 36_000_000), monthly)
    # ratio = 60M / 120M = 50% -> score = 25.0 -> 'low'
    low = assess_feasibility(_strategy("c", 60_000_000), monthly)
    assert high.achievability == "high"
    assert medium.achievability == "medium"
    assert low.achievability == "low"
    # Score decreases as the ask grows.
    assert high.achievability_score > medium.achievability_score > low.achievability_score


def test_larger_wc_deficit_lowers_score() -> None:
    """Larger working-capital deficit -> lower achievability score (monotone)."""
    s = _strategy("price", 5_000_000)
    monthly = 100_000_000
    no_deficit = assess_feasibility(s, monthly, working_capital_gap=0)
    small_deficit = assess_feasibility(s, monthly, working_capital_gap=-10_000_000)
    large_deficit = assess_feasibility(s, monthly, working_capital_gap=-100_000_000)
    assert no_deficit.achievability_score >= small_deficit.achievability_score
    assert small_deficit.achievability_score >= large_deficit.achievability_score


def test_higher_rate_stress_lowers_score() -> None:
    """Higher BOJ rate -> lower achievability score (monotone)."""
    s = _strategy("price", 5_000_000)
    monthly = 100_000_000
    low_rate = assess_feasibility(s, monthly, rate_curve=_mock_rate_curve(bps=10))
    high_rate = assess_feasibility(s, monthly, rate_curve=_mock_rate_curve(bps=100))
    assert low_rate.achievability_score > high_rate.achievability_score


def test_longer_cash_cycle_lowers_score() -> None:
    """Longer cash-conversion cycle -> lower achievability score (monotone)."""
    s = _strategy("price", 5_000_000)
    monthly = 100_000_000
    short_cycle = assess_feasibility(
        s, monthly, settlement_metrics=_mock_metrics(receivable_days=30, payable_days=30)
    )
    long_cycle = assess_feasibility(
        s, monthly, settlement_metrics=_mock_metrics(receivable_days=120, payable_days=30)
    )
    assert short_cycle.achievability_score > long_cycle.achievability_score


def test_capital_intensive_industry_lowers_score() -> None:
    """Capital-intensive industry -> lower score than service industry."""
    s = _strategy("price", 5_000_000)
    monthly = 100_000_000
    service = assess_feasibility(s, monthly, industry="情報サービス業")
    manufacturing = assess_feasibility(s, monthly, industry="金属部品製造業")
    assert service.achievability_score > manufacturing.achievability_score


def test_score_clamped_to_zero_on_extreme_stress() -> None:
    """Score is clamped to 0 when stress signals are extreme."""
    s = _strategy("price", 1_000_000_000)  # enormous uplift
    monthly = 1_000_000  # tiny sales
    note = assess_feasibility(
        s,
        monthly,
        working_capital_gap=-1_000_000_000,
        rate_curve=_mock_rate_curve(bps=500),
        settlement_metrics=_mock_metrics(receivable_days=180, payable_days=0),
    )
    assert note.achievability_score == 0.0
    assert note.achievability == "low"


def test_score_at_100_with_zero_stress() -> None:
    """Score approaches 100 when all stress signals are zero and uplift is tiny."""
    s = _strategy("price", 1)  # negligible uplift
    monthly = 1_000_000_000  # enormous sales
    note = assess_feasibility(
        s,
        monthly,
        working_capital_gap=0,
        rate_curve=[],
        settlement_metrics=_mock_metrics(receivable_days=30, payable_days=30),
    )
    # Score should be very close to 100 (tiny uplift ratio, no stress).
    assert note.achievability_score >= 99.0
    assert note.achievability == "high"


def test_rationale_contains_all_key_terms() -> None:
    """Rationale string contains all auditable terms."""
    s = _strategy("price", 5_000_000)
    note = assess_feasibility(
        s,
        monthly_sales=100_000_000,
        working_capital_gap=-10_000_000,
        rate_curve=_mock_rate_curve(60),
        settlement_metrics=_mock_metrics(),
    )
    assert "資金繰りストレス" in note.rationale
    assert "金利ストレス" in note.rationale
    assert "決済サイクルストレス" in note.rationale
    assert "総合スコア" in note.rationale


# ---------------------------------------------------------------------------
# Citation-grounding post-check.
# ---------------------------------------------------------------------------


def test_is_advisory_grounded_empty_inputs() -> None:
    """Empty advisory or empty snippets -> not grounded."""
    from app.backend.tools.retrieval import RetrievalSnippet

    snippet = RetrievalSnippet(source="past_keikakusho", text="some text here", score=0.9)
    assert is_advisory_grounded("", [snippet]) is False
    assert is_advisory_grounded("some advisory", []) is False
    assert is_advisory_grounded("", []) is False


def test_is_advisory_grounded_source_match() -> None:
    """Advisory containing the snippet source label -> grounded."""
    from app.backend.tools.retrieval import RetrievalSnippet

    snippet = RetrievalSnippet(source="past_keikakusho", text="irrelevant", score=0.9)
    assert is_advisory_grounded("参考: [past_keikakusho] の事例に基づき", [snippet]) is True


def test_is_advisory_grounded_token_match() -> None:
    """Advisory containing a 4+ char token from snippet text -> grounded."""
    from app.backend.tools.retrieval import RetrievalSnippet

    snippet = RetrievalSnippet(
        source="benchmark", text="価格転嫁 implementation strategy", score=0.8
    )
    # "implementation" is >= 4 chars and appears in advisory.
    assert is_advisory_grounded("implementation of price pass-through", [snippet]) is True


def test_is_advisory_grounded_no_match() -> None:
    """Advisory with no overlap -> not grounded."""
    from app.backend.tools.retrieval import RetrievalSnippet

    snippet = RetrievalSnippet(source="xyz", text="abc def", score=0.5)
    assert is_advisory_grounded("completely unrelated text", [snippet]) is False


# ---------------------------------------------------------------------------
# Node offline behaviour (MR #2 additions).
# ---------------------------------------------------------------------------


def test_node_offline_emits_correct_keys() -> None:
    """With no LLM, the node returns feasibility_notes + reconciliation fields."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    assert set(out.keys()) == {
        "feasibility_notes",
        "reconciliation_required",
        "reconciliation_details",
    }
    notes = out["feasibility_notes"]
    assert len(notes) == len(_full_strategies())
    for note in notes:
        assert note["advisory"] == ""
        assert note["advisory_grounded"] is False
        assert note["achievability"] in {"high", "medium", "low"}
        assert set(note.keys()) == {
            "strategy_title",
            "achievability",
            "achievability_score",
            "rationale",
            "advisory",
            "advisory_grounded",
            "advisory_provenance",
            "uplift_credibility",
            "uplift_credibility_ratio",
            "uplift_credibility_reason",
            "realism_flag",
            "realism_note",
        }


def test_node_populates_realism_flag_when_history_present() -> None:
    """Depth step 4 pt 3: each note carries a deterministic realism verdict
    reconciling the achievability and uplift-credibility bands (advisory only)."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    for note in out["feasibility_notes"]:
        assert note["realism_flag"] in {
            "consistent",
            "consistently_weak",
            "optimistic_uplift",
            "pessimistic_uplift",
        }
        assert note["realism_note"] != ""


def test_node_skips_realism_flag_without_history() -> None:
    """No shisanhyo -> realism fields stay empty (offline/no-history safe)."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    for note in out["feasibility_notes"]:
        assert note["realism_flag"] == ""
        assert note["realism_note"] == ""


def test_node_populates_uplift_credibility_when_history_present() -> None:
    """Depth step 4 wiring: each note carries a deterministic uplift-credibility
    verdict when shisanhyo is present, surfaced for the banker (advisory only)."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    for note in out["feasibility_notes"]:
        assert note["uplift_credibility"] in {"grounded", "stretch", "implausible"}
        assert note["uplift_credibility_reason"] != ""


def test_node_skips_uplift_credibility_without_history() -> None:
    """No shisanhyo -> credibility fields stay empty (offline/no-history safe)."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    for note in out["feasibility_notes"]:
        assert note["uplift_credibility"] == ""
        assert note["uplift_credibility_ratio"] is None
        assert note["uplift_credibility_reason"] == ""


def test_node_offline_reconciliation_is_false() -> None:
    """With no LLM, reconciliation_required is always False (offline-safe)."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    assert out["reconciliation_required"] is False
    assert out["reconciliation_details"] == []


def test_node_is_reproducible_offline() -> None:
    """Two runs with the same state produce byte-identical notes (offline)."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
    )
    a = feasibility_critic_node(state, settings=_OFFLINE)
    b = feasibility_critic_node(state, settings=_OFFLINE)
    assert a == b


def test_node_no_strategies_returns_empty() -> None:
    """No strategies -> empty notes and no reconciliation."""
    state = SaiseiState(tdb_code="1234567")
    out = feasibility_critic_node(state, settings=_OFFLINE)
    assert out == {
        "feasibility_notes": [],
        "reconciliation_required": False,
        "reconciliation_details": [],
    }


def _run_meeting(state: SaiseiState) -> dict:  # type: ignore[type-arg]
    """Run the three deterministic critics + lead_arranger over a state."""
    feedbacks: list[dict] = []  # type: ignore[type-arg]
    feedbacks += main_bank_critic_node(state)["critic_feedbacks"]
    feedbacks += sub_bank_critic_node(state)["critic_feedbacks"]
    feedbacks += guarantor_critic_node(state)["critic_feedbacks"]
    consolidated = state.model_copy(update={"critic_feedbacks": feedbacks})
    return lead_arranger_node(consolidated)


def test_meeting_is_byte_identical_with_and_without_feasibility_notes() -> None:
    """feasibility_notes never change the gate verdicts, routing, or burden.

    Run the deterministic critics + lead_arranger twice: once on a bare state,
    once on a state already carrying feasibility_notes. The consolidated output
    must be identical, proving nothing downstream reads the advisory channel.
    """
    base = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        shisanhyo=[_tb(100_000_000)],
        working_capital_gap=-5_000_000,
        revision_count=0,
    )
    out = feasibility_critic_node(base, settings=_OFFLINE)
    notes = out["feasibility_notes"]
    with_notes = base.model_copy(update={"feasibility_notes": notes})

    result_without = _run_meeting(base)
    result_with = _run_meeting(with_notes)

    # meeting_briefing INTENTIONALLY renders feasibility_notes (the advisory
    # rehearsal — see test_part4_meeting_briefing.py::
    # test_feasibility_notes_change_only_the_briefing_text). The spine contract
    # is that NOTHING ELSE depends on feasibility_notes: gate verdicts, routing,
    # revision_count and the burden table must be byte-identical. Compare all
    # keys except the advisory briefing.
    without_rest = {k: v for k, v in result_without.items() if k != "meeting_briefing"}
    with_rest = {k: v for k, v in result_with.items() if k != "meeting_briefing"}

    assert without_rest == with_rest, (
        "meeting consolidation (excluding the advisory briefing) must not depend "
        "on feasibility_notes"
    )
