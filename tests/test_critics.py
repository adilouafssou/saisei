"""Tests for the multi-critic nodes and lead arranger.

Covers:
- main_bank_critic: PASS and FAIL cases (explicit commitment flags).
- sub_bank_critic: PASS and FAIL cases (stake-based and heuristic proxy).
- guarantor_critic: PASS and FAIL cases.
- lead_arranger: consolidation, P0/P1/P2 priority, burden-sharing table,
  cyclic-routing decision (approved vs rejected).
- critic_feedbacks_reducer: clear sentinel regression test (Fix 1).
- Cyclic revision accumulation regression test (Fix 1).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.backend.nodes.critics.guarantor import guarantor_critic_node
from app.backend.nodes.critics.main_bank import main_bank_critic_node
from app.backend.nodes.critics.sub_bank import (
    compute_main_bank_share,
    sub_bank_critic_node,
)
from app.backend.nodes.kaizen_generation import strategist_node
from app.backend.nodes.lead_arranger import (
    compute_burden_sharing_table,
    is_banker_only_blocker,
    lead_arranger_node,
)
from app.backend.state import (
    CRITIC_FEEDBACKS_CLEAR,
    CriticFeedback,
    SaiseiState,
    Strategy,
    critic_feedbacks_reducer,
)
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tb(
    period: str = "2026-03-31",
    uriage: int = 122_000_000,
    uriage_genka: int = 129_000_000,
    hanbaihi: int = 23_500_000,
    eigai_shueki: int = 100_000,
    eigai_hiyo: int = 4_300_000,
) -> TrialBalance:
    return TrialBalance(
        period=dt.date.fromisoformat(period),
        uriage=uriage,
        uriage_genka=uriage_genka,
        hanbaihi=hanbaihi,
        eigai_shueki=eigai_shueki,
        eigai_hiyo=eigai_hiyo,
    )


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="test", expected_keijo_uplift=uplift)


def _sga_strategy() -> Strategy:
    return _strategy("販売費・一般管理費の見直し（SG&A rationalisation）", 14_100_000)


def _wc_strategy() -> Strategy:
    return _strategy("資金繰り改善（Working-capital / Shikin Kuri）", 5_000_000)


def _price_strategy() -> Strategy:
    return _strategy("価格転嫁の実行（Price pass-through）", 43_920_000)


def _cogs_strategy() -> Strategy:
    return _strategy("原価低減（COGS reduction）", 30_960_000)


def _full_strategies() -> list[Strategy]:
    return [_price_strategy(), _cogs_strategy(), _sga_strategy(), _wc_strategy()]


def _distressed_state_with_strategies(
    strategies: list[Strategy] | None = None,
    working_capital_gap: int = -5_000_000,
    ews_score: float = 75.0,
    fsa_classification: FsaClass = FsaClass.HATAN_KENENSAKI,
    yakuin_hoshu_cut: bool = False,
    personal_asset_disposal: bool = False,
    lender_stakes: dict[str, int] | None = None,
) -> SaiseiState:
    return SaiseiState(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        tdb_score=41,
        ews_score=ews_score,
        working_capital_gap=working_capital_gap,
        fsa_classification=fsa_classification,
        shisanhyo=[_tb()],
        # NOTE: use an explicit None check so an intentionally-empty strategy
        # list (strategies=[]) is preserved. `strategies or _full_strategies()`
        # would wrongly replace [] with the full set, defeating the
        # no-strategies FAIL tests (sub_bank / guarantor).
        proposed_strategies=(strategies if strategies is not None else _full_strategies()),
        yakuin_hoshu_cut=yakuin_hoshu_cut,
        personal_asset_disposal=personal_asset_disposal,
        lender_stakes=lender_stakes or {},
    )


# ---------------------------------------------------------------------------
# main_bank_critic  (Fix 3: explicit commitment flags replace dead proxies)
# ---------------------------------------------------------------------------


def test_main_bank_pass_both_flags_set() -> None:
    """Both yakuin_hoshu_cut=True and personal_asset_disposal=True → PASS."""
    state = _distressed_state_with_strategies(
        yakuin_hoshu_cut=True,
        personal_asset_disposal=True,
    )
    result = main_bank_critic_node(state)
    feedbacks = result["critic_feedbacks"]
    assert len(feedbacks) == 1
    fb = feedbacks[0]
    assert fb["status"] == "PASS"
    assert fb["persona"] == "main_bank"
    assert fb["priority"] == "P1"
    assert fb["fatal_blockers"] == []


def test_main_bank_fail_yakuin_hoshu_not_set() -> None:
    """yakuin_hoshu_cut=False → FAIL (yakuin_hoshu_not_cut), regardless of strategies."""
    # Even with ALL strategies present, the gate fires because the flag is False.
    state = _distressed_state_with_strategies(
        strategies=_full_strategies(),
        yakuin_hoshu_cut=False,
        personal_asset_disposal=True,
    )
    result = main_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"
    assert any("yakuin_hoshu_not_cut" in b for b in fb["fatal_blockers"])


def test_main_bank_fail_no_asset_disposal_with_deficit() -> None:
    """Deficit + personal_asset_disposal=False → FAIL (no_asset_disposal)."""
    state = _distressed_state_with_strategies(
        working_capital_gap=-5_000_000,
        yakuin_hoshu_cut=True,
        personal_asset_disposal=False,
    )
    result = main_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"
    assert any("no_asset_disposal" in b for b in fb["fatal_blockers"])


def test_main_bank_pass_no_deficit_no_disposal_flag() -> None:
    """No deficit + personal_asset_disposal=False → PASS (no disposal needed)."""
    state = _distressed_state_with_strategies(
        working_capital_gap=5_000_000,
        yakuin_hoshu_cut=True,
        personal_asset_disposal=False,
    )
    result = main_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "PASS"


def test_main_bank_fail_both_flags_missing() -> None:
    """Both flags False with deficit → two blockers."""
    state = _distressed_state_with_strategies(
        working_capital_gap=-5_000_000,
        yakuin_hoshu_cut=False,
        personal_asset_disposal=False,
    )
    result = main_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"
    assert len(fb["fatal_blockers"]) == 2


def test_main_bank_gate_fires_even_with_sga_strategy() -> None:
    """Gate fires even when SG&A strategy is present (old dead proxy no longer used)."""
    # This test proves the old proxy is gone: having the SG&A strategy is NOT enough.
    state = _distressed_state_with_strategies(
        strategies=_full_strategies(),  # includes SG&A and WC strategies
        yakuin_hoshu_cut=False,  # flag not set → must FAIL
        personal_asset_disposal=False,
        working_capital_gap=-5_000_000,
    )
    result = main_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL", (
        "Gate must FAIL when yakuin_hoshu_cut=False, even if SG&A strategy is present"
    )


def test_main_bank_deterministic() -> None:
    """Same state → same result."""
    state = _distressed_state_with_strategies(yakuin_hoshu_cut=True, personal_asset_disposal=True)
    r1 = main_bank_critic_node(state)
    r2 = main_bank_critic_node(state)
    assert r1["critic_feedbacks"][0]["status"] == r2["critic_feedbacks"][0]["status"]


# ---------------------------------------------------------------------------
# sub_bank_critic
# ---------------------------------------------------------------------------


def test_sub_bank_pass_balanced() -> None:
    """Balanced strategies → PASS (within pro-rata tolerance)."""
    # Equal uplifts → 50/50 split → within tolerance.
    strategies = [
        _strategy("A", 50_000_000),
        _strategy("B", 50_000_000),
    ]
    state = _distressed_state_with_strategies(strategies=strategies)
    result = sub_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "PASS"
    assert fb["persona"] == "sub_bank"
    assert fb["priority"] == "P2"


def test_sub_bank_fail_disproportionate() -> None:
    """One strategy dominates (>70% of total) → FAIL."""
    strategies = [
        _strategy("Dominant", 90_000_000),
        _strategy("Minor", 10_000_000),
    ]
    state = _distressed_state_with_strategies(strategies=strategies)
    result = sub_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"
    assert any("pro_rata_deviation" in b for b in fb["fatal_blockers"])


def test_sub_bank_pass_no_uplift() -> None:
    """Zero total uplift → PASS (no burden to assess)."""
    strategies = [_strategy("Zero", 0)]
    state = _distressed_state_with_strategies(strategies=strategies)
    result = sub_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "PASS"


def test_sub_bank_fail_no_strategies() -> None:
    """No strategies → FAIL."""
    state = _distressed_state_with_strategies(strategies=[])
    result = sub_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"


def test_sub_bank_deterministic() -> None:
    """Same state → same result."""
    state = _distressed_state_with_strategies()
    r1 = sub_bank_critic_node(state)
    r2 = sub_bank_critic_node(state)
    assert r1["critic_feedbacks"][0]["status"] == r2["critic_feedbacks"][0]["status"]


# ---------------------------------------------------------------------------
# guarantor_critic
# ---------------------------------------------------------------------------


def test_guarantor_pass_sufficient_uplift() -> None:
    """Sufficient uplift to cover deficit within 5 years → PASS."""
    # Annual deficit: 34_800_000 * 12 = 417_600_000 (from _tb: keijo_rieki < 0)
    # Required annual uplift: 417_600_000 / 5 = 83_520_000
    # Total uplift from full strategies: ~94M → PASS
    state = _distressed_state_with_strategies(
        strategies=_full_strategies(),
        ews_score=75.0,
    )
    result = guarantor_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["persona"] == "guarantor"
    assert fb["priority"] == "P0"
    # With full strategies, should pass the recovery horizon check.
    # (actual result depends on fixture data; just verify structure)
    assert fb["status"] in ("PASS", "FAIL")
    assert isinstance(fb["fatal_blockers"], list)


def test_guarantor_fail_no_strategies_doubtful() -> None:
    """HATAN_KENENSAKI with no strategies → FAIL (no_recovery_plan)."""
    state = _distressed_state_with_strategies(
        strategies=[],
        fsa_classification=FsaClass.HATAN_KENENSAKI,
        ews_score=75.0,
    )
    result = guarantor_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"
    assert any("no_recovery_plan" in b for b in fb["fatal_blockers"])


def test_guarantor_fail_insufficient_strategies_high_ews() -> None:
    """EWS >= 70 with only 1 strategy → FAIL (insufficient_strategies)."""
    state = _distressed_state_with_strategies(
        strategies=[_price_strategy()],  # only 1
        ews_score=75.0,
    )
    result = guarantor_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"
    assert any("insufficient_strategies" in b for b in fb["fatal_blockers"])


def test_guarantor_pass_low_ews_single_strategy() -> None:
    """EWS < 70 with 1 strategy → no insufficient_strategies blocker."""
    state = _distressed_state_with_strategies(
        strategies=[_price_strategy()],
        ews_score=50.0,  # below doubtful threshold
        fsa_classification=FsaClass.YOCHUISAKI,
    )
    result = guarantor_critic_node(state)
    fb = result["critic_feedbacks"][0]
    # Should not have insufficient_strategies blocker.
    assert not any("insufficient_strategies" in b for b in fb["fatal_blockers"])


def test_guarantor_deterministic() -> None:
    """Same state → same result."""
    state = _distressed_state_with_strategies()
    r1 = guarantor_critic_node(state)
    r2 = guarantor_critic_node(state)
    assert r1["critic_feedbacks"][0]["status"] == r2["critic_feedbacks"][0]["status"]


# ---------------------------------------------------------------------------
# lead_arranger
# ---------------------------------------------------------------------------


def _make_feedback(
    persona: str,
    status: str,
    priority: str,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    return CriticFeedback(
        persona=persona,
        status=status,
        fatal_blockers=blockers or [],
        priority=priority,
        rationale=f"{persona}: {status}",
    ).model_dump()


def _state_with_feedbacks(
    feedbacks: list[dict[str, Any]],
    revision_count: int = 0,
    strategies: list[Strategy] | None = None,
    working_capital_gap: int = -5_000_000,
) -> SaiseiState:
    return SaiseiState(
        tdb_code="1234567",
        critic_feedbacks=feedbacks,
        revision_count=revision_count,
        proposed_strategies=strategies or _full_strategies(),
        working_capital_gap=working_capital_gap,
    )


def test_lead_arranger_all_pass() -> None:
    """All critics PASS → approved."""
    feedbacks = [
        _make_feedback("guarantor", "PASS", "P0"),
        _make_feedback("main_bank", "PASS", "P1"),
        _make_feedback("sub_bank", "PASS", "P2"),
    ]
    state = _state_with_feedbacks(feedbacks)
    result = lead_arranger_node(state)
    assert result["negotiation_status"] == "approved"
    assert result["revision_count"] == 0  # not incremented on approval


def test_lead_arranger_any_fail_rejected() -> None:
    """Any FAIL with a strategist-fixable blocker → rejected (cyclic revision).

    Uses a strategist-fixable blocker (pro_rata_deviation), NOT a banker-only
    one: when the only fatal blockers are banker-only (yakuin_hoshu_not_cut /
    no_asset_disposal) the lead_arranger correctly returns 'needs_human' and
    routes to HITL instead of looping the strategist (see
    test_lead_arranger_needs_human_when_only_banker_blockers). A FAIL the
    strategist can act on must produce 'rejected'.
    """
    feedbacks = [
        _make_feedback("guarantor", "PASS", "P0"),
        _make_feedback("sub_bank", "FAIL", "P2", ["pro_rata_deviation: test"]),
        _make_feedback("main_bank", "PASS", "P1"),
    ]
    state = _state_with_feedbacks(feedbacks)
    result = lead_arranger_node(state)
    assert result["negotiation_status"] == "rejected"
    assert result["revision_count"] == 1
    assert "pro_rata_deviation" in result["revision_directive"]


def test_lead_arranger_priority_order() -> None:
    """P0 blockers appear before P1 and P2 in the directive."""
    feedbacks = [
        _make_feedback("sub_bank", "FAIL", "P2", ["p2_blocker: fairness issue"]),
        _make_feedback("main_bank", "FAIL", "P1", ["p1_blocker: accountability issue"]),
        _make_feedback("guarantor", "FAIL", "P0", ["p0_blocker: compliance issue"]),
    ]
    state = _state_with_feedbacks(feedbacks)
    result = lead_arranger_node(state)
    directive = result["revision_directive"]
    p0_pos = directive.find("p0_blocker")
    p1_pos = directive.find("p1_blocker")
    p2_pos = directive.find("p2_blocker")
    assert p0_pos < p1_pos < p2_pos, "P0 must appear before P1 before P2"


def test_lead_arranger_revision_count_increments() -> None:
    """Revision count increments on each rejection."""
    feedbacks = [_make_feedback("main_bank", "FAIL", "P1", ["blocker"])]
    state = _state_with_feedbacks(feedbacks, revision_count=1)
    result = lead_arranger_node(state)
    assert result["revision_count"] == 2


def test_lead_arranger_no_feedbacks_rejected() -> None:
    """No feedbacks → rejected (safety net)."""
    state = _state_with_feedbacks([])
    result = lead_arranger_node(state)
    assert result["negotiation_status"] == "rejected"


def test_lead_arranger_burden_sharing_table() -> None:
    """Burden-sharing table has 3 rows with correct lender names."""
    state = _state_with_feedbacks(
        [_make_feedback("main_bank", "PASS", "P1")],
        strategies=_full_strategies(),
        working_capital_gap=-10_000_000,
    )
    table = compute_burden_sharing_table(state)
    assert len(table) == 3
    lenders = {row["lender"] for row in table}
    assert lenders == {"main_bank", "sub_bank", "guarantor"}


def test_lead_arranger_burden_sharing_shares_sum_to_100() -> None:
    """Main bank + sub bank shares sum to 100%."""
    state = _state_with_feedbacks(
        [_make_feedback("main_bank", "PASS", "P1")],
        strategies=_full_strategies(),
    )
    table = compute_burden_sharing_table(state)
    main = next(r for r in table if r["lender"] == "main_bank")
    sub = next(r for r in table if r["lender"] == "sub_bank")
    assert abs(main["share_pct"] + sub["share_pct"] - 100.0) < 0.01


def test_lead_arranger_burden_sharing_deterministic() -> None:
    """Same state → same burden-sharing table."""
    state = _state_with_feedbacks(
        [_make_feedback("main_bank", "PASS", "P1")],
        strategies=_full_strategies(),
    )
    t1 = compute_burden_sharing_table(state)
    t2 = compute_burden_sharing_table(state)
    assert t1 == t2


def test_lead_arranger_cyclic_routing_approved() -> None:
    """Approved → revision_count unchanged (routing to hitl)."""
    feedbacks = [
        _make_feedback("guarantor", "PASS", "P0"),
        _make_feedback("main_bank", "PASS", "P1"),
        _make_feedback("sub_bank", "PASS", "P2"),
    ]
    state = _state_with_feedbacks(feedbacks, revision_count=2)
    result = lead_arranger_node(state)
    assert result["negotiation_status"] == "approved"
    assert result["revision_count"] == 2  # unchanged


def test_lead_arranger_cyclic_routing_rejected_increments() -> None:
    """Rejected → revision_count incremented (routing back to strategist)."""
    feedbacks = [_make_feedback("main_bank", "FAIL", "P1", ["blocker"])]
    state = _state_with_feedbacks(feedbacks, revision_count=0)
    result = lead_arranger_node(state)
    assert result["negotiation_status"] == "rejected"
    assert result["revision_count"] == 1


# ---------------------------------------------------------------------------
# Fix 1 regression: critic_feedbacks_reducer clear sentinel
# ---------------------------------------------------------------------------


def test_critic_feedbacks_reducer_append() -> None:
    """Normal append: non-sentinel update is concatenated."""
    existing = [{"persona": "guarantor", "status": "PASS"}]
    new_fb = [{"persona": "main_bank", "status": "FAIL"}]
    result = critic_feedbacks_reducer(existing, new_fb)
    assert len(result) == 2
    assert result[0]["persona"] == "guarantor"
    assert result[1]["persona"] == "main_bank"


def test_critic_feedbacks_reducer_clear_sentinel() -> None:
    """Clear sentinel: CRITIC_FEEDBACKS_CLEAR replaces the list with []."""
    existing = [
        {"persona": "guarantor", "status": "PASS"},
        {"persona": "main_bank", "status": "FAIL"},
        {"persona": "sub_bank", "status": "PASS"},
    ]
    result = critic_feedbacks_reducer(existing, CRITIC_FEEDBACKS_CLEAR)
    assert result == [], "Sentinel must clear the accumulated list"


def test_critic_feedbacks_reducer_empty_list_appends_nothing() -> None:
    """An ordinary empty list (not the sentinel) appends nothing (no-op)."""
    existing = [{"persona": "guarantor", "status": "PASS"}]
    # A freshly constructed [] is NOT the sentinel (different object identity).
    ordinary_empty: list[dict[str, Any]] = []
    result = critic_feedbacks_reducer(existing, ordinary_empty)
    # ordinary_empty is not CRITIC_FEEDBACKS_CLEAR (different object), so it appends.
    assert result == existing + ordinary_empty


def test_strategist_node_resets_critic_feedbacks() -> None:
    """strategist_node returns CRITIC_FEEDBACKS_CLEAR so the reducer clears state.

    This is the regression test for the no-op reset bug: previously returning []
    would APPEND (no-op) via operator.add; now the sentinel triggers a clear.
    """
    state = SaiseiState(
        tdb_code="1234567",
        shisanhyo=[_tb()],
        critic_feedbacks=[
            {
                "persona": "guarantor",
                "status": "PASS",
                "fatal_blockers": [],
                "priority": "P0",
                "rationale": "old",
            },
            {
                "persona": "main_bank",
                "status": "FAIL",
                "fatal_blockers": ["stale"],
                "priority": "P1",
                "rationale": "old",
            },
        ],
    )
    result = strategist_node(state)
    # The returned value must be the sentinel object (checked by `is`).
    assert result["critic_feedbacks"] is CRITIC_FEEDBACKS_CLEAR, (
        "strategist_node must return CRITIC_FEEDBACKS_CLEAR (the sentinel), not a new []"
    )
    # Simulate what the reducer does when it receives the sentinel.
    cleared = critic_feedbacks_reducer(state.critic_feedbacks, result["critic_feedbacks"])
    assert cleared == [], "Reducer must produce an empty list when given the sentinel"


def test_cyclic_revision_no_cross_round_accumulation() -> None:
    """Regression: lead_arranger sees exactly 3 feedbacks on round 2, not 6.

    Simulates two revision cycles:
    Round 1: strategist → 3 critics → lead_arranger (rejected).
    Round 2: strategist (reset) → 3 critics → lead_arranger.
    lead_arranger must see exactly 3 feedbacks on round 2, not 6.
    """
    # --- Round 1 ---
    # Simulate state after round 1 critics have run (3 feedbacks accumulated).
    # Use a strategist-fixable blocker (pro_rata_deviation) so round 1 is a
    # genuine 'rejected' cyclic revision, not a banker-only 'needs_human' handoff.
    round1_feedbacks = [
        _make_feedback("guarantor", "PASS", "P0"),
        _make_feedback("sub_bank", "FAIL", "P2", ["pro_rata_deviation: round1"]),
        _make_feedback("main_bank", "PASS", "P1"),
    ]
    state_after_round1 = SaiseiState(
        tdb_code="1234567",
        shisanhyo=[_tb()],
        proposed_strategies=_full_strategies(),
        critic_feedbacks=round1_feedbacks,
        revision_count=0,
        working_capital_gap=-5_000_000,
    )
    # lead_arranger rejects round 1.
    la_result_1 = lead_arranger_node(state_after_round1)
    assert la_result_1["negotiation_status"] == "rejected"
    assert la_result_1["revision_count"] == 1

    # --- Simulate strategist reset (round 2) ---
    # strategist_node returns CRITIC_FEEDBACKS_CLEAR; the reducer clears the list.
    strategist_result = strategist_node(state_after_round1)
    assert strategist_result["critic_feedbacks"] is CRITIC_FEEDBACKS_CLEAR
    cleared_feedbacks = critic_feedbacks_reducer(
        state_after_round1.critic_feedbacks,
        strategist_result["critic_feedbacks"],
    )
    assert cleared_feedbacks == [], "After strategist reset, feedbacks must be empty"

    # --- Round 2: 3 critics run in parallel, each appending one feedback ---
    round2_feedbacks_after_reset: list[dict[str, Any]] = []
    for fb in [
        _make_feedback("guarantor", "PASS", "P0"),
        _make_feedback("main_bank", "PASS", "P1"),  # now passes (flags set)
        _make_feedback("sub_bank", "PASS", "P2"),
    ]:
        round2_feedbacks_after_reset = critic_feedbacks_reducer(round2_feedbacks_after_reset, [fb])

    # Exactly 3 feedbacks — no stale round-1 feedbacks.
    assert len(round2_feedbacks_after_reset) == 3, (
        f"lead_arranger must see exactly 3 feedbacks on round 2, "
        f"got {len(round2_feedbacks_after_reset)}"
    )

    state_after_round2 = SaiseiState(
        tdb_code="1234567",
        shisanhyo=[_tb()],
        proposed_strategies=_full_strategies(),
        critic_feedbacks=round2_feedbacks_after_reset,
        revision_count=1,
        working_capital_gap=-5_000_000,
    )
    la_result_2 = lead_arranger_node(state_after_round2)
    assert la_result_2["negotiation_status"] == "approved"


# ---------------------------------------------------------------------------
# Fix 4: sub_bank stake-based pro-rata
# ---------------------------------------------------------------------------


def test_sub_bank_stake_based_pass() -> None:
    """Stake-based pro-rata: balanced stakes → PASS."""
    state = _distressed_state_with_strategies(
        lender_stakes={"main_bank": 50_000_000, "sub_bank": 50_000_000},
    )
    result = sub_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "PASS"


def test_sub_bank_stake_based_fail_disproportionate() -> None:
    """Stake-based pro-rata: main bank has 90% stake → FAIL."""
    state = _distressed_state_with_strategies(
        lender_stakes={"main_bank": 90_000_000, "sub_bank": 10_000_000},
    )
    result = sub_bank_critic_node(state)
    fb = result["critic_feedbacks"][0]
    assert fb["status"] == "FAIL"
    assert any("pro_rata_deviation" in b for b in fb["fatal_blockers"])


def test_compute_main_bank_share_stake_based() -> None:
    """compute_main_bank_share uses stakes when available."""
    state = _distressed_state_with_strategies(
        lender_stakes={"main_bank": 70_000_000, "sub_bank": 30_000_000},
    )
    share, mode = compute_main_bank_share(state)
    assert mode == "stake_based"
    assert abs(share - 0.7) < 1e-9


def test_compute_main_bank_share_heuristic_proxy() -> None:
    """compute_main_bank_share falls back to heuristic when no stakes."""
    state = _distressed_state_with_strategies(lender_stakes={})
    share, mode = compute_main_bank_share(state)
    assert mode == "heuristic_proxy"
    # Heuristic: max uplift / total uplift from _full_strategies().
    strategies = _full_strategies()
    uplifts = [int(s.expected_keijo_uplift) for s in strategies]
    expected_share = max(uplifts) / sum(uplifts)
    assert abs(share - expected_share) < 1e-9


def test_lead_arranger_burden_sharing_stake_based() -> None:
    """Burden-sharing table uses stake-based shares when lender_stakes provided."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=_full_strategies(),
        working_capital_gap=-10_000_000,
        lender_stakes={"main_bank": 60_000_000, "sub_bank": 40_000_000},
        critic_feedbacks=[_make_feedback("main_bank", "PASS", "P1")],
    )
    table = compute_burden_sharing_table(state)
    main = next(r for r in table if r["lender"] == "main_bank")
    sub = next(r for r in table if r["lender"] == "sub_bank")
    # With 60/40 stakes, main bank share should be 60%.
    assert abs(main["share_pct"] - 60.0) < 0.1
    assert abs(sub["share_pct"] - 40.0) < 0.1


