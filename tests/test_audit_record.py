"""Tests for record_event + version hashing (spec §10, tests 5 & 8).

Test 5 — best-effort / never fatal: a sink whose append raises does NOT
         propagate out of record_event (it logs + swallows).
Test 8 — data-version pinning: changing a borrower input changes data_version;
         changing a relevant constant changes thresholds_version.
Plus: end-to-end chain linking through record_event with an InMemory sink, and
      the offline no-op (NullSink) path.

Offline, deterministic; imports only from ``app.*`` + stdlib.
"""

from __future__ import annotations

import datetime as dt

from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.record import (
    compute_data_version,
    compute_thresholds_version,
    record_event,
)
from app.backend.audit.sink import InMemoryAuditSink, NullAuditSink
from app.backend.state import SaiseiState
from app.shared.models.accounting import TrialBalance
from app.shared.settings import Settings


def _state(**overrides: object) -> SaiseiState:
    base: dict[str, object] = {
        "tdb_code": "1234567",
        "hojin_bango": "1234567890123",
        "tdb_score": 41,
        "working_capital_gap": -5_000_000,
        "ews_score": 62.5,
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
    # thread_id is a UI/runtime concept, not a SaiseiState field; pop it before
    # constructing the model (extra="forbid") and set it as an attribute below.
    thread_id = base.pop("thread_id", "t-1")
    state = SaiseiState(**base)
    # record_event reads thread_id via getattr, so set it on the instance.
    object.__setattr__(state, "thread_id", thread_id)
    return state


class _BoomSink(NullAuditSink):
    """A sink whose append always raises (to prove record_event swallows it)."""

    def append(self, event: AuditEvent) -> None:  # noqa: D102
        raise RuntimeError("backend down")


class TestRecordEventNeverFatal:
    """Test 5: record_event is best-effort and never raises."""

    def test_append_failure_is_swallowed(self) -> None:
        # Must not raise even though the sink's append blows up.
        record_event(
            AuditEventType.CLASSIFICATION,
            state=_state(),
            payload={"fsa_classification": "要注意先"},
            sink=_BoomSink(),
        )

    def test_returns_none(self) -> None:
        out = record_event(  # type: ignore[func-returns-value]
            AuditEventType.CLASSIFICATION,
            state=_state(),
            payload={},
            sink=InMemoryAuditSink(),
        )
        assert out is None

    def test_offline_nullsink_is_noop(self) -> None:
        sink = NullAuditSink()
        record_event(
            AuditEventType.CLASSIFICATION,
            state=_state(),
            payload={"x": 1},
            settings=Settings(audit_dsn=""),
            sink=sink,
        )
        assert sink.read("t-1") == []


class TestRecordEventChaining:
    """record_event writes a linked, verifiable chain through a real sink."""

    def test_events_are_chained_and_verify(self) -> None:
        sink = InMemoryAuditSink()
        state = _state(thread_id="t-chain")
        record_event(AuditEventType.CLASSIFICATION, state=state, payload={"n": 1}, sink=sink)
        record_event(AuditEventType.GUARANTEE_RELEASE, state=state, payload={"n": 2}, sink=sink)
        record_event(
            AuditEventType.HUMAN_DECISION,
            state=state,
            payload={"decision": "approve"},
            actor="banker-1",
            sink=sink,
        )
        events = sink.read("t-chain")
        assert len(events) == 3
        assert events[0].prev_hash == ""
        assert events[1].prev_hash == events[0].content_hash
        assert events[2].prev_hash == events[1].content_hash
        assert events[2].actor == "banker-1"
        assert sink.verify_chain("t-chain").ok

    def test_event_carries_identity_and_versions(self) -> None:
        sink = InMemoryAuditSink()
        record_event(
            AuditEventType.CLASSIFICATION,
            state=_state(thread_id="t-id"),
            payload={"fsa_classification": "要注意先"},
            sink=sink,
        )
        ev = sink.read("t-id")[0]
        assert ev.tdb_code == "1234567"
        assert ev.hojin_bango == "1234567890123"
        assert ev.event_type is AuditEventType.CLASSIFICATION
        assert ev.data_version and ev.thresholds_version
        assert ev.hash_is_valid()


class TestDataVersionPinning:
    """Test 8: version hashes pin the inputs / thresholds."""

    def test_data_version_changes_with_input(self) -> None:
        base = compute_data_version(_state())
        assert compute_data_version(_state(tdb_score=99)) != base
        assert compute_data_version(_state(working_capital_gap=0)) != base
        assert compute_data_version(_state(net_worth=-1)) != base

    def test_data_version_stable_for_same_inputs(self) -> None:
        assert compute_data_version(_state()) == compute_data_version(_state())

    def test_thresholds_version_is_stable_and_nonempty(self) -> None:
        v = compute_thresholds_version()
        assert v and v == compute_thresholds_version()
