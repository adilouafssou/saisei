"""Verifier: guarantee_release audit event is chained under the run thread_id.

No CI here, so this pins the audit-integrity fix: keieisha_hosho_node must
record its guarantee_release event under the SAME thread_id as the rest of the
borrower's ledger (read from the LangGraph run config), not under the empty
thread "" — otherwise the examiner surface GET /audit/<thread_id> would never
return it and the per-thread hash chain would be incomplete.

The node's audit write is best-effort and uses the configured sink; we inject an
InMemoryAuditSink by patching get_audit_sink (the name record_event resolves),
so the test is fully offline (no DB, no network).
"""

from __future__ import annotations

import app.backend.audit.record as audit_record
import pytest
from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.sink import InMemoryAuditSink
from app.backend.nodes.keieisha_hosho import keieisha_hosho_node
from app.backend.state import SaiseiState
from langchain_core.runnables import RunnableConfig

_THREAD = "hosho-thread-1"
_CONFIG: RunnableConfig = {"configurable": {"thread_id": _THREAD}}


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditSink:
    """Inject an in-memory audit sink for the duration of a test."""
    s = InMemoryAuditSink()
    monkeypatch.setattr(audit_record, "get_audit_sink", lambda *a, **k: s)
    return s


def _state() -> SaiseiState:
    return SaiseiState(tdb_code="1234567", ews_score=55.0, tdb_score=60)


def test_guarantee_release_event_is_under_the_run_thread(sink: InMemoryAuditSink) -> None:
    """The event must land under the config thread_id, not the empty thread."""
    keieisha_hosho_node(_state(), _CONFIG)
    under_thread = sink.read(_THREAD)
    assert len(under_thread) == 1
    assert under_thread[0].event_type is AuditEventType.GUARANTEE_RELEASE
    # Regression: nothing must be orphaned under the empty thread.
    assert sink.read("") == []


def test_guarantee_release_chain_is_intact(sink: InMemoryAuditSink) -> None:
    """The single-event chain under the thread verifies OK."""
    keieisha_hosho_node(_state(), _CONFIG)
    assert sink.verify_chain(_THREAD).ok


def test_node_still_returns_the_assessment(sink: InMemoryAuditSink) -> None:
    """Spine invariance: the audit side-record never changes the returned state."""
    result = keieisha_hosho_node(_state(), _CONFIG)
    assert "hosho_kaijo_score" in result
    assert "hosho_kaijo_eligible" in result
    assert "succession_ready" in result


def test_node_runs_without_config(sink: InMemoryAuditSink) -> None:
    """With no config (thread_id ''), the node still works and never raises.

    This degrades to the empty thread (acceptable when there is genuinely no
    run thread), but must not crash — the audit write is best-effort.
    """
    result = keieisha_hosho_node(_state())
    assert "hosho_kaijo_score" in result
    # With no thread_id, the event chains under "" (no run thread to attribute to).
    assert len(sink.read("")) == 1