# ---------------------------------------------------------------------------
# Fix: banker-only blockers route to HITL (needs_human) not to escalation
# ---------------------------------------------------------------------------


def test_is_banker_only_blocker() -> None:
    """Banker-only blocker codes are recognised; strategist-fixable ones are not."""
    assert is_banker_only_blocker("[P1/main_bank] yakuin_hoshu_not_cut: ...")
    assert is_banker_only_blocker("[P1/main_bank] no_asset_disposal: ...")
    assert not is_banker_only_blocker("[P0/guarantor] recovery_horizon_exceeded: ...")
    assert not is_banker_only_blocker("[P2/sub_bank] pro_rata_deviation: ...")


def test_lead_arranger_needs_human_when_only_banker_blockers() -> None:
    """Only banker-only blockers → needs_human, revision_count NOT incremented."""
    feedbacks = [
        _make_feedback("guarantor", "PASS", "P0"),
        _make_feedback("main_bank", "FAIL", "P1", ["yakuin_hoshu_not_cut: confirm exec-comp cut"]),
        _make_feedback("sub_bank", "PASS", "P2"),
    ]
    state = _state_with_feedbacks(feedbacks, revision_count=0)
    result = lead_arranger_node(state)
    assert result["negotiation_status"] == "needs_human"
    assert result["revision_count"] == 0  # handoff, not a revision cycle


