"""Offline tests for per-node latency/cost budgets (Feature 1 observability).

The budget layer is deterministic and offline: it times a node and emits a
structured SLO-breach alert, but it must NEVER change the node's output. These
tests pin both halves -- the pure breach logic and the behaviour-preserving
wrapper.
"""

from __future__ import annotations

from typing import Any

from app.backend.analysis.node_budgets import (
    DEFAULT_NODE_LATENCY_BUDGET_MS,
    NODE_LATENCY_BUDGETS_MS,
    budget_for,
    evaluate_breach,
    instrument_node,
    record_node_metrics,
)


class TestBudgetFor:
    def test_known_node_uses_its_budget(self) -> None:
        assert budget_for("ews") == NODE_LATENCY_BUDGETS_MS["ews"]

    def test_unknown_node_uses_default(self) -> None:
        assert budget_for("not_a_node") == DEFAULT_NODE_LATENCY_BUDGET_MS


class TestEvaluateBreach:
    def test_under_budget_is_not_breached(self) -> None:
        m = evaluate_breach("ews", latency_ms=1.0)
        assert not m.latency_breached
        assert not m.cost_breached
        assert not m.breached

    def test_over_latency_budget_is_breached(self) -> None:
        over = NODE_LATENCY_BUDGETS_MS["ews"] + 1.0
        m = evaluate_breach("ews", latency_ms=over)
        assert m.latency_breached
        assert m.breached

    def test_zero_tokens_is_zero_cost_and_no_cost_breach(self) -> None:
        m = evaluate_breach("strategist", latency_ms=1.0, tokens=0)
        assert m.tokens == 0
        assert m.cost_usd == 0.0
        assert not m.cost_breached

    def test_large_token_count_trips_cost_breach(self) -> None:
        # 10M tokens * 0.01 USD/1k = 100 USD, far over the 0.50 USD budget.
        m = evaluate_breach("plan_writer", latency_ms=1.0, tokens=10_000_000)
        assert m.cost_breached
        assert m.breached

    def test_negative_tokens_are_clamped(self) -> None:
        m = evaluate_breach("ews", latency_ms=1.0, tokens=-5)
        assert m.tokens == 0
        assert m.cost_usd == 0.0

    def test_is_deterministic(self) -> None:
        a = evaluate_breach("macro", latency_ms=42.0, tokens=100)
        b = evaluate_breach("macro", latency_ms=42.0, tokens=100)
        assert a == b


class TestRecordNodeMetrics:
    def test_returns_metrics_without_raising(self) -> None:
        m = record_node_metrics("ews", latency_ms=1.0)
        assert m.node == "ews"
        assert not m.breached

    def test_breach_path_still_returns_metrics(self) -> None:
        over = NODE_LATENCY_BUDGETS_MS["ews"] + 1.0
        m = record_node_metrics("ews", latency_ms=over)
        assert m.latency_breached


class TestInstrumentNode:
    def test_wrapper_returns_node_output_unchanged(self) -> None:
        def node(state: dict[str, Any]) -> dict[str, Any]:
            return {"echo": state["x"], "ok": True}

        wrapped = instrument_node("ews", node)
        out = wrapped({"x": 7})
        assert out == {"echo": 7, "ok": True}

    def test_wrapper_forwards_args_and_kwargs(self) -> None:
        def node(a: int, b: int = 0) -> dict[str, int]:
            return {"sum": a + b}

        wrapped = instrument_node("classifier", node)
        assert wrapped(2, b=3) == {"sum": 5}

    def test_wrapper_propagates_node_exception(self) -> None:
        def node(_: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("boom")

        wrapped = instrument_node("macro", node)
        try:
            wrapped({})
        except ValueError as exc:
            assert str(exc) == "boom"
        else:  # pragma: no cover - the call must raise
            raise AssertionError("expected ValueError to propagate")

    def test_wrapper_is_identity_on_repeated_calls(self) -> None:
        calls: list[int] = []

        def node(n: int) -> dict[str, int]:
            calls.append(n)
            return {"n": n}

        wrapped = instrument_node("strategist", node)
        assert wrapped(1) == {"n": 1}
        assert wrapped(2) == {"n": 2}
        assert calls == [1, 2]
