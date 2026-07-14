"""MR2 (outcome capture) — who-was-right corpus tests.

Verifies the permanent, append-only reconciliation_outcomes corpus captured at
each HITL resolution:

1. ReconciliationOutcome model: round-trip + banker_verdict validation
   (domain enforcement, case-insensitivity, whitespace trim, None -> '').
2. reconciliation_outcomes_reducer: pure append, no clear sentinel.
3. _reconciliation_outcomes builder: routed-only capture, verdict passthrough,
   decision tagging, offline no-op.
4. Graph-level resume: capture on the reconciliation path; no-op on the normal
   path.

All tests run fully offline (in-memory checkpointer, no LLM / network).
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import app.backend.agents.turnaround_orchestrator as orchestrator
import pytest
from app.backend.agents.turnaround_orchestrator import _reconciliation_outcomes
from app.backend.graph import build_graph
from app.backend.state import (
    BANKER_VERDICTS,
    NegotiationDecision,
    ReconciliationOutcome,
    SaiseiState,
    reconciliation_outcomes_reducer,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _routed_detail(
    title: str = "price",
    *,
    deterministic_band: str = "high",
    llm_band: str = "low",
    band_distance: int = 2,
    routed: bool = True,
) -> dict[str, Any]:
    return {
        "strategy_title": title,
        "deterministic_band": deterministic_band,
        "deterministic_score": 80.0,
        "llm_band": llm_band,
        "llm_score": 10.0,
        "band_distance": band_distance,
        "routed": routed,
    }


def _compiled() -> CompiledStateGraph[SaiseiState]:
    return build_graph().compile(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# Model: round-trip + banker_verdict validation.
# ---------------------------------------------------------------------------


def test_outcome_model_round_trip() -> None:
    """A constructed outcome serialises and reconstructs identically."""
    outcome = ReconciliationOutcome(
        strategy_title="price",
        deterministic_band="high",
        llm_band="low",
        band_distance=2,
        banker_decision="approve",
        banker_verdict="floor",
    )
    dumped = outcome.model_dump()
    assert ReconciliationOutcome(**dumped) == outcome


def test_outcome_verdict_defaults_to_empty() -> None:
    """banker_verdict defaults to '' (not adjudicated) when omitted."""
    outcome = ReconciliationOutcome(
        strategy_title="price",
        deterministic_band="high",
        llm_band="low",
        band_distance=2,
        banker_decision="revise",
    )
    assert outcome.banker_verdict == ""


@pytest.mark.parametrize("verdict", sorted(BANKER_VERDICTS))
def test_outcome_accepts_all_valid_verdicts(verdict: str) -> None:
    """Every documented verdict in BANKER_VERDICTS is accepted."""
    outcome = ReconciliationOutcome(
        strategy_title="x",
        deterministic_band="high",
        llm_band="low",
        band_distance=2,
        banker_decision="approve",
        banker_verdict=verdict,
    )
    assert outcome.banker_verdict == verdict


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FLOOR", "floor"),
        ("  llm  ", "llm"),
        ("Neither", "neither"),
        (None, ""),
    ],
)
def test_outcome_verdict_is_normalised(raw: Any, expected: str) -> None:
    """banker_verdict is trimmed, lower-cased, and None -> '' by the validator."""
    outcome = ReconciliationOutcome(
        strategy_title="x",
        deterministic_band="high",
        llm_band="low",
        band_distance=2,
        banker_decision="approve",
        banker_verdict=raw,
    )
    assert outcome.banker_verdict == expected


def test_outcome_rejects_invalid_verdict() -> None:
    """A verdict outside BANKER_VERDICTS is rejected at construction."""
    with pytest.raises(ValueError, match="banker_verdict must be one of"):
        ReconciliationOutcome(
            strategy_title="x",
            deterministic_band="high",
            llm_band="low",
            band_distance=2,
            banker_decision="approve",
            banker_verdict="maybe",
        )


def test_outcome_is_frozen_and_forbids_extra() -> None:
    """The model honours frozen=True / extra='forbid'."""
    with pytest.raises(ValueError):
        ReconciliationOutcome(
            strategy_title="x",
            deterministic_band="high",
            llm_band="low",
            band_distance=2,
            banker_decision="approve",
            unexpected="nope",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Reducer: pure append (no clear sentinel).
# ---------------------------------------------------------------------------


def test_reducer_appends() -> None:
    """The reducer concatenates current + update."""
    current = [{"a": 1}]
    update = [{"b": 2}]
    assert reconciliation_outcomes_reducer(current, update) == [{"a": 1}, {"b": 2}]


def test_reducer_empty_update_is_noop() -> None:
    """An empty update leaves the corpus unchanged (offline-safe no-op)."""
    current = [{"a": 1}]
    assert reconciliation_outcomes_reducer(current, []) == [{"a": 1}]


def test_reducer_never_clears() -> None:
    """Even an empty-list update never discards prior outcomes (no sentinel)."""
    current = [{"a": 1}, {"b": 2}]
    # Unlike critic_feedbacks, [] is just an empty append, not a reset.
    assert reconciliation_outcomes_reducer(current, []) == current


# ---------------------------------------------------------------------------
# Builder: routed-only capture, verdict passthrough, decision tagging.
# ---------------------------------------------------------------------------


def test_builder_offline_noop() -> None:
    """No reconciliation -> empty corpus contribution."""
    state = SaiseiState(tdb_code="1234567", reconciliation_required=False)
    out = _reconciliation_outcomes(state, banker_decision=NegotiationDecision.APPROVE, response={})
    assert out == []


def test_builder_captures_routed_only() -> None:
    """Only routed=True disagreements are captured; audit-only are skipped."""
    state = SaiseiState(
        tdb_code="1234567",
        reconciliation_required=True,
        reconciliation_details=[
            _routed_detail("price", routed=True),
            _routed_detail("cogs", routed=False),
        ],
    )
    out = _reconciliation_outcomes(
        state,
        banker_decision=NegotiationDecision.APPROVE,
        response={"reconciliation_verdict": "floor"},
    )
    assert len(out) == 1
    assert out[0]["strategy_title"] == "price"
    assert out[0]["banker_verdict"] == "floor"
    assert out[0]["banker_decision"] == "approve"


def test_builder_normalises_verdict_case_insensitively() -> None:
    """A mixed-case verdict in the payload is normalised by the model."""
    state = SaiseiState(
        tdb_code="1234567",
        reconciliation_required=True,
        reconciliation_details=[_routed_detail()],
    )
    out = _reconciliation_outcomes(
        state,
        banker_decision=NegotiationDecision.REVISE,
        response={"reconciliation_verdict": "  LLM "},
    )
    assert out[0]["banker_verdict"] == "llm"
    assert out[0]["banker_decision"] == "revise"


def test_builder_missing_verdict_is_unadjudicated() -> None:
    """A missing verdict captures as '' (not adjudicated)."""
    state = SaiseiState(
        tdb_code="1234567",
        reconciliation_required=True,
        reconciliation_details=[_routed_detail()],
    )
    out = _reconciliation_outcomes(state, banker_decision=NegotiationDecision.REJECT, response={})
    assert out[0]["banker_verdict"] == ""
    assert out[0]["banker_decision"] == "reject"


def test_builder_tags_each_decision() -> None:
    """banker_decision matches the resolution on each path."""
    state = SaiseiState(
        tdb_code="1234567",
        reconciliation_required=True,
        reconciliation_details=[_routed_detail()],
    )
    for decision in NegotiationDecision:
        out = _reconciliation_outcomes(state, banker_decision=decision, response={})
        assert out[0]["banker_decision"] == decision.value


# ---------------------------------------------------------------------------
# Graph-level resume: capture on the reconciliation path; no-op normally.
# ---------------------------------------------------------------------------


def test_graph_captures_outcome_on_reconciliation_path() -> None:
    """Resolving a routed reconciliation appends an outcome to the corpus."""
    cfg: RunnableConfig = {"configurable": {"thread_id": "recon-outcome-01"}}

    def _fake_feasibility(state: SaiseiState, **kw: Any) -> dict[str, Any]:
        return {
            "feasibility_notes": [],
            "reconciliation_required": True,
            "reconciliation_details": [_routed_detail(routed=True)],
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
        app.invoke(
            Command(
                resume={
                    "decision": "approve",
                    "strategy_index": 0,
                    "reconciliation_verdict": "floor",
                }
            ),
            config=cfg,
        )

    snapshot = app.get_state(cfg)
    outcomes = snapshot.values["reconciliation_outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["banker_verdict"] == "floor"
    assert outcomes[0]["banker_decision"] == "approve"
    assert outcomes[0]["strategy_title"] == "price"


def test_graph_no_outcome_on_normal_path() -> None:
    """The normal fan-out path (no reconciliation) records no outcomes."""
    app = _compiled()
    cfg: RunnableConfig = {"configurable": {"thread_id": "recon-outcome-normal"}}
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
    app.invoke(Command(resume={"decision": "approve", "strategy_index": 0}), config=cfg)

    snapshot = app.get_state(cfg)
    assert snapshot.values["reconciliation_required"] is False
    assert snapshot.values["reconciliation_outcomes"] == []


def test_verdict_prompt_present_only_when_reconciliation_required() -> None:
    """The interrupt payload carries reconciliation_verdict_prompt only when routed."""
    required = SaiseiState(tdb_code="1234567", reconciliation_required=True)
    not_required = SaiseiState(tdb_code="1234567", reconciliation_required=False)
    assert orchestrator._interrupt_payload(required)["reconciliation_verdict_prompt"] is not None
    assert orchestrator._interrupt_payload(not_required)["reconciliation_verdict_prompt"] is None