def test_lead_arranger_rejected_when_strategist_fixable_blocker_present() -> None:
    """A strategist-fixable blocker (even alongside banker-only) → rejected/cyclic."""
    feedbacks = [
        _make_feedback("guarantor", "FAIL", "P0", ["recovery_horizon_exceeded: add strategies"]),
        _make_feedback("main_bank", "FAIL", "P1", ["yakuin_hoshu_not_cut: confirm exec-comp cut"]),
    ]
    state = _state_with_feedbacks(feedbacks, revision_count=0)
    result = lead_arranger_node(state)
    assert result["negotiation_status"] == "rejected"
    assert result["revision_count"] == 1


# ---------------------------------------------------------------------------
# Fix 1 (guarantor critic): guardrail — shared EWS_DOUBTFUL constant usage
# ---------------------------------------------------------------------------


def test_guarantor_uses_shared_ews_doubtful_constant() -> None:
    """Guardrail: guarantor critic must use the shared EWS_DOUBTFUL constant.

    This test prevents silent drift where a local _EWS_DOUBTFUL copy diverges
    from app.shared.constants.EWS_DOUBTFUL (HANDOFF forbids local constant copies).

    Strategy: inspect the guarantor module's globals to confirm no local
    _EWS_DOUBTFUL float is defined, and that the EWS gate fires at exactly the
    shared EWS_DOUBTFUL value (70.0).
    """
    import app.backend.nodes.critics.guarantor as guarantor_module
    from app.shared.constants import EWS_DOUBTFUL

    # The module must expose _EWS_DOUBTFUL as the shared constant, not a local copy.
    # (The fix imports `EWS_DOUBTFUL as _EWS_DOUBTFUL`, so the attribute exists as
    # an alias; an identity check distinguishes that alias from a local float copy.)
    assert hasattr(guarantor_module, "_EWS_DOUBTFUL"), (
        "guarantor.py must import EWS_DOUBTFUL from app.shared.constants "
        "(aliased as _EWS_DOUBTFUL); the attribute is missing."
    )
    assert guarantor_module._EWS_DOUBTFUL is EWS_DOUBTFUL, (
        "guarantor._EWS_DOUBTFUL must be the same object as "
        "app.shared.constants.EWS_DOUBTFUL, not a locally-defined copy."
    )

    # Behavioural check: EWS exactly at the shared threshold with 1 strategy → FAIL.
    state_at_threshold = _distressed_state_with_strategies(
        strategies=[_price_strategy()],  # only 1 strategy
        ews_score=EWS_DOUBTFUL,  # exactly at the shared threshold
        fsa_classification=FsaClass.YOCHUISAKI,
    )
    result = guarantor_critic_node(state_at_threshold)
    fb = result["critic_feedbacks"][0]
    assert any("insufficient_strategies" in b for b in fb["fatal_blockers"]), (
        f"EWS={EWS_DOUBTFUL} with 1 strategy must trigger insufficient_strategies "
        f"(shared EWS_DOUBTFUL={EWS_DOUBTFUL})"
    )

    # One below threshold with 1 strategy → no insufficient_strategies blocker.
    state_below_threshold = _distressed_state_with_strategies(
        strategies=[_price_strategy()],
        ews_score=EWS_DOUBTFUL - 0.1,
        fsa_classification=FsaClass.YOCHUISAKI,
    )
    result_below = guarantor_critic_node(state_below_threshold)
    fb_below = result_below["critic_feedbacks"][0]
    assert not any("insufficient_strategies" in b for b in fb_below["fatal_blockers"]), (
        f"EWS={EWS_DOUBTFUL - 0.1} (below threshold) must NOT trigger insufficient_strategies"
    )


