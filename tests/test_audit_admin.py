"""Verifier for retention / redaction / legal-hold audit admin actions.

Pins the append-only hardening contract, fully offline on the InMemory sink:
* a redaction is recorded as its OWN event (never an edit) and masks the named
  payload keys AT VIEW TIME, while the raw row -- and thus the hash chain --
  stays intact;
* legal-hold place/release markers drive is_on_legal_hold (hold wins until
  released);
* plan_retention sorts threads into purgeable / retained_recent /
  retained_on_hold, with legal hold always excluding a thread from purge;
* the authenticated admin endpoints append the actions and the read endpoint
  shows the masked view while still reporting an intact chain.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import app.app as app_module
import app.backend.audit.record as record_module
import pytest
from app.backend.audit.admin import (
    REDACTED_PLACEHOLDER,
    apply_redactions,
    is_on_legal_hold,
    place_legal_hold,
    plan_retention,
    record_redaction,
    release_legal_hold,
)
from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.record import record_event
from app.backend.audit.sink import InMemoryAuditSink, verify_chain
from fastapi.testclient import TestClient


class _State:
    """Minimal state-like object for record_event in tests."""

    tdb_code = "1234567"
    hojin_bango = "1234567890123"
    shisanhyo: list[Any] = []
    tdb_score = 55
    working_capital_gap = None
    net_worth = None
    is_insolvent = None


def _seed_classification(sink: InMemoryAuditSink, thread_id: str = "t1") -> str:
    """Append one classification event and return its event_id."""
    record_event(
        AuditEventType.CLASSIFICATION,
        state=_State(),
        payload={"fsa_classification": "\u8981\u6ce8\u610f\u5148", "ews_score": 62.5},
        thread_id=thread_id,
        sink=sink,
    )
    return sink.read(thread_id)[-1].event_id


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_redaction_is_appended_not_edited(self) -> None:
        sink = InMemoryAuditSink()
        target = _seed_classification(sink)
        record_redaction("t1", target, ["ews_score"], reason="PII", actor="admin-1", sink=sink)
        events = sink.read("t1")
        # Original event still present and UNCHANGED; a new REDACTION appended.
        assert len(events) == 2
        assert events[0].event_id == target
        assert events[0].payload["ews_score"] == 62.5  # raw row untouched
        assert events[1].event_type is AuditEventType.REDACTION
        assert events[1].actor == "admin-1"

    def test_view_time_masking_masks_named_keys(self) -> None:
        sink = InMemoryAuditSink()
        target = _seed_classification(sink)
        record_redaction("t1", target, ["ews_score"], reason="PII", actor="admin-1", sink=sink)
        displayed = apply_redactions(sink.read("t1"))
        masked = next(e for e in displayed if e.event_id == target)
        assert masked.payload["ews_score"] == REDACTED_PLACEHOLDER
        # A non-redacted key is left alone.
        assert masked.payload["fsa_classification"] == "\u8981\u6ce8\u610f\u5148"

    def test_raw_chain_still_verifies_after_redaction(self) -> None:
        sink = InMemoryAuditSink()
        target = _seed_classification(sink)
        record_redaction("t1", target, ["ews_score"], reason="PII", actor="admin-1", sink=sink)
        # Verification runs on RAW rows, which are untouched -> chain intact.
        assert verify_chain(sink.read("t1")).ok is True

    def test_apply_redactions_does_not_mutate_input(self) -> None:
        sink = InMemoryAuditSink()
        target = _seed_classification(sink)
        record_redaction("t1", target, ["ews_score"], reason="PII", actor="admin-1", sink=sink)
        raw = sink.read("t1")
        apply_redactions(raw)
        assert raw[0].payload["ews_score"] == 62.5  # input untouched

    def test_no_redactions_passes_through(self) -> None:
        sink = InMemoryAuditSink()
        _seed_classification(sink)
        events = sink.read("t1")
        assert [e.event_id for e in apply_redactions(events)] == [e.event_id for e in events]


# ---------------------------------------------------------------------------
# Legal hold
# ---------------------------------------------------------------------------


class TestLegalHold:
    def test_place_sets_hold(self) -> None:
        sink = InMemoryAuditSink()
        _seed_classification(sink)
        place_legal_hold("t1", reason="litigation", actor="admin-1", sink=sink)
        assert is_on_legal_hold(sink.read("t1")) is True

    def test_release_clears_hold(self) -> None:
        sink = InMemoryAuditSink()
        _seed_classification(sink)
        place_legal_hold("t1", reason="litigation", actor="admin-1", sink=sink)
        release_legal_hold("t1", reason="resolved", actor="admin-1", sink=sink)
        assert is_on_legal_hold(sink.read("t1")) is False

    def test_second_hold_survives_one_release(self) -> None:
        sink = InMemoryAuditSink()
        _seed_classification(sink)
        place_legal_hold("t1", reason="a", actor="admin-1", sink=sink)
        place_legal_hold("t1", reason="b", actor="admin-2", sink=sink)
        release_legal_hold("t1", reason="a-resolved", actor="admin-1", sink=sink)
        assert is_on_legal_hold(sink.read("t1")) is True

    def test_no_hold_by_default(self) -> None:
        sink = InMemoryAuditSink()
        _seed_classification(sink)
        assert is_on_legal_hold(sink.read("t1")) is False


# ---------------------------------------------------------------------------
# Retention planning
# ---------------------------------------------------------------------------


def _evt(thread_id: str, created_at: str, *, hold: bool = False) -> Any:
    from app.backend.audit.audit_log import AuditEvent

    etype = AuditEventType.LEGAL_HOLD if hold else AuditEventType.CLASSIFICATION
    return AuditEvent(
        event_id=f"{thread_id}-{created_at}-{etype.value}",
        thread_id=thread_id,
        tdb_code="1234567",
        event_type=etype,
        created_at=created_at,
    ).with_content_hash()


class TestRetentionPlan:
    def test_old_thread_is_purgeable(self) -> None:
        threads = {"old": [_evt("old", "2020-01-01T00:00:00+00:00")]}
        plan = plan_retention(threads, cutoff="2024-01-01T00:00:00+00:00")
        assert plan.purgeable == ["old"]

    def test_recent_thread_is_retained(self) -> None:
        threads = {"new": [_evt("new", "2026-05-01T00:00:00+00:00")]}
        plan = plan_retention(threads, cutoff="2024-01-01T00:00:00+00:00")
        assert plan.retained_recent == ["new"]
        assert plan.purgeable == []

    def test_thread_with_any_recent_event_is_retained(self) -> None:
        threads = {
            "mixed": [
                _evt("mixed", "2020-01-01T00:00:00+00:00"),
                _evt("mixed", "2026-05-01T00:00:00+00:00"),
            ]
        }
        plan = plan_retention(threads, cutoff="2024-01-01T00:00:00+00:00")
        assert plan.retained_recent == ["mixed"]

    def test_legal_hold_always_excludes_from_purge(self) -> None:
        threads = {
            "held": [
                _evt("held", "2020-01-01T00:00:00+00:00"),
                _evt("held", "2020-01-02T00:00:00+00:00", hold=True),
            ]
        }
        plan = plan_retention(threads, cutoff="2024-01-01T00:00:00+00:00")
        assert plan.retained_on_hold == ["held"]
        assert plan.purgeable == []


# ---------------------------------------------------------------------------
# Admin API endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with ONE shared in-memory sink wired into read + write paths."""
    sink = InMemoryAuditSink()
    # The read endpoint resolves the sink via app.get_audit_sink; the admin
    # actions write via record_event -> record.get_audit_sink. Point both at the
    # same instance so an appended action is visible on the next read.
    monkeypatch.setattr(app_module, "get_audit_sink", lambda _settings: sink)
    monkeypatch.setattr(record_module, "get_audit_sink", lambda _settings: sink)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def _seed_via_record(sink_thread: str = "t1") -> None:
    pass


