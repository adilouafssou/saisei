"""Verifier for cross-thread audit-ledger queries (audit-ledger hardening).

The per-thread ``read`` already had coverage; this pins the new CROSS-THREAD
``query`` path -- the book-level / regulator view -- on the offline
``InMemoryAuditSink`` (whose ``matches``/limit semantics are the shared source of
truth the Postgres sink mirrors), plus the read-only ``GET /audit`` endpoint via
TestClient with the sink patched in (fully offline, no DB).

What is pinned:
* filtering by tdb_code / event_type / actor / created_at range, and combinations;
* an empty query returns everything (newest last, in global write order);
* the limit is clamped to [1, MAX_QUERY_LIMIT] and applied;
* results are in global write order across threads;
* the NullAuditSink query is an offline no-op;
* the endpoint returns matches, applies filters, and 400s an unknown event_type.
"""

from __future__ import annotations

from collections.abc import Iterator

import app.app as app_module
import pytest
from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.sink import (
    MAX_QUERY_LIMIT,
    AuditQuery,
    InMemoryAuditSink,
    NullAuditSink,
)
from fastapi.testclient import TestClient


def _event(
    *,
    event_id: str,
    thread_id: str,
    tdb_code: str = "1234567",
    event_type: AuditEventType = AuditEventType.CLASSIFICATION,
    actor: str = "system",
    created_at: str = "2026-03-01T00:00:00+00:00",
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        thread_id=thread_id,
        tdb_code=tdb_code,
        event_type=event_type,
        created_at=created_at,
        actor=actor,
    ).with_content_hash()


def _seeded_sink() -> InMemoryAuditSink:
    """A sink with events across multiple threads / borrowers / actors / dates."""
    sink = InMemoryAuditSink()
    sink.append(
        _event(
            event_id="e1",
            thread_id="tA",
            tdb_code="1111111",
            event_type=AuditEventType.CLASSIFICATION,
            created_at="2026-03-01T09:00:00+00:00",
        )
    )
    sink.append(
        _event(
            event_id="e2",
            thread_id="tA",
            tdb_code="1111111",
            event_type=AuditEventType.HUMAN_DECISION,
            actor="banker-jane",
            created_at="2026-03-02T09:00:00+00:00",
        )
    )
    sink.append(
        _event(
            event_id="e3",
            thread_id="tB",
            tdb_code="2222222",
            event_type=AuditEventType.CLASSIFICATION,
            created_at="2026-03-10T09:00:00+00:00",
        )
    )
    sink.append(
        _event(
            event_id="e4",
            thread_id="tB",
            tdb_code="2222222",
            event_type=AuditEventType.HUMAN_DECISION,
            actor="banker-bob",
            created_at="2026-04-01T09:00:00+00:00",
        )
    )
    return sink


# ---------------------------------------------------------------------------
# In-memory query semantics
# ---------------------------------------------------------------------------


class TestInMemoryQuery:
    def test_empty_query_returns_all_in_write_order(self) -> None:
        events = _seeded_sink().query(AuditQuery())
        assert [e.event_id for e in events] == ["e1", "e2", "e3", "e4"]

    def test_filter_by_tdb_code(self) -> None:
        events = _seeded_sink().query(AuditQuery(tdb_code="2222222"))
        assert [e.event_id for e in events] == ["e3", "e4"]

    def test_filter_by_event_type(self) -> None:
        events = _seeded_sink().query(AuditQuery(event_type=AuditEventType.HUMAN_DECISION))
        assert [e.event_id for e in events] == ["e2", "e4"]

    def test_filter_by_actor(self) -> None:
        events = _seeded_sink().query(AuditQuery(actor="banker-jane"))
        assert [e.event_id for e in events] == ["e2"]

    def test_filter_by_date_range_inclusive(self) -> None:
        events = _seeded_sink().query(
            AuditQuery(since="2026-03-02T00:00:00+00:00", until="2026-03-31T23:59:59+00:00")
        )
        assert [e.event_id for e in events] == ["e2", "e3"]

    def test_combined_filters(self) -> None:
        events = _seeded_sink().query(
            AuditQuery(
                event_type=AuditEventType.HUMAN_DECISION,
                since="2026-03-15T00:00:00+00:00",
            )
        )
        assert [e.event_id for e in events] == ["e4"]

    def test_no_match_returns_empty(self) -> None:
        assert _seeded_sink().query(AuditQuery(tdb_code="9999999")) == []

    def test_limit_is_applied(self) -> None:
        events = _seeded_sink().query(AuditQuery(limit=2))
        assert [e.event_id for e in events] == ["e1", "e2"]

    def test_limit_is_clamped_to_minimum_one(self) -> None:
        assert AuditQuery(limit=0).effective_limit() == 1
        assert AuditQuery(limit=-5).effective_limit() == 1

    def test_limit_is_clamped_to_maximum(self) -> None:
        assert AuditQuery(limit=10_000).effective_limit() == MAX_QUERY_LIMIT


def test_null_sink_query_is_noop() -> None:
    assert NullAuditSink().query(AuditQuery()) == []


# ---------------------------------------------------------------------------
# GET /audit endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with get_audit_sink patched to a seeded in-memory sink."""
    sink = _seeded_sink()
    monkeypatch.setattr(app_module, "get_audit_sink", lambda _settings: sink)
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


def test_endpoint_returns_all_events(client: TestClient) -> None:
    resp = client.get("/audit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 4
    assert [e["event_id"] for e in body["events"]] == ["e1", "e2", "e3", "e4"]


def test_endpoint_filters_by_actor_and_type(client: TestClient) -> None:
    resp = client.get("/audit", params={"event_type": "human_decision", "actor": "banker-bob"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["event_id"] for e in body["events"]] == ["e4"]


def test_endpoint_filters_by_date_range(client: TestClient) -> None:
    resp = client.get(
        "/audit",
        params={"since": "2026-03-02T00:00:00+00:00", "until": "2026-03-31T23:59:59+00:00"},
    )
    assert [e["event_id"] for e in resp.json()["events"]] == ["e2", "e3"]


def test_endpoint_unknown_event_type_is_400(client: TestClient) -> None:
    resp = client.get("/audit", params={"event_type": "not_a_type"})
    assert resp.status_code == 400, resp.text


def test_endpoint_reports_effective_limit(client: TestClient) -> None:
    resp = client.get("/audit", params={"limit": 1})
    body = resp.json()
    assert body["limit"] == 1
    assert body["count"] == 1
    assert body["events"][0]["event_id"] == "e1"
