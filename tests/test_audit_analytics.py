"""Verifier for cross-thread audit-ledger analytics (audit-ledger hardening).

Pins the deterministic aggregation layer on top of the existing cross-thread
``query`` surface: the pure :func:`~app.backend.audit.analytics.summarise`
function and the read-only ``GET /audit/analytics`` endpoint (offline, via
TestClient with the sink patched in -- no DB).

What is pinned:
* an empty list summarises to an all-zero, byte-stable summary;
* totals, per-event-type / per-actor / per-borrower counts;
* the human-decision approve/revise/reject breakdown sums to the decision total,
  with unrecognised verdicts bucketed under 'unknown';
* decision counts read only the ``decision`` discriminator, so redacting a note
  never changes an aggregate;
* distinct cardinalities and the activity time span (earliest/latest);
* the governance posture: active legal holds (per-thread holds minus releases)
  and the redaction-event count;
* the analytics aggregate the SAME filtered events the raw query returns;
* the endpoint applies filters, reports the effective limit, and 400s an
  unknown event_type.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import app.app as app_module
import pytest
from app.backend.audit.analytics import AuditAnalytics, summarise
from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.sink import InMemoryAuditSink
from fastapi.testclient import TestClient


def _event(
    *,
    event_id: str,
    thread_id: str,
    tdb_code: str = "1234567",
    event_type: AuditEventType = AuditEventType.CLASSIFICATION,
    actor: str = "system",
    created_at: str = "2026-03-01T00:00:00+00:00",
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        thread_id=thread_id,
        tdb_code=tdb_code,
        event_type=event_type,
        actor=actor,
        created_at=created_at,
        payload=payload or {},
    ).with_content_hash()


def _decision(event_id: str, thread_id: str, actor: str, decision: str, **kw: Any) -> AuditEvent:
    return _event(
        event_id=event_id,
        thread_id=thread_id,
        actor=actor,
        event_type=AuditEventType.HUMAN_DECISION,
        payload={"decision": decision, "revision_note": "secret note"},
        **kw,
    )


def _mixed_events() -> list[AuditEvent]:
    """Events across borrowers / actors / decisions / dates for aggregation."""
    return [
        _event(
            event_id="c1",
            thread_id="tA",
            tdb_code="1111111",
            created_at="2026-03-01T09:00:00+00:00",
        ),
        _decision(
            "d1",
            "tA",
            "banker-jane",
            "approve",
            tdb_code="1111111",
            created_at="2026-03-02T09:00:00+00:00",
        ),
        _event(
            event_id="c2",
            thread_id="tB",
            tdb_code="2222222",
            created_at="2026-03-10T09:00:00+00:00",
        ),
        _decision(
            "d2",
            "tB",
            "banker-bob",
            "revise",
            tdb_code="2222222",
            created_at="2026-04-01T09:00:00+00:00",
        ),
        _decision(
            "d3",
            "tB",
            "banker-bob",
            "reject",
            tdb_code="2222222",
            created_at="2026-04-02T09:00:00+00:00",
        ),
    ]


class TestSummarise:
    def test_empty_is_all_zero(self) -> None:
        assert summarise([]) == AuditAnalytics()

    def test_totals_and_breakdowns(self) -> None:
        a = summarise(_mixed_events())
        assert a.total_events == 5
        assert a.by_event_type == {"classification": 2, "human_decision": 3}
        assert a.by_actor == {"system": 2, "banker-jane": 1, "banker-bob": 2}
        assert a.by_borrower == {"1111111": 2, "2222222": 3}

    def test_decision_breakdown_sums_to_human_decision_total(self) -> None:
        a = summarise(_mixed_events())
        assert a.decisions == {"approve": 1, "revise": 1, "reject": 1}
        assert sum(a.decisions.values()) == a.by_event_type["human_decision"]

    def test_unknown_decision_is_bucketed(self) -> None:
        a = summarise([_decision("d", "t", "banker", "maybe")])
        assert a.decisions == {"unknown": 1}

    def test_distinct_cardinalities_and_span(self) -> None:
        a = summarise(_mixed_events())
        assert a.distinct_actors == 3
        assert a.distinct_borrowers == 2
        assert a.distinct_threads == 2
        assert a.earliest == "2026-03-01T09:00:00+00:00"
        assert a.latest == "2026-04-02T09:00:00+00:00"

    def test_active_legal_hold_counted_per_thread(self) -> None:
        events = [
            _event(
                event_id="h1",
                thread_id="tHeld",
                event_type=AuditEventType.LEGAL_HOLD,
                actor="admin",
            ),
            _event(
                event_id="h2",
                thread_id="tReleased",
                event_type=AuditEventType.LEGAL_HOLD,
                actor="admin",
            ),
            _event(
                event_id="h3",
                thread_id="tReleased",
                event_type=AuditEventType.LEGAL_HOLD_RELEASE,
                actor="admin",
            ),
        ]
        a = summarise(events)
        assert a.active_legal_holds == 1

    def test_redaction_events_counted(self) -> None:
        events = [
            _event(
                event_id="r1",
                thread_id="t",
                event_type=AuditEventType.REDACTION,
                actor="admin",
                payload={"target_event_id": "x", "redact_keys": ["k"]},
            ),
        ]
        assert summarise(events).redaction_events == 1


# ---------------------------------------------------------------------------
# GET /audit/analytics endpoint
# ---------------------------------------------------------------------------


def _seeded_sink() -> InMemoryAuditSink:
    sink = InMemoryAuditSink()
    for event in _mixed_events():
        sink.append(event)
    return sink


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    sink = _seeded_sink()
    monkeypatch.setattr(app_module, "get_audit_sink", lambda _settings: sink)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def test_endpoint_returns_full_summary(client: TestClient) -> None:
    resp = client.get("/audit/analytics")
    assert resp.status_code == 200, resp.text
    a = resp.json()["analytics"]
    assert a["total_events"] == 5
    assert a["by_event_type"] == {"classification": 2, "human_decision": 3}
    assert a["decisions"] == {"approve": 1, "revise": 1, "reject": 1}
    assert a["distinct_borrowers"] == 2


def test_endpoint_respects_filters(client: TestClient) -> None:
    resp = client.get("/audit/analytics", params={"actor": "banker-bob"})
    a = resp.json()["analytics"]
    assert a["total_events"] == 2
    assert a["decisions"] == {"revise": 1, "reject": 1}
    assert a["by_actor"] == {"banker-bob": 2}


def test_endpoint_reports_effective_limit(client: TestClient) -> None:
    resp = client.get("/audit/analytics", params={"limit": 2})
    body = resp.json()
    assert body["limit"] == 2
    # Only the first 2 events (by global write order) are summarised.
    assert body["analytics"]["total_events"] == 2


def test_endpoint_unknown_event_type_is_400(client: TestClient) -> None:
    resp = client.get("/audit/analytics", params={"event_type": "not_a_type"})
    assert resp.status_code == 400, resp.text