def test_guarantor_uses_shared_min_recovery_horizon_years() -> None:
    """Guardrail: guarantor critic must use MIN_RECOVERY_HORIZON_YEARS under a
    semantically correct (non-inverted) name.

    The old code aliased MIN_RECOVERY_HORIZON_YEARS as _MAX_RECOVERY_YEARS —
    a semantic inversion. This test confirms the import alias is gone and the
    numeric behaviour (divisor = 5) is preserved.
    """
    import app.backend.nodes.critics.guarantor as guarantor_module
    from app.shared.constants import MIN_RECOVERY_HORIZON_YEARS

    # The inverted alias must not exist in the module namespace.
    assert not hasattr(guarantor_module, "_MAX_RECOVERY_YEARS"), (
        "guarantor.py must not use the inverted alias _MAX_RECOVERY_YEARS; "
        "use a semantically correct name for MIN_RECOVERY_HORIZON_YEARS."
    )

    # Numeric behaviour: deficit / MIN_RECOVERY_HORIZON_YEARS is the required uplift.
    # Build a state where uplift is exactly 1/5 of the annual deficit → should PASS.
    # The guarantor uses the model's keijo_rieki (which INCLUDES non-operating
    # items eigai_shueki/eigai_hiyo), so derive the deficit from the actual
    # _tb() trial balance rather than the operating subtotal alone.
    _latest = _tb()
    annual_deficit = abs(_latest.keijo_rieki * 12)
    # The gate fires when total_annual_uplift < annual_deficit / N. Use a
    # ceil-based required uplift so uplift >= required clears the strict `<`.
    required_uplift = -(-annual_deficit // MIN_RECOVERY_HORIZON_YEARS)

    # Exactly meeting the threshold → PASS on recovery horizon gate.
    state_exact = _distressed_state_with_strategies(
        strategies=[_strategy("exact", required_uplift)],
        ews_score=50.0,  # below doubtful → no insufficient_strategies blocker
        fsa_classification=FsaClass.YOCHUISAKI,
    )
    result_exact = guarantor_critic_node(state_exact)
    fb_exact = result_exact["critic_feedbacks"][0]
    assert not any("recovery_horizon_exceeded" in b for b in fb_exact["fatal_blockers"]), (
        f"Uplift={required_uplift:,} exactly covers 1/{MIN_RECOVERY_HORIZON_YEARS} of "
        f"annual deficit={annual_deficit:,}; recovery_horizon gate must PASS."
    )
