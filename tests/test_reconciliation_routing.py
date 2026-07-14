"""MR #2 — deterministic LLM-vs-floor reconciliation routing tests.

Verifies:
1. The reconciliation predicate is a PURE DETERMINISTIC FUNCTION of
   (deterministic_band, llm_band, RECONCILIATION_BAND_DISTANCE).
2. When reconciliation_required=True, the compiled graph routes to
   hitl_negotiation BEFORE the critic fan-out.
3. When reconciliation_required=False (default, offline), the normal fan-out
   path is taken (graph reaches HITL via the normal lead_arranger path).
4. The HITL interrupt payload surfaces reconciliation_details.
5. All tests run fully offline (no LLM, no network).

The LLM signal is injected via a test-only monkey-patch of
``_call_llm_feasibility_signal`` so the reconciliation predicate can be
exercised without a real LLM.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast
from unittest.mock import patch

from app.backend.graph import build_graph, route_after_feasibility
from app.backend.nodes.critics.feasibility import (
    band_ordinal,
    feasibility_critic_node,
)
from app.backend.state import SaiseiState, Strategy
from app.backend.tools.boj_macro import RatePoint, SettlementMetrics
from app.shared.constants import RECONCILIATION_BAND_DISTANCE
from app.shared.models.accounting import TrialBalance
from app.shared.settings import Settings
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="test", expected_keijo_uplift=uplift)


def _tb(uriage: int) -> TrialBalance:
    return TrialBalance(
        period=dt.date(2024, 1, 31),
        uriage=uriage,
        uriage_genka=uriage // 2,
        hanbaihi=uriage // 5,
        eigai_shueki=0,
        eigai_hiyo=0,
    )


def _rate_curve(bps: int = 60) -> list[RatePoint]:
    return [RatePoint(as_of=dt.date(2026, 3, 31), policy_rate_bps=bps)]


def _metrics() -> SettlementMetrics:
    return SettlementMetrics(
        t_plus_1_liquidity_ratio=0.82,
        t_plus_2_liquidity_ratio=0.74,
        receivable_days=95,
        payable_days=45,
    )


#: Offline settings (no LLM).
_OFFLINE = Settings(llm_api_key="", llm_model="")

#: Fake "LLM configured" settings (key/model set, but no real endpoint).
_FAKE_LLM = Settings(
    llm_api_key="test-key",
    llm_model="test-model",
    llm_base_url="http://localhost:9999",
    llm_timeout_seconds=0.1,
)


def _compiled() -> CompiledStateGraph[SaiseiState]:
    """Return a compiled graph with an in-memory checkpointer (no Postgres)."""
    return build_graph().compile(checkpointer=MemorySaver())


def _cfg(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# Unit tests: reconciliation predicate logic.
# ---------------------------------------------------------------------------


def test_reconciliation_band_distance_constant_is_positive() -> None:
    """RECONCILIATION_BAND_DISTANCE must be a positive integer."""
    assert isinstance(RECONCILIATION_BAND_DISTANCE, int)
    assert RECONCILIATION_BAND_DISTANCE > 0


def test_band_ordinal_distance_full_scale() -> None:
    """Full-scale disagreement (high vs low) has distance 2."""
    assert abs(band_ordinal("high") - band_ordinal("low")) == 2


def test_band_ordinal_distance_adjacent() -> None:
    """Adjacent bands (high vs medium, medium vs low) have distance 1."""
    assert abs(band_ordinal("high") - band_ordinal("medium")) == 1
    assert abs(band_ordinal("medium") - band_ordinal("low")) == 1


def test_reconciliation_not_triggered_offline() -> None:
    """With no LLM, reconciliation_required is always False."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=[_strategy("price", 5_000_000)],
        shisanhyo=[_tb(100_000_000)],
    )
    out = feasibility_critic_node(state, settings=_OFFLINE)
    assert out["reconciliation_required"] is False
    assert out["reconciliation_details"] == []