def test_endpoint_redaction_masks_subsequent_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a classification through the same record path the app uses.
    record_event(
        AuditEventType.CLASSIFICATION,
        state=_State(),
        payload={"fsa_classification": "\u8981\u6ce8\u610f\u5148", "ews_score": 62.5},
        thread_id="t1",
        sink=record_module.get_audit_sink(None),
    )
    target = record_module.get_audit_sink(None).read("t1")[-1].event_id

    resp = client.post(
        "/audit/t1/redactions",
        json={"target_event_id": target, "redact_keys": ["ews_score"], "reason": "PII"},
    )
    assert resp.status_code == 200, resp.text

    read = client.get("/audit/t1").json()
    classification = next(e for e in read["events"] if e["event_id"] == target)
    assert classification["payload"]["ews_score"] == REDACTED_PLACEHOLDER
    # Chain still intact (verification runs on raw rows).
    assert read["chain"]["ok"] is True


def test_endpoint_legal_hold_roundtrip(client: TestClient) -> None:
    record_event(
        AuditEventType.CLASSIFICATION,
        state=_State(),
        payload={"fsa_classification": "\u8981\u6ce8\u610f\u5148"},
        thread_id="t2",
        sink=record_module.get_audit_sink(None),
    )
    assert client.post("/audit/t2/legal-hold", json={"reason": "litigation"}).status_code == 200
    assert is_on_legal_hold(record_module.get_audit_sink(None).read("t2")) is True
    assert (
        client.post("/audit/t2/legal-hold/release", json={"reason": "resolved"}).status_code == 200
    )
    assert is_on_legal_hold(record_module.get_audit_sink(None).read("t2")) is False
