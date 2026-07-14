"""Deterministic golden-eval harness for the Saisei spine.

CI-gated, offline. Drives three borrower archetypes through the compiled graph
using the existing :class:`~app.backend.tools.provider.MockDataProvider` and
asserts the deterministic spine to the yen:

* FSA classification (five FSA bands via :class:`~app.shared.models.classification.FsaClass`)
* ``special_attention`` sub-tier flag (要管理先)
* ``negotiation_status`` and ``working_capital_gap``
* Burden-sharing table produced by
  :func:`~app.backend.nodes.lead_arranger.compute_burden_sharing_table`
  (``share_pct``, ``grace_period_months``, ``haircut_pct``, ``new_money_jpy``)

**Offline-by-default contract**: all three fixtures use the
:class:`~app.backend.tools.provider.MockDataProvider` (no network calls).
The graph compiles without a Postgres checkpointer (``MemorySaver`` only) so
no database is required.

**Fixture summary**:

+----------+----------+-------------------+-------------------+------------------+
| Fixture  | TDB code | FSA band          | special_attention | Path             |
+==========+==========+===================+===================+==================+
| Normal   | 2000001  | 正常先 (SEIJOSAKI)| False             | → END (monitor)  |
+----------+----------+-------------------+-------------------+------------------+
| Needs-   | 3000001  | 要注意先          | True              | → turnaround     |
| Attention|          | (YOCHUISAKI)      | (要管理先)        | (HITL pause)     |
+----------+----------+-------------------+-------------------+------------------+
| Insolvent| 1234567  | 実質破綻先        | False             | → workout        |
|          |          | (JISSHITSU_       |                   | (terminal)       |
|          |          | HATANSAKI)        |                   |                  |
+----------+----------+-------------------+-------------------+------------------+

Expected values are computed by reading the actual rule code
(``constants.py`` thresholds, ``lead_arranger.py`` burden-sharing math,
``ews_scoring.py`` classify logic) — not guessed.
"""

from __future__ import annotations

from typing import cast

from app.backend.graph import build_graph
from app.backend.nodes.ews_scoring import classify, compute_ews_score
from app.backend.nodes.financial_extraction import estimate_working_capital_gap
from app.backend.nodes.lead_arranger import compute_burden_sharing_table
from app.backend.state import SaiseiState, Strategy
from app.backend.tools.provider import MockDataProvider
from app.shared.models.classification import FsaClass
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MEMORY = MemorySaver


def _compiled() -> CompiledStateGraph[SaiseiState]:
    """Return a compiled graph with an in-memory checkpointer (no Postgres)."""
    return build_graph().compile(checkpointer=_MEMORY())


