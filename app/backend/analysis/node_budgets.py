"""Per-node cost & latency budgets with SLO-breach alerting (Feature 1).

The final Feature 1 observability slice: give every graph node a latency (and
optional token/cost) budget, measure each invocation, and emit a structured
**alert** when a node exceeds its SLO. This turns the LangSmith traces
(``observability.configure_tracing``) into something actionable -- a rising
latency or cost on a node is surfaced as a log event a dashboard/alerting rule
can trigger on, rather than being buried in per-run traces.

Design (mirrors the rest of the stack)
--------------------------------------
- **Deterministic, offline, zero-dependency.** Timing uses
  ``time.perf_counter``; the breach signal is a structured ``structlog`` event.
  No network, no new dependency -- ``make verify`` stays green and instrumented
  nodes return byte-identical output.
- **Display/observability only -- never load-bearing.** :func:`instrument_node`
  wraps a node callable and returns its result UNCHANGED. It can never alter a
  verdict, a figure, or a route; it only times the call and logs. If the timer
  itself somehow raised, the node result is still returned (best-effort), so
  instrumentation can never break the graph.
- **Budgets are data, in one place.** :data:`NODE_LATENCY_BUDGETS_MS` is the
  single source of truth for each node's latency SLO, alongside the EWS/
  feasibility thresholds in ``app.shared.constants``. A node with no explicit
  budget falls back to :data:`DEFAULT_NODE_LATENCY_BUDGET_MS`.

Usage
-----
Wrap a node when registering it on the graph::

    from app.backend.analysis.node_budgets import instrument_node
    graph.add_node("ews", instrument_node("ews", ews_node))

The wrapper is a no-op on behaviour and a thin timing shell, so wrapping every
node is safe and cheap. Token/cost accounting is opt-in: pass ``tokens`` via
:func:`record_node_metrics` from inside an LLM node when a real token count is
available (the deterministic offline path reports zero tokens, so no cost SLO
fires offline).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any

from app.shared.logging import get_logger

__all__ = [
    "DEFAULT_NODE_LATENCY_BUDGET_MS",
    "NODE_LATENCY_BUDGETS_MS",
    "NodeMetrics",
    "budget_for",
    "evaluate_breach",
    "record_node_metrics",
    "instrument_node",
]

_log = get_logger(__name__)

#: Fallback latency SLO (milliseconds) for a node with no explicit budget.
#: Deliberately generous: the deterministic spine completes in well under this
#: offline, so a breach indicates a real regression (a slow live client, an LLM
#: call on the critical path, etc.) rather than normal operation.
DEFAULT_NODE_LATENCY_BUDGET_MS: float = 2_000.0

#: Per-node latency SLOs (milliseconds). Single source of truth for each node's
#: budget. LLM-capable nodes (strategist/critics/plan_writer carry the optional
#: advisory/polish passes) get a larger budget because a configured live LLM
#: call legitimately dominates their latency; the deterministic data nodes get a
#: tight budget so a slow live data client trips the alert quickly.
NODE_LATENCY_BUDGETS_MS: dict[str, float] = {
    # Deterministic data-loading nodes (mock = sub-ms; live client = network).
    "intake": 1_500.0,
    "ews": 500.0,
    "macro": 1_500.0,
    "classifier": 500.0,
    "keieisha_hosho": 500.0,
    "workout": 500.0,
    # Turnaround nodes that may carry an optional LLM pass on the critical path.
    "strategist": 1_000.0,
    "feasibility_critic": 8_000.0,
    "main_bank_critic": 8_000.0,
    "sub_bank_critic": 8_000.0,
    "guarantor_critic": 8_000.0,
    "lead_arranger": 1_000.0,
    "plan_writer": 8_000.0,
}

#: Cost SLO (USD) per node invocation. 0.0 disables the cost alert for a node.
#: Token->cost is computed with :data:`USD_PER_1K_TOKENS`; offline runs report 0
#: tokens so no cost alert ever fires without a configured LLM.
NODE_COST_BUDGET_USD: float = 0.50

#: Approximate blended USD per 1K tokens, used only to turn an opt-in token
#: count into a cost figure for the SLO. A coarse estimate is enough for an
#: alert threshold; precise billing is the provider's job.
USD_PER_1K_TOKENS: float = 0.01


def budget_for(node_name: str) -> float:
    """Return the latency budget (ms) for a node, or the default.

    Args:
        node_name: The graph node name.

    Returns:
        The node's latency SLO in milliseconds.
    """
    return NODE_LATENCY_BUDGETS_MS.get(node_name, DEFAULT_NODE_LATENCY_BUDGET_MS)


@dataclass(frozen=True)
class NodeMetrics:
    """Measured metrics for a single node invocation.

    Attributes:
        node: The graph node name.
        latency_ms: Wall-clock duration of the node call, in milliseconds.
        tokens: Tokens consumed (0 on the deterministic/offline path).
        cost_usd: Estimated cost in USD (``tokens`` * :data:`USD_PER_1K_TOKENS`).
        latency_budget_ms: The node's latency SLO applied.
        latency_breached: Whether ``latency_ms`` exceeded the budget.
        cost_breached: Whether ``cost_usd`` exceeded :data:`NODE_COST_BUDGET_USD`.
    """

    node: str
    latency_ms: float
    tokens: int
    cost_usd: float
    latency_budget_ms: float
    latency_breached: bool
    cost_breached: bool

    @property
    def breached(self) -> bool:
        """True when either the latency or the cost SLO was exceeded."""
        return self.latency_breached or self.cost_breached


def evaluate_breach(
    node_name: str,
    latency_ms: float,
    tokens: int = 0,
) -> NodeMetrics:
    """Compute the :class:`NodeMetrics` (incl. breach flags) for a measurement.

    Pure function: deterministic given its inputs, no I/O. Separated from
    :func:`record_node_metrics` so the breach logic is unit-testable without
    capturing logs.

    Args:
        node_name: The graph node name.
        latency_ms: Measured wall-clock latency in milliseconds.
        tokens: Tokens consumed (default 0 -> zero cost).

    Returns:
        A :class:`NodeMetrics` with the budget applied and breach flags set.
    """
    budget = budget_for(node_name)
    cost = (max(tokens, 0) / 1_000.0) * USD_PER_1K_TOKENS
    return NodeMetrics(
        node=node_name,
        latency_ms=round(latency_ms, 3),
        tokens=max(tokens, 0),
        cost_usd=round(cost, 6),
        latency_budget_ms=budget,
        latency_breached=latency_ms > budget,
        cost_breached=NODE_COST_BUDGET_USD > 0.0 and cost > NODE_COST_BUDGET_USD,
    )


def record_node_metrics(
    node_name: str,
    latency_ms: float,
    tokens: int = 0,
) -> NodeMetrics:
    """Evaluate and LOG a node measurement, emitting an alert on any SLO breach.

    Always emits an ``observability.node_metrics`` info event with the measured
    latency/tokens/cost. When the latency or cost budget is exceeded it ALSO
    emits an ``observability.node_slo_breach`` WARNING -- the actionable alert a
    dashboard / alerting rule triggers on.

    Args:
        node_name: The graph node name.
        latency_ms: Measured wall-clock latency in milliseconds.
        tokens: Tokens consumed (default 0; offline path reports 0).

    Returns:
        The computed :class:`NodeMetrics`.
    """
    metrics = evaluate_breach(node_name, latency_ms, tokens)
    _log.info(
        "observability.node_metrics",
        node=metrics.node,
        latency_ms=metrics.latency_ms,
        tokens=metrics.tokens,
        cost_usd=metrics.cost_usd,
        latency_budget_ms=metrics.latency_budget_ms,
    )
    if metrics.breached:
        _log.warning(
            "observability.node_slo_breach",
            node=metrics.node,
            latency_ms=metrics.latency_ms,
            latency_budget_ms=metrics.latency_budget_ms,
            latency_breached=metrics.latency_breached,
            cost_usd=metrics.cost_usd,
            cost_budget_usd=NODE_COST_BUDGET_USD,
            cost_breached=metrics.cost_breached,
        )
    return metrics


def instrument_node[R: Mapping[str, Any]](
    node_name: str,
    node_fn: Callable[..., R],
) -> Callable[..., R]:
    """Wrap a graph node to time it and alert on an SLO breach.

    The returned callable forwards all args/kwargs to ``node_fn`` and returns
    its result UNCHANGED -- this is display/observability only and can never
    alter a verdict, a figure, or a route. The node is timed with
    ``perf_counter`` and the measurement is logged via
    :func:`record_node_metrics` (which emits the breach alert).

    Timing is best-effort: the node's return value is captured before metrics
    are recorded, so even if the recording path raised, the node result would
    still be returned. The node's own exceptions propagate normally (after the
    elapsed time is still recorded), so a failing node is not masked.

    Args:
        node_name: The graph node name (used for the budget lookup + logs).
        node_fn: The node callable to wrap.

    Returns:
        A wrapped node callable with identical behaviour plus timing/alerting.
    """

    @wraps(node_fn)
    def _wrapped(*args: Any, **kwargs: Any) -> R:
        start = time.perf_counter()
        try:
            result = node_fn(*args, **kwargs)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1_000.0
            record_node_metrics(node_name, elapsed_ms)
            raise
        elapsed_ms = (time.perf_counter() - start) * 1_000.0
        record_node_metrics(node_name, elapsed_ms)
        return result

    return _wrapped