def test_reconciliation_triggered_when_llm_disagrees_full_scale() -> None:
    """When LLM returns a score in the opposite band, reconciliation fires.

    The deterministic floor for a tiny uplift (5M / 1.2B annual) with no stress
    signals will be 'high'. We inject an LLM score of 0 (-> 'low'), giving a
    band distance of 2 >= RECONCILIATION_BAND_DISTANCE (2). This must trigger
    reconciliation_required=True.
    """
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=[_strategy("price", 5_000_000)],
        shisanhyo=[_tb(100_000_000)],
        working_capital_gap=0,
        boj_rate_curve=_rate_curve(bps=0),
        settlement_metrics=_metrics(),
    )

    # Inject LLM signal: score=0 -> band='low' (opposite of deterministic 'high').
    with patch(
        "app.backend.nodes.critics.feasibility._call_llm_feasibility_signals",
        return_value=[0.0],
    ):
        out = feasibility_critic_node(state, settings=_FAKE_LLM)

    assert out["reconciliation_required"] is True
    assert len(out["reconciliation_details"]) == 1
    detail = out["reconciliation_details"][0]
    assert detail["strategy_title"] == "price"
    assert detail["deterministic_band"] == "high"
    assert detail["llm_band"] == "low"
    assert detail["band_distance"] == 2


def test_reconciliation_not_triggered_when_llm_agrees() -> None:
    """When LLM returns a score in the same band, reconciliation does not fire.

    Deterministic floor for tiny uplift -> 'high'. LLM score=90 -> 'high'.
    Band distance = 0 < RECONCILIATION_BAND_DISTANCE. No reconciliation.
    """
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=[_strategy("price", 5_000_000)],
        shisanhyo=[_tb(100_000_000)],
        working_capital_gap=0,
        boj_rate_curve=_rate_curve(bps=0),
        settlement_metrics=_metrics(),
    )

    with patch(
        "app.backend.nodes.critics.feasibility._call_llm_feasibility_signals",
        return_value=[90.0],
    ):
        out = feasibility_critic_node(state, settings=_FAKE_LLM)

    assert out["reconciliation_required"] is False
    assert out["reconciliation_details"] == []


def test_reconciliation_not_triggered_when_llm_adjacent() -> None:
    """When LLM is one band away (distance=1 < threshold=2), no reconciliation.

    Deterministic floor for moderate uplift -> 'medium'. LLM score=80 -> 'high'.
    Band distance = 1 < RECONCILIATION_BAND_DISTANCE (2). No reconciliation.
    """
    # Use a moderate uplift that lands in 'medium' band.
    # annual_sales = 100M * 12 = 1.2B; uplift = 120M -> ratio = 10% -> medium.
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=[_strategy("price", 120_000_000)],
        shisanhyo=[_tb(100_000_000)],
        working_capital_gap=0,
        boj_rate_curve=_rate_curve(bps=0),
        settlement_metrics=_metrics(),
    )

    with patch(
        "app.backend.nodes.critics.feasibility._call_llm_feasibility_signals",
        return_value=[80.0],  # -> 'high', distance from 'medium' = 1
    ):
        out = feasibility_critic_node(state, settings=_FAKE_LLM)

    # Distance = 1 < RECONCILIATION_BAND_DISTANCE (2) -> no reconciliation.
    assert out["reconciliation_required"] is False


def test_reconciliation_not_triggered_when_llm_call_fails() -> None:
    """When the LLM signal call fails (returns None), reconciliation stays False."""
    state = SaiseiState(
        tdb_code="1234567",
        proposed_strategies=[_strategy("price", 5_000_000)],
        shisanhyo=[_tb(100_000_000)],
    )

    with patch(
        "app.backend.nodes.critics.feasibility._call_llm_feasibility_signals",
        return_value=None,
    ):
        out = feasibility_critic_node(state, settings=_FAKE_LLM)

    assert out["reconciliation_required"] is False
    assert out["reconciliation_details"] == []


# ---------------------------------------------------------------------------
# route_after_feasibility: pure deterministic predicate.
# ---------------------------------------------------------------------------


def test_route_after_feasibility_false_returns_fan_out() -> None:
    """reconciliation_required=False -> fan-out list (all three critics)."""
    state = SaiseiState(tdb_code="1234567", reconciliation_required=False)
    result = route_after_feasibility(state)
    assert isinstance(result, list)
    assert set(result) == {"main_bank_critic", "sub_bank_critic", "guarantor_critic"}


def test_route_after_feasibility_true_returns_hitl() -> None:
    """reconciliation_required=True -> 'hitl_negotiation'."""
    state = SaiseiState(tdb_code="1234567", reconciliation_required=True)
    result = route_after_feasibility(state)
    assert result == "hitl_negotiation"


