"""Wiring tests: hitl_negotiation_node must invoke capture_hitl_feedback.

This is a DIFFERENT concern from ``tests/test_observability.py`` (which tests
``capture_hitl_feedback`` in isolation and its offline-by-default contract).
Here we assert the *call site*: that the banker's approve / revise / reject
decision actually reaches the LangSmith capture function from inside the live
graph, driven through the real ``interrupt()`` / ``Command(resume=...)`` cycle.

Regression guard for the dead-code bug where ``capture_hitl_feedback`` was
implemented and unit-tested but never invoked, so the HITL -> LangSmith dataset
flywheel was wired to nothing.

The capture function is monkeypatched with a recording spy, so these tests make
zero network calls and stay fully offline (the real function is a no-op offline
anyway; the spy just lets us assert it was reached and with what arguments).
"""

from __future__ import annotations

from typing import Any, cast

import app.backend.agents.turnaround_orchestrator as orchestrator
import pytest
from app.backend.graph import build_graph
from app.backend.state import NegotiationDecision, SaiseiState
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command


def _compiled() -> CompiledStateGraph[SaiseiState]:
    return build_graph().compile(checkpointer=MemorySaver())


class _CaptureSpy:
    """Records every call to the patched ``capture_hitl_feedback``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return False  # mirror the offline no-op return


@pytest.fixture
def capture_spy(monkeypatch: pytest.MonkeyPatch) -> _CaptureSpy:
    """Patch capture_hitl_feedback in the orchestrator module with a spy."""
    spy = _CaptureSpy()
    monkeypatch.setattr(orchestrator, "capture_hitl_feedback", spy)
    return spy


def test_capture_invoked_on_approve(capture_spy: _CaptureSpy) -> None:
    """An approve decision must reach capture_hitl_feedback with decision='approve'."""
    app = _compiled()
    config: RunnableConfig = {"configurable": {"thread_id": "capture-approve"}}
    app.invoke(
        cast(
            "SaiseiState",
            {
                "tdb_code": "1234567",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            },
        ),
        config=config,
    )
    app.invoke(Command(resume={"decision": "approve", "strategy_index": 0}), config=config)

    assert capture_spy.calls, "capture_hitl_feedback was never invoked on approve"
    call = capture_spy.calls[-1]
    # Compare against the enum member, not a bare string literal: the captured
    # value must round-trip back to NegotiationDecision.APPROVE.
    assert call["decision"] == NegotiationDecision.APPROVE.value
    assert NegotiationDecision(call["decision"]) is NegotiationDecision.APPROVE
    assert call["tdb_code"] == "1234567"
    # The approved strategy must be passed through on the approve path.
    assert call["approved_strategy"] is not None
    assert call["revision_note"] is None
    # Advisory context must be carried through for the outcomes corpus.
    assert call["fsa_classification"] is not None
    assert "strategies" in call and isinstance(call["strategies"], list)


def test_capture_invoked_on_revise(capture_spy: _CaptureSpy) -> None:
    """A revise decision must reach capture_hitl_feedback with the revision note."""
    app = _compiled()
    config: RunnableConfig = {"configurable": {"thread_id": "capture-revise"}}
    # Start with default flags so the graph pauses at HITL (needs_human).
    app.invoke(cast("SaiseiState", {"tdb_code": "1234567"}), config=config)
    app.invoke(
        Command(
            resume={
                "decision": "revise",
                "revision_note": "\u5f79\u54e1\u5831\u916c\u524a\u6e1b\u3092\u78ba\u8a8d",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            }
        ),
        config=config,
    )

    assert capture_spy.calls, "capture_hitl_feedback was never invoked on revise"
    revise_calls = [
        c for c in capture_spy.calls if c["decision"] == NegotiationDecision.REVISE.value
    ]
    assert revise_calls, "no capture call recorded the revise decision"
    call = revise_calls[-1]
    assert NegotiationDecision(call["decision"]) is NegotiationDecision.REVISE
    assert call["revision_note"] == "\u5f79\u54e1\u5831\u916c\u524a\u6e1b\u3092\u78ba\u8a8d"
    assert call["approved_strategy"] is None


def test_capture_not_invoked_on_invalid_decision(capture_spy: _CaptureSpy) -> None:
    """An invalid decision must NOT reach capture (it returns an error early)."""
    app = _compiled()
    config: RunnableConfig = {"configurable": {"thread_id": "capture-invalid"}}
    app.invoke(
        cast(
            "SaiseiState",
            {
                "tdb_code": "1234567",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            },
        ),
        config=config,
    )
    app.invoke(Command(resume={"decision": "not-a-decision"}), config=config)

    assert not capture_spy.calls, "capture must not run for an invalid decision"


def test_capture_failure_does_not_break_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture exception must NOT break the graph node (best-effort contract)."""

    def _boom(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("langsmith down")

    monkeypatch.setattr(orchestrator, "capture_hitl_feedback", _boom)

    app = _compiled()
    config: RunnableConfig = {"configurable": {"thread_id": "capture-boom"}}
    app.invoke(
        cast(
            "SaiseiState",
            {
                "tdb_code": "1234567",
                "yakuin_hoshu_cut": True,
                "personal_asset_disposal": True,
            },
        ),
        config=config,
    )
    # The approve must still complete and write the Keikakusho despite the
    # capture raising — observability is best-effort and never fatal.
    app.invoke(Command(resume={"decision": "approve", "strategy_index": 0}), config=config)
    snapshot = app.get_state(config)
    assert not snapshot.next  # graph completed
    draft = snapshot.values["keikakusho_draft"]
    assert draft and "\u7d4c\u55b6\u6539\u5584\u8a08\u753b\u66f8" in draft
