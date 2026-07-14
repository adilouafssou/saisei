"""Spine-invariance for the audit wiring (Feature 7, spec §10 test 7).

The audit log is a SIDE-RECORD: capturing an event must never change a gate,
route, score, figure, or the deterministic verdict. These tests pin that
contract at the wired call sites — ``classifier_node``, ``keieisha_hosho_node``,
and ``hitl_negotiation_node`` — by asserting each returns the SAME partial-state
dict whether the audit sink is a no-op (offline default) or a real recording
sink, AND that the recording sink actually captured the expected event (so the
test proves the write happened yet changed nothing).

Offline, deterministic; imports only from ``app.*`` + stdlib + pytest.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from app.backend.agents import turnaround_orchestrator as hitl_mod
from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.sink import InMemoryAuditSink, NullAuditSink
from app.backend.nodes import ews_scoring as ews_mod
from app.backend.nodes import keieisha_hosho as hosho_mod
from app.backend.nodes.ews_scoring import classifier_node
from app.backend.nodes.keieisha_hosho import keieisha_hosho_node
from app.backend.state import NegotiationDecision, SaiseiState, Strategy
from app.shared.models.accounting import TrialBalance
from langchain_core.runnables import RunnableConfig

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "t-spine"}}


def _state(**overrides: object) -> SaiseiState:
    base: dict[str, object] = {
        "tdb_code": "1234567",
        "hojin_bango": "1234567890123",
        "tdb_score": 41,
        "working_capital_gap": -5_000_000,
        "ews_score": 62.5,
        "net_worth": 10_000_000,
        "is_insolvent": False,
        "shisanhyo": [
            TrialBalance(
                period=dt.date(2025, 6, 30),
                uriage=100_000_000,
                uriage_genka=78_000_000,
                hanbaihi=18_000_000,
                eigai_shueki=0,
                eigai_hiyo=0,
            )
        ],
    }
    base.update(overrides)
    return SaiseiState(**base)


def _patch_sink(monkeypatch: pytest.MonkeyPatch, module: Any, sink: Any) -> None:
    """Force ``module``'s ``record_event`` to resolve the given sink.

    ``record_event`` selects its sink via ``get_audit_sink(settings)``; patching
    that symbol inside the ``record`` module makes every wired call site use the
    supplied sink without changing the node signatures.
    """
    from app.backend.audit import record as record_mod

    monkeypatch.setattr(record_mod, "get_audit_sink", lambda *_a, **_k: sink)


class TestClassifierSpineInvariance:
    def test_return_is_identical_with_and_without_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_sink(monkeypatch, ews_mod, NullAuditSink())
        baseline = classifier_node(_state(), _CONFIG)

        rec = InMemoryAuditSink()
        _patch_sink(monkeypatch, ews_mod, rec)
        with_sink = classifier_node(_state(), _CONFIG)

        assert with_sink == baseline
        events = rec.read("t-spine")
        assert len(events) == 1
        assert events[0].event_type is AuditEventType.CLASSIFICATION
        assert rec.verify_chain("t-spine").ok


class TestKeieishaHoshoSpineInvariance:
    def test_return_is_identical_with_and_without_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_sink(monkeypatch, hosho_mod, NullAuditSink())
        baseline = keieisha_hosho_node(_state())

        rec = InMemoryAuditSink()
        _patch_sink(monkeypatch, hosho_mod, rec)
        with_sink = keieisha_hosho_node(_state())

        assert with_sink == baseline
        # keieisha_hosho_node has no config, so the event keys on the empty
        # thread_id; assert one guarantee_release event was recorded.
        events = rec.read("")
        assert len(events) == 1
        assert events[0].event_type is AuditEventType.GUARANTEE_RELEASE


class TestHitlSpineInvariance:
    """The HITL node interrupts; drive it via a patched interrupt + a sink."""

    def _run(
        self, monkeypatch: pytest.MonkeyPatch, sink: Any, response: dict[str, Any]
    ) -> dict[str, Any]:
        _patch_sink(monkeypatch, hitl_mod, sink)
        monkeypatch.setattr(hitl_mod, "interrupt", lambda _payload: response)
        state = _state(
            proposed_strategies=[
                Strategy(
                    title="Cost reset",
                    rationale="Restore ordinary profit",
                    expected_keijo_uplift=1_000_000,
                )
            ]
        )
        return hitl_mod.hitl_negotiation_node(state, _CONFIG)

    def test_approve_return_is_identical_with_and_without_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = {"decision": "approve", "strategy_index": 0}
        baseline = self._run(monkeypatch, NullAuditSink(), response)

        rec = InMemoryAuditSink()
        with_sink = self._run(monkeypatch, rec, response)

        assert with_sink == baseline
        events = rec.read("t-spine")
        assert len(events) == 1
        assert events[0].event_type is AuditEventType.HUMAN_DECISION
        assert events[0].payload["decision"] == NegotiationDecision.APPROVE.value

    def test_reject_return_is_identical_with_and_without_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = {"decision": "reject", "revision_note": "insufficient"}
        baseline = self._run(monkeypatch, NullAuditSink(), response)

        rec = InMemoryAuditSink()
        with_sink = self._run(monkeypatch, rec, response)

        assert with_sink == baseline
        events = rec.read("t-spine")
        assert len(events) == 1
        assert events[0].event_type is AuditEventType.HUMAN_DECISION

    def test_human_decision_actor_resolves_from_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = InMemoryAuditSink()
        self._run(
            monkeypatch,
            rec,
            {"decision": "approve", "strategy_index": 0, "actor": "banker-42"},
        )
        assert rec.read("t-spine")[0].actor == "banker-42"