# ---------------------------------------------------------------------------
# Graph-level routing tests (compiled graph, in-memory checkpointer).
# ---------------------------------------------------------------------------


def test_graph_routes_to_hitl_when_reconciliation_required() -> None:
    """When reconciliation_required=True, the graph pauses at hitl_negotiation.

    We patch feasibility_critic_node in the graph module (where it is imported
    and bound to the node) BEFORE building the graph, so the compiled graph
    uses the patched version. The patch injects reconciliation_required=True
    so the conditional edge fires and routes to hitl_negotiation.
    """
    cfg = _cfg("recon-hitl-01")

    def _fake_feasibility(state: SaiseiState, **kw: Any) -> dict[str, Any]:
        return {
            "feasibility_notes": [],
            "reconciliation_required": True,
            "reconciliation_details": [
                {
                    "strategy_title": "test",
                    "deterministic_band": "high",
                    "deterministic_score": 80.0,
                    "llm_band": "low",
                    "llm_score": 10.0,
                    "band_distance": 2,
                }
            ],
        }

    # Patch in the graph module where the function is imported and bound.
    with patch("app.backend.graph.feasibility_critic_node", _fake_feasibility):
        app = _compiled()
        app.invoke(
            cast(
                "SaiseiState",
                {
                    "tdb_code": "3000001",
                    "yakuin_hoshu_cut": True,
                    "personal_asset_disposal": True,
                },
            ),
            config=cfg,
        )

    snapshot = app.get_state(cfg)
    # Graph must pause at hitl_negotiation.
    assert snapshot.next, "Graph must pause at HITL when reconciliation_required=True"
    assert "hitl_negotiation" in snapshot.next
    # reconciliation_required must be True in state.
    assert snapshot.values["reconciliation_required"] is True


def test_graph_takes_normal_fan_out_when_no_reconciliation() -> None:
    """When reconciliation_required=False (offline default), normal fan-out path.

    The graph must reach hitl_negotiation via the normal lead_arranger path
    (not the reconciliation shortcut), meaning the three critics ran.
    """
    app = _compiled()
    cfg = _cfg("recon-normal-01")

    app.invoke(
        cast(
            "SaiseiState",
            {
                "tdb_code": "3000001",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            },
        ),
        config=cfg,
    )

    snapshot = app.get_state(cfg)
    # Graph must pause at hitl_negotiation (normal path).
    assert snapshot.next, "Graph must pause at HITL via normal path"
    assert "hitl_negotiation" in snapshot.next
    # reconciliation_required must be False (no LLM configured).
    assert snapshot.values["reconciliation_required"] is False
    # Critic feedbacks must be populated (fan-out ran).
    assert len(snapshot.values["critic_feedbacks"]) > 0


def test_hitl_payload_surfaces_reconciliation_details() -> None:
    """The HITL interrupt payload includes reconciliation_required and details.

    When reconciliation_required=True, the state at the HITL pause must contain
    reconciliation_required=True and non-empty reconciliation_details so the
    banker sees the disagreement with full context (the HITL node reads these
    from state to build the interrupt payload).
    """
    cfg = _cfg("recon-payload-01")

    recon_details = [
        {
            "strategy_title": "test",
            "deterministic_band": "high",
            "deterministic_score": 80.0,
            "llm_band": "low",
            "llm_score": 10.0,
            "band_distance": 2,
        }
    ]

    def _fake_feasibility(state: SaiseiState, **kw: Any) -> dict[str, Any]:
        return {
            "feasibility_notes": [],
            "reconciliation_required": True,
            "reconciliation_details": recon_details,
        }

    with patch("app.backend.graph.feasibility_critic_node", _fake_feasibility):
        app = _compiled()
        app.invoke(
            cast(
                "SaiseiState",
                {
                    "tdb_code": "3000001",
                    "yakuin_hoshu_cut": True,
                    "personal_asset_disposal": True,
                },
            ),
            config=cfg,
        )

    snapshot = app.get_state(cfg)
    assert snapshot.next
    assert "hitl_negotiation" in snapshot.next

    # Verify reconciliation fields are in state (the HITL node reads these
    # to build the interrupt payload surfaced to the banker).
    values = snapshot.values
    assert values["reconciliation_required"] is True
    assert values["reconciliation_details"] == recon_details