def _cfg(thread_id: str) -> RunnableConfig:
    """Return a LangGraph config dict for the given thread id."""
    return {"configurable": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# Fixture 1: Normal borrower (正常先 / SEIJOSAKI)
#
# TDB code 2000001 → normal_service_co.json
# Service company: flat sales, very low COGS, no losses.
#
# Expected EWS derivation (compute_ews_score):
#   sales_drop  = 0.0  (flat sales)
#   margin_drop = 0.0  (flat margins)
#   keijo_drop  = 0.0  (flat profit)
#   loss_ratio  = 0.0  (no loss months)
#   EWS = 0.0
#
# Expected working_capital_gap derivation (estimate_working_capital_gap):
#   latest: uriage=100_000_000, uriage_genka=1_000_000, hanbaihi=10_000_000
#   eigyo_rieki = (100M - 1M) - 10M = 89_000_000
#   cash_cycle_days = receivable_days(95) - payable_days(45) = 50
#   daily_cogs = 1_000_000 / 30.0 = 33_333.333...
#   financing_req = 50 * 33_333.333 = 1_666_666.667
#   rate_stress = 1 + 60/10_000 = 1.006  (latest BOJ bps = 60)
#   buffer = 89_000_000 * (50 / 30.0) = 148_333_333.333
#   gap = 148_333_333.333 - 1_666_666.667 * 1.006 = 146_656_666.667
#   int(round(...)) = 146_656_667
#
# classify(ews=0.0, gap=146_656_667, tdb=75) → SEIJOSAKI, special_attention=False
# ---------------------------------------------------------------------------

_NORMAL_TDB = "2000001"
_NORMAL_EXPECTED_EWS = 0.0
_NORMAL_EXPECTED_GAP = 146_656_667
_NORMAL_EXPECTED_FSA = FsaClass.SEIJOSAKI


def test_normal_borrower_routes_to_end() -> None:
    """正常先 borrower must classify as SEIJOSAKI and terminate without turnaround."""
    app = _compiled()
    cfg = _cfg("eval-normal-01")
    app.invoke(cast("SaiseiState", {"tdb_code": _NORMAL_TDB}), config=cfg)
    snapshot = app.get_state(cfg)

    # Graph must have completed (no pending nodes).
    assert not snapshot.next, "Normal borrower must reach END, not pause at HITL"

    values = snapshot.values

    # FSA classification spine.
    assert values["fsa_classification"] is _NORMAL_EXPECTED_FSA
    assert values["special_attention"] is False

    # EWS score (deterministic from flat financials).
    assert values["ews_score"] == _NORMAL_EXPECTED_EWS

    # Working-capital gap (positive → no deficit).
    assert values["working_capital_gap"] == _NORMAL_EXPECTED_GAP
    assert values["working_capital_gap"] > 0

    # No turnaround artefacts. The Normal borrower terminates at END before the
    # strategist, so the proposed_strategies channel is never written; LangGraph
    # omits unwritten channels from snapshot.values, so read the default.
    assert values.get("proposed_strategies", []) == []
    assert values["negotiation_status"] == "pending"


def test_normal_borrower_is_deterministic() -> None:
    """Two identical runs of the Normal borrower must produce identical state."""
    app_a = _compiled()
    app_b = _compiled()
    cfg_a = _cfg("eval-normal-det-a")
    cfg_b = _cfg("eval-normal-det-b")

    app_a.invoke(cast("SaiseiState", {"tdb_code": _NORMAL_TDB}), config=cfg_a)
    app_b.invoke(cast("SaiseiState", {"tdb_code": _NORMAL_TDB}), config=cfg_b)

    vals_a = app_a.get_state(cfg_a).values
    vals_b = app_b.get_state(cfg_b).values

    assert vals_a["fsa_classification"] == vals_b["fsa_classification"]
    assert vals_a["ews_score"] == vals_b["ews_score"]
    assert vals_a["working_capital_gap"] == vals_b["working_capital_gap"]
    assert vals_a["special_attention"] == vals_b["special_attention"]


# ---------------------------------------------------------------------------
# Fixture 2: Needs-Attention borrower with working-capital deficit
#            (要注意先 / YOCHUISAKI + special_attention=True → 要管理先)
#
# TDB code 3000001 → needs_attention_mfg.json
# Manufacturing company: 11 months stable, month 12 slight deterioration.
#
# Expected EWS derivation (compute_ews_score):
#   first = month 1: uriage=80_000_000, uriage_genka=60_000_000
#   last  = month 12: uriage=79_000_000, uriage_genka=61_000_000
#   sales_drop  = (80M - 79M) / 80M = 1/80 = 0.0125
#   margin_first = (80M - 60M) / 80M = 20/80 = 0.25
#   margin_last  = (79M - 61M) / 79M = 18/79 ≈ 0.22785
#   margin_drop  = max(0, 0.25 - 0.22785) ≈ 0.02215
#   keijo_first  = (80M - 60M) - 15M = 5_000_000
#   keijo_last   = (79M - 61M) - 15M = 3_000_000
#   keijo_drop   = max(0, (5M - 3M) / 5M) = 0.4
#   loss_months  = 0  (all months profitable)
#   trend        = sustained-deterioration ratio over the full 12-month series
#   EWS (five-signal model: sales 22 / margin 26 / keijo 27 / loss 13 / trend 12)
#       ≈ 0.825 + 5.76 + 10.8 + 0.0 + (12 * trend_ratio)
#   The exact magnitude is NOT asserted here (the band is): EWS < EWS_SUBSTANDARD
#   (40), so the classification is driven by the working-capital deficit below.
#
# Expected working_capital_gap derivation (estimate_working_capital_gap):
#   latest: uriage=79_000_000, uriage_genka=61_000_000, hanbaihi=15_000_000
#   eigyo_rieki = (79M - 61M) - 15M = 3_000_000
#   cash_cycle_days = 50
#   daily_cogs = 61_000_000 / 30.0 = 2_033_333.333...
#   financing_req = 50 * 2_033_333.333 = 101_666_666.667
#   rate_stress = 1.006
#   buffer = 3_000_000 * (50 / 30.0) = 5_000_000.0
#   gap = 5_000_000.0 - 101_666_666.667 * 1.006 = -97_276_666.667
#   int(round(...)) = -97_276_667
#
# classify(ews≈19.58, gap=-97_276_667, tdb=55):
#   EWS < 40 → not HATAN_KENENSAKI from EWS alone
#   deficit AND EWS >= 40? No (EWS < 40) → not HATAN_KENENSAKI
#   EWS >= 40? No; deficit? Yes → YOCHUISAKI, special_attention=True
# ---------------------------------------------------------------------------

_NEEDS_ATTN_TDB = "3000001"
_NEEDS_ATTN_EXPECTED_FSA = FsaClass.YOCHUISAKI
_NEEDS_ATTN_EXPECTED_GAP = -97_276_667


def test_needs_attention_borrower_classification() -> None:
    """要注意先 borrower with deficit must classify as YOCHUISAKI + special_attention=True."""
    app = _compiled()
    cfg = _cfg("eval-needs-attn-01")
    # Set commitment flags so the main_bank critic PASSes and the graph reaches HITL.
    app.invoke(
        cast(
            "SaiseiState",
            {
                "tdb_code": _NEEDS_ATTN_TDB,
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            },
        ),
        config=cfg,
    )
    snapshot = app.get_state(cfg)

    # Graph must pause at HITL (turnaround path).
    assert snapshot.next, "Needs-Attention borrower must reach the HITL interrupt"

    values = snapshot.values

    # FSA classification spine — exact band.
    assert values["fsa_classification"] is _NEEDS_ATTN_EXPECTED_FSA

    # 要管理先 sub-tier: special_attention must be True (deficit present).
    assert values["special_attention"] is True

    # Working-capital gap must be the expected deficit (exact integer yen).
    assert values["working_capital_gap"] == _NEEDS_ATTN_EXPECTED_GAP
    assert values["working_capital_gap"] < 0

    # Turnaround path must have produced strategies.
    assert len(values["proposed_strategies"]) >= 3

    # negotiation_status must be approved (commitment flags set).
    assert values["negotiation_status"] == "approved"


def test_needs_attention_strategies_are_deterministic() -> None:
    """Proposed strategies for the Needs-Attention borrower must be deterministic.

    The strategist is a pure function of the latest trial balance; two identical
    runs must produce byte-identical strategy lists.
    """
    app_a = _compiled()
    app_b = _compiled()
    cfg_a = _cfg("eval-needs-attn-det-a")
    cfg_b = _cfg("eval-needs-attn-det-b")

    for app, cfg in [(app_a, cfg_a), (app_b, cfg_b)]:
        app.invoke(
            cast(
                "SaiseiState",
                {
                    "tdb_code": _NEEDS_ATTN_TDB,
                    "yakuin_hoshu_cut": True,
                    "personal_asset_disposal": True,
                },
            ),
            config=cfg,
        )

    snap_a = app_a.get_state(cfg_a).values
    snap_b = app_b.get_state(cfg_b).values

    strats_a = snap_a["proposed_strategies"]
    strats_b = snap_b["proposed_strategies"]

    assert len(strats_a) == len(strats_b)
    for s_a, s_b in zip(strats_a, strats_b, strict=True):
        assert s_a["title"] == s_b["title"]
        assert s_a["expected_keijo_uplift"] == s_b["expected_keijo_uplift"]


# ---------------------------------------------------------------------------
# Fixture 3: Insolvent borrower (実質破綻先 / JISSHITSU_HATANSAKI)
#
# Uses the existing Aichi manufacturer fixture (TDB 1234567) with an explicit
# net_worth < 0 override in the initial state. The classifier's Band 2 rule:
#   net_worth < 0 → JISSHITSU_HATANSAKI (regardless of EWS)
# routes the borrower to the workout node (terminal).
# ---------------------------------------------------------------------------

_INSOLVENT_TDB = "1234567"
_INSOLVENT_NET_WORTH = -5_000_000
_INSOLVENT_EXPECTED_FSA = FsaClass.JISSHITSU_HATANSAKI


def test_insolvent_borrower_routes_to_workout() -> None:
    """実質破綻先 borrower (net_worth < 0) must route to workout and terminate."""
    app = _compiled()
    cfg = _cfg("eval-insolvent-01")
    app.invoke(
        cast(
            "SaiseiState",
            {
                "tdb_code": _INSOLVENT_TDB,
                "net_worth": _INSOLVENT_NET_WORTH,
            },
        ),
        config=cfg,
    )
    snapshot = app.get_state(cfg)

    # Graph must have completed (workout is terminal).
    assert not snapshot.next, "Insolvent borrower must reach END via workout"

    values = snapshot.values

    # FSA classification spine — exact band.
    assert values["fsa_classification"] is _INSOLVENT_EXPECTED_FSA

    # special_attention is False for bankrupt bands.
    assert values["special_attention"] is False

    # Workout handoff must be populated.
    assert values["workout_handoff"] is not None
    assert "実質破綻先" in values["workout_handoff"]
    assert "WORKOUT HANDOFF" in values["workout_handoff"]

    # No turnaround artefacts. The insolvent borrower routes straight to workout
    # (terminal) before the strategist, so the proposed_strategies channel is
    # never written; LangGraph omits unwritten channels from snapshot.values.
    assert values.get("proposed_strategies", []) == []


def test_insolvent_borrower_is_deterministic() -> None:
    """Two identical runs of the Insolvent borrower must produce identical state."""
    app_a = _compiled()
    app_b = _compiled()
    cfg_a = _cfg("eval-insolvent-det-a")
    cfg_b = _cfg("eval-insolvent-det-b")

    app_a.invoke(
        cast("SaiseiState", {"tdb_code": _INSOLVENT_TDB, "net_worth": _INSOLVENT_NET_WORTH}),
        config=cfg_a,
    )
    app_b.invoke(
        cast("SaiseiState", {"tdb_code": _INSOLVENT_TDB, "net_worth": _INSOLVENT_NET_WORTH}),
        config=cfg_b,
    )

    vals_a = app_a.get_state(cfg_a).values
    vals_b = app_b.get_state(cfg_b).values

    assert vals_a["fsa_classification"] == vals_b["fsa_classification"]
    assert vals_a["special_attention"] == vals_b["special_attention"]
    assert vals_a["workout_handoff"] == vals_b["workout_handoff"]


# ---------------------------------------------------------------------------
# Burden-sharing table — exact integer-yen assertions
#
# compute_burden_sharing_table is a pure function; we drive it directly with
# a constructed SaiseiState so the expected values are reproducible from the
# rule code alone (no graph execution required).
#
# Test A — Heuristic proxy mode (lender_stakes empty):
#   proposed_strategies = [uplift=60M, uplift=40M]
#   total_uplift = 100_000_000
#   max_uplift   = 60_000_000
#   main_bank_share = 60_000_000 / 100_000_000 = 0.6  (exact float)
#   sub_bank_share  = 0.4
#
#   main_bank row:
#     share_pct          = round(0.6 * 100, 1)       = 60.0
#     grace_period_months= round(12 + 6 * 0.6)       = round(15.6) = 16
#     haircut_pct        = round(0.6 * 30, 1)         = 18.0
#     new_money_jpy      = round(60_000_000 * 0.6)   = 36_000_000
#     allocation_type    = "main_bank_heavy"  (0.6 > 0.5)
#
#   sub_bank row:
#     share_pct          = round(0.4 * 100, 1)       = 40.0
#     grace_period_months= max(6, min(round(12*0.4*2), 12))
#                        = max(6, min(round(9.6), 12))
#                        = max(6, min(10, 12)) = 10
#     haircut_pct        = round(0.4 * 15, 1)         = 6.0
#     new_money_jpy      = round(60_000_000 * 0.4)   = 24_000_000
#     allocation_type    = "pro_rata"
#
#   guarantor row:
#     share_pct = 0.0, grace_period_months = 0, haircut_pct = 0.0,
#     new_money_jpy = 0, allocation_type = "guarantee_only"
#
# Test B — Stake-based mode (lender_stakes populated):
#   lender_stakes = {"main_bank": 300_000_000, "sub_bank": 200_000_000}
#   total_stakes = 500_000_000
#   main_bank_share = 300_000_000 / 500_000_000 = 0.6  (exact float)
#   working_capital_gap = -50_000_000
#
#   main_bank row:
#     share_pct          = 60.0
#     grace_period_months= 16
#     haircut_pct        = 18.0
#     new_money_jpy      = round(50_000_000 * 0.6) = 30_000_000
#
#   sub_bank row:
#     share_pct          = 40.0
#     grace_period_months= 10
#     haircut_pct        = 6.0
#     new_money_jpy      = round(50_000_000 * 0.4) = 20_000_000
# ---------------------------------------------------------------------------


def _make_state(
    gap: int,
    uplifts: list[int],
    lender_stakes: dict[str, int] | None = None,
) -> SaiseiState:
    """Build a minimal SaiseiState for burden-sharing table tests.

    Args:
        gap: working_capital_gap (negative = deficit).
        uplifts: List of expected_keijo_uplift values for proposed strategies.
        lender_stakes: Optional per-lender outstanding balances.

    Returns:
        A :class:`SaiseiState` with the given inputs.
    """
    strategies = [
        Strategy(
            title=f"Strategy {i}",
            rationale="Test strategy.",
            expected_keijo_uplift=u,
        )
        for i, u in enumerate(uplifts)
    ]
    return SaiseiState(
        tdb_code="0000001",
        working_capital_gap=gap,
        proposed_strategies=strategies,
        lender_stakes=lender_stakes or {},
    )


def test_burden_sharing_heuristic_proxy_exact_values() -> None:
    """Burden-sharing table (heuristic proxy mode) must match exact yen values.

    main_bank_share = max_uplift / total_uplift = 60M / 100M = 0.6 (exact).
    All expected values are derived from the rule code with no guessing.
    """
    state = _make_state(gap=-60_000_000, uplifts=[60_000_000, 40_000_000])
    table = compute_burden_sharing_table(state)

    assert len(table) == 3

    main = table[0]
    assert main["lender"] == "main_bank"
    assert main["share_pct"] == 60.0
    assert main["grace_period_months"] == 16
    assert main["haircut_pct"] == 18.0
    assert main["new_money_jpy"] == 36_000_000
    assert main["allocation_type"] == "main_bank_heavy"

    sub = table[1]
    assert sub["lender"] == "sub_bank"
    assert sub["share_pct"] == 40.0
    assert sub["grace_period_months"] == 10
    assert sub["haircut_pct"] == 6.0
    assert sub["new_money_jpy"] == 24_000_000
    assert sub["allocation_type"] == "pro_rata"

    guarantor = table[2]
    assert guarantor["lender"] == "guarantor"
    assert guarantor["share_pct"] == 0.0
    assert guarantor["grace_period_months"] == 0
    assert guarantor["haircut_pct"] == 0.0
    assert guarantor["new_money_jpy"] == 0
    assert guarantor["allocation_type"] == "guarantee_only"

    # New-money must sum to abs(gap).
    assert main["new_money_jpy"] + sub["new_money_jpy"] == 60_000_000


def test_burden_sharing_stake_based_exact_values() -> None:
    """Burden-sharing table (stake-based mode) must match exact yen values.

    main_bank_share = 300M / 500M = 0.6 (exact).
    All expected values are derived from the rule code with no guessing.
    """
    state = _make_state(
        gap=-50_000_000,
        uplifts=[1],  # irrelevant when lender_stakes is populated
        lender_stakes={"main_bank": 300_000_000, "sub_bank": 200_000_000},
    )
    table = compute_burden_sharing_table(state)

    main = table[0]
    assert main["lender"] == "main_bank"
    assert main["share_pct"] == 60.0
    assert main["grace_period_months"] == 16
    assert main["haircut_pct"] == 18.0
    assert main["new_money_jpy"] == 30_000_000
    assert main["allocation_type"] == "main_bank_heavy"

    sub = table[1]
    assert sub["lender"] == "sub_bank"
    assert sub["share_pct"] == 40.0
    assert sub["grace_period_months"] == 10
    assert sub["haircut_pct"] == 6.0
    assert sub["new_money_jpy"] == 20_000_000
    assert sub["allocation_type"] == "pro_rata"

    # New-money must sum to abs(gap).
    assert main["new_money_jpy"] + sub["new_money_jpy"] == 50_000_000


def test_burden_sharing_pro_rata_split() -> None:
    """Equal uplifts → 50/50 split → allocation_type = pro_rata for both.

    main_bank_share = 50M / 100M = 0.5 (exact).
    """
    state = _make_state(gap=-100_000_000, uplifts=[50_000_000, 50_000_000])
    table = compute_burden_sharing_table(state)

    main = table[0]
    assert main["share_pct"] == 50.0
    assert main["grace_period_months"] == 15  # round(12 + 6*0.5) = round(15.0) = 15
    assert main["haircut_pct"] == 15.0  # round(0.5 * 30, 1) = 15.0
    assert main["new_money_jpy"] == 50_000_000
    assert main["allocation_type"] == "pro_rata"  # 0.5 is NOT > 0.5

    sub = table[1]
    assert sub["share_pct"] == 50.0
    assert sub["grace_period_months"] == 12  # max(6, min(round(12*0.5*2), 12)) = max(6,12)=12
    assert sub["haircut_pct"] == 7.5  # round(0.5 * 15, 1) = 7.5
    assert sub["new_money_jpy"] == 50_000_000
    assert sub["allocation_type"] == "pro_rata"

    assert main["new_money_jpy"] + sub["new_money_jpy"] == 100_000_000


def test_burden_sharing_no_gap_zero_new_money() -> None:
    """When working_capital_gap >= 0, new_money_jpy must be 0 for all lenders."""
    state = _make_state(gap=0, uplifts=[60_000_000, 40_000_000])
    table = compute_burden_sharing_table(state)

    for row in table:
        assert row["new_money_jpy"] == 0, (
            f"new_money_jpy must be 0 when gap >= 0, got {row['new_money_jpy']} "
            f"for lender {row['lender']}"
        )


def test_burden_sharing_is_deterministic() -> None:
    """compute_burden_sharing_table must be deterministic (same inputs → same output)."""
    state = _make_state(gap=-60_000_000, uplifts=[60_000_000, 40_000_000])
    table_a = compute_burden_sharing_table(state)
    table_b = compute_burden_sharing_table(state)
    assert table_a == table_b


# ---------------------------------------------------------------------------
# New multi-period demo fixtures (24-month) — CLASS-ONLY golden assertions.
#
# These guard ONLY the FSA classification (and the special_attention sub-flag)
# for the two fixtures added for the demo / Feature-5 foundation. They
# deliberately assert NO exact yen/EWS magnitudes: the classification band is
# the load-bearing invariant, and class-only assertions stay robust to harmless
# numeric drift while still catching any band regression.
#
# They exercise the DETERMINISTIC SPINE directly via the MockDataProvider and
# the pure rule functions (compute_ews_score, estimate_working_capital_gap,
# classify), rather than the full graph, so the assertion does not depend on
# the critic/HITL outcome of the turnaround path. This mirrors how the rule
# code itself composes these functions (see ews_node / macro_node / classifier).
# ---------------------------------------------------------------------------


def _classify_fixture(tdb_code: str) -> tuple[FsaClass, bool]:
    """Run the deterministic spine for a fixture and return (FsaClass, special).

    Loads the fixture through the MockDataProvider and composes the same pure
    rule functions the graph nodes use:
        compute_ews_score(shisanhyo)
        estimate_working_capital_gap(latest sales/COGS, metrics, rate curve, eigyo)
        classify(ews, gap, tdb_score)
    No graph execution, no HITL — just the classification spine.
    """
    provider = MockDataProvider()
    report = provider.credit_report(tdb_code)
    shisanhyo = provider.shisanhyo(report.profile.hojin_bango)
    rate_curve = provider.rate_curve()
    metrics = provider.settlement_metrics()

    ews = compute_ews_score(shisanhyo)
    latest = shisanhyo[-1]
    gap = estimate_working_capital_gap(
        monthly_sales=int(latest.uriage),
        monthly_cogs=int(latest.uriage_genka),
        metrics=metrics,
        rate_curve=rate_curve,
        monthly_operating_profit=latest.eigyo_rieki,
    )
    return classify(
        ews_score=ews,
        working_capital_gap=gap,
        tdb_score=report.tdb_score,
    )


def test_in_danger_fixture_classifies_hatan_kenensaki() -> None:
    """4000001 (osaka_distressed_mfg) must classify as 破綻懸念先 (HATAN_KENENSAKI).

    Sustained 24-month decline with gross-margin compression drives EWS into
    the [EWS_DOUBTFUL, EWS_DANGER) band → In Danger of Bankruptcy.
    """
    fsa, _special = _classify_fixture("4000001")
    assert fsa is FsaClass.HATAN_KENENSAKI


def test_wc_deficit_fixture_classifies_yochuisaki_special_attention() -> None:
    """5000001 (kyoto_wc_deficit_co) must be 要注意先 with special_attention=True.

    Thin-but-positive profit keeps EWS below EWS_SUBSTANDARD, while high COGS
    drives a negative working-capital gap. classify then yields 要注意先
    (YOCHUISAKI) with the 要管理先 sub-flag set (deficit present).
    """
    fsa, special = _classify_fixture("5000001")
    assert fsa is FsaClass.YOCHUISAKI
    assert special is True


def test_new_fixtures_are_deterministic() -> None:
    """Both new fixtures must classify identically across repeated runs."""
    for code in ("4000001", "5000001"):
        assert _classify_fixture(code) == _classify_fixture(code)
