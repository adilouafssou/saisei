"""Regression: burden-sharing must not discard real stake data.

``compute_burden_sharing_table`` previously forced ``main_bank_share = 0.5``
whenever there were no proposed strategies. But ``compute_main_bank_share``
returns a TRUE stake-based pro-rata from ``lender_stakes`` (the banker-supplied
outstanding balances) independently of strategies, so the override silently
threw that real data away and mislabelled the allocation. The fix keeps the
stake-based share and only defaults to 50/50 for the strategy-less heuristic
path. Fully offline.
"""

from __future__ import annotations

from app.backend.nodes.lead_arranger import compute_burden_sharing_table
from app.backend.state import SaiseiState, Strategy


def _state(**kwargs: object) -> SaiseiState:
    return SaiseiState(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        **kwargs,
    )


def test_stake_based_share_survives_no_strategies() -> None:
    """With real stakes but no strategies, the split stays stake-based, not 50/50."""
    state = _state(
        proposed_strategies=[],
        lender_stakes={"main_bank": 800, "sub_bank": 200},
        working_capital_gap=-1_000,
    )
    table = compute_burden_sharing_table(state)
    main = next(r for r in table if r["lender"] == "main_bank")
    sub = next(r for r in table if r["lender"] == "sub_bank")

    assert main["share_basis"] == "stake_based"
    assert main["share_pct"] == 80.0  # 800 / 1000, NOT 50.0
    assert sub["share_pct"] == 20.0
    assert main["allocation_type"] == "main_bank_heavy"  # > 0.5
    # New-money ask follows the real share (¥1,000 deficit -> 800 / 200).
    assert main["new_money_jpy"] == 800
    assert sub["new_money_jpy"] == 200


def test_no_stakes_no_strategies_defaults_balanced() -> None:
    """Without stakes AND without strategies, the split defaults to 50/50."""
    state = _state(proposed_strategies=[], lender_stakes={}, working_capital_gap=0)
    table = compute_burden_sharing_table(state)
    main = next(r for r in table if r["lender"] == "main_bank")

    assert main["share_basis"] == "heuristic_proxy"
    assert main["share_pct"] == 50.0
    assert main["allocation_type"] == "pro_rata"


def test_new_money_shares_reconcile_to_total_on_rounding_boundary() -> None:
    """main + sub new-money must sum to the exact deficit, even at a .5-yen split.

    Regression: rounding the two shares INDEPENDENTLY drifted by ¥1 on a .5-yen
    boundary (total=5, 50/50 -> round(2.5)+round(2.5) = 2+2 = 4 != 5), leaving a
    burden table whose columns did not reconcile to the stated deficit. The
    sub-bank share is now the exact remainder, so the columns always sum.
    """
    # 50/50 split (no stakes, equal uplifts) over a ¥5 deficit -> the exact
    # boundary the independent-round bug under-allocated.
    state = _state(
        proposed_strategies=[
            Strategy(title="a", rationale="r", expected_keijo_uplift=100),
            Strategy(title="b", rationale="r", expected_keijo_uplift=100),
        ],
        lender_stakes={},
        working_capital_gap=-5,
    )
    table = compute_burden_sharing_table(state)
    main = next(r for r in table if r["lender"] == "main_bank")
    sub = next(r for r in table if r["lender"] == "sub_bank")
    guarantor = next(r for r in table if r["lender"] == "guarantor")

    total_allocated = main["new_money_jpy"] + sub["new_money_jpy"] + guarantor["new_money_jpy"]
    assert total_allocated == 5, (
        f"new-money columns must reconcile to the ¥5 deficit, got {total_allocated}"
    )


def test_new_money_reconciles_across_many_deficits() -> None:
    """For any deficit, main + sub + guarantor new-money equals the deficit."""
    for deficit in (1, 3, 5, 7, 11, 999, 1_000_001):
        state = _state(
            proposed_strategies=[],
            lender_stakes={"main_bank": 1, "sub_bank": 2},  # 1/3 vs 2/3
            working_capital_gap=-deficit,
        )
        table = compute_burden_sharing_table(state)
        allocated = sum(r["new_money_jpy"] for r in table)
        assert allocated == deficit, f"deficit ¥{deficit}: columns summed to {allocated}"
