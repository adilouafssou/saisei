"""Verifier for the companion audit trail (COMPANION_QUERY events).

The advisory companion is read-only and never decides, but a case-shaping
conversation must leave a compliance trail. These tests pin that the
``companion_query`` audit event:

- rides the SAME append-only, hash-chained, data-version-pinned ledger as every
  other event, via the shared ``record_event`` helper;
- records the question + intent + grounding status + citations (NOT the answer
  prose), pinned to the data version in force;
- is a strict side-record: best-effort, never fatal, and an offline no-op when no
  audit DSN is configured (NullAuditSink);
- renders a one-line banker/examiner summary via ``summarise_event``.

These mirror the existing audit tests' use of ``InMemoryAuditSink`` + explicit
``sink=`` injection so they are pure and offline.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.record import record_event, summarise_event
from app.backend.audit.sink import InMemoryAuditSink, NullAuditSink
from app.shared.models.accounting import TrialBalance


def _state_like() -> SimpleNamespace:
    """A minimal state-like object the ledger reads via getattr (as the UI does)."""
    return SimpleNamespace(
        shisanhyo=[
            TrialBalance(
                period=dt.date(2025, 3, 31),
                uriage=100_000_000,
                uriage_genka=80_000_000,
                hanbaihi=15_000_000,
            )
        ],
        tdb_code="1234567",
        hojin_bango="1234567890123",
        tdb_score=42,
        working_capital_gap=-12_000_000,
        net_worth=5_000_000,
        is_insolvent=False,
    )


def _payload() -> dict[str, object]:
    return {
        "question": "なぜこの区分になりましたか？",
        "intent": "explain",
        "grounded": True,
        "citations": ["ews_score", "classification_reason"],
    }


def test_companion_query_is_recorded_and_chains() -> None:
    """A companion_query event is appended and forms a valid hash chain."""
    sink = InMemoryAuditSink()
    record_event(
        AuditEventType.COMPANION_QUERY,
        state=_state_like(),
        payload=_payload(),
        actor="banker",
        thread_id="t-1",
        sink=sink,
    )
    events = sink.read("t-1")
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type is AuditEventType.COMPANION_QUERY
    assert ev.actor == "banker"
    assert ev.tdb_code == "1234567"
    # Question + answer metadata are recorded; the answer prose is NOT.
    assert ev.payload["question"] == "なぜこの区分になりましたか？"
    assert ev.payload["grounded"] is True
    assert ev.payload["citations"] == ["ews_score", "classification_reason"]
    # Pinned to the data version in force, and tamper-evident.
    assert ev.data_version
    assert ev.hash_is_valid()
    assert sink.verify_chain("t-1").ok


def test_companion_query_chains_after_a_prior_event() -> None:
    """A companion query links to the previous event for the same thread."""
    sink = InMemoryAuditSink()
    record_event(
        AuditEventType.CLASSIFICATION,
        state=_state_like(),
        payload={"fsa_classification": "要注意先", "ews_score": 62},
        thread_id="t-2",
        sink=sink,
    )
    record_event(
        AuditEventType.COMPANION_QUERY,
        state=_state_like(),
        payload=_payload(),
        actor="banker",
        thread_id="t-2",
        sink=sink,
    )
    events = sink.read("t-2")
    assert len(events) == 2
    assert events[1].prev_hash == events[0].content_hash
    assert sink.verify_chain("t-2").ok


def test_companion_query_is_offline_noop_with_null_sink() -> None:
    """With the Null sink (no DSN), recording is a no-op and never raises."""
    sink = NullAuditSink()
    record_event(
        AuditEventType.COMPANION_QUERY,
        state=_state_like(),
        payload=_payload(),
        actor="banker",
        thread_id="t-3",
        sink=sink,
    )
    assert sink.read("t-3") == []


def test_companion_query_summary_line() -> None:
    """summarise_event renders a one-line summary with the grounding status."""
    sink = InMemoryAuditSink()
    record_event(
        AuditEventType.COMPANION_QUERY,
        state=_state_like(),
        payload=_payload(),
        actor="banker",
        thread_id="t-4",
        sink=sink,
    )
    summary = summarise_event(sink.read("t-4")[0])
    assert "AI助言の質問" in summary
    assert "接地済" in summary  # grounded == True


def test_companion_query_summary_marks_unverified() -> None:
    """An ungrounded answer is summarised as carrying unverified commentary."""
    sink = InMemoryAuditSink()
    payload = {**_payload(), "grounded": False}
    record_event(
        AuditEventType.COMPANION_QUERY,
        state=_state_like(),
        payload=payload,
        actor="banker",
        thread_id="t-5",
        sink=sink,
    )
    summary = summarise_event(sink.read("t-5")[0])
    assert "未検証あり" in summary
