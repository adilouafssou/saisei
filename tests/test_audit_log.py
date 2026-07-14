"""Tests for the audit-event model + canonical hashing (spec §10, tests 1 & 4).

Test 1 — hashing is canonical + deterministic: same event -> same content_hash;
         dict/key insertion order does not change the hash.
Test 4 — append-only at the model layer: AuditEvent is frozen (mutation raises).

Offline, deterministic; imports only from ``app.*`` + stdlib.
"""

from __future__ import annotations

import json

import pytest
from app.backend.audit.audit_log import (
    AuditEvent,
    AuditEventType,
    canonical_json,
    compute_content_hash,
)
from app.backend.audit.sink import (
    ChainVerdict,
    InMemoryAuditSink,
    NullAuditSink,
    verify_chain,
)
from pydantic import ValidationError


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "event_id": "evt-1",
        "thread_id": "thread-abc",
        "tdb_code": "1234567",
        "hojin_bango": "1234567890123",
        "event_type": AuditEventType.CLASSIFICATION,
        "created_at": "2026-06-18T00:00:00+00:00",
        "actor": "system",
        "payload": {"fsa_classification": "要注意先", "ews_score": 62.5},
        "data_version": "dv-1",
        "thresholds_version": "tv-1",
        "prev_hash": "",
    }
    base.update(overrides)
    return AuditEvent(**base)


class TestCanonicalHashing:
    """Test 1: deterministic, order-independent canonical hashing."""

    def test_same_event_same_hash(self) -> None:
        assert compute_content_hash(_event()) == compute_content_hash(_event())

    def test_hash_is_64_char_hex(self) -> None:
        digest = compute_content_hash(_event())
        assert len(digest) == 64
        int(digest, 16)  # valid hex (raises if not)

    def test_payload_key_order_does_not_change_hash(self) -> None:
        """Dict insertion order in the payload must not affect the hash."""
        a = _event(payload={"a": 1, "b": 2, "c": 3})
        b = _event(payload={"c": 3, "b": 2, "a": 1})
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_canonical_json_excludes_content_hash(self) -> None:
        """content_hash must not appear in its own hash input."""
        ev = _event().model_copy(update={"content_hash": "deadbeef"})
        assert "content_hash" not in canonical_json(ev)
        # And setting content_hash does not change the computed digest.
        assert compute_content_hash(ev) == compute_content_hash(_event())

    def test_canonical_json_is_sorted_and_unescaped(self) -> None:
        text = canonical_json(_event())
        parsed = json.loads(text)
        # Round-trips, keys sorted, CJK preserved literally (not \uXXXX).
        assert parsed["tdb_code"] == "1234567"
        assert "要注意先" in text

    def test_changing_any_field_changes_hash(self) -> None:
        base = compute_content_hash(_event())
        assert compute_content_hash(_event(tdb_code="7654321")) != base
        assert compute_content_hash(_event(prev_hash="x")) != base
        assert compute_content_hash(_event(actor="banker")) != base

    def test_with_content_hash_sets_valid_hash(self) -> None:
        ev = _event().with_content_hash()
        assert ev.content_hash == compute_content_hash(_event())
        assert ev.hash_is_valid()

    def test_event_type_serialises_to_value(self) -> None:
        """The StrEnum serialises to its string value in the canonical form."""
        assert '"event_type":"classification"' in canonical_json(_event())


class TestFrozenModel:
    """Test 4: append-only at the model layer — AuditEvent is immutable."""

    def test_mutation_raises(self) -> None:
        ev = _event()
        with pytest.raises(ValidationError):
            ev.tdb_code = "0000000"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            _event(unexpected_field="nope")

    def test_with_content_hash_returns_new_instance(self) -> None:
        """with_content_hash does not mutate the original (frozen) event."""
        original = _event()
        hashed = original.with_content_hash()
        assert original.content_hash == ""
        assert hashed is not original
        assert hashed.content_hash != ""

    def test_tamper_detection_via_hash_is_valid(self) -> None:
        """A hashed event whose hash is recomputed against altered fields fails."""
        hashed = _event().with_content_hash()
        # Build a copy with the same (now-stale) content_hash but a changed field.
        tampered = hashed.model_copy(update={"tdb_code": "9999999"})
        assert not tampered.hash_is_valid()


def _chained(*events: AuditEvent) -> list[AuditEvent]:
    """Return events hash-chained in order (prev_hash linkage + content_hash)."""
    out: list[AuditEvent] = []
    prev = ""
    for ev in events:
        linked = ev.model_copy(update={"prev_hash": prev}).with_content_hash()
        out.append(linked)
        prev = linked.content_hash
    return out


class TestChainVerification:
    """Test 2 (chain links) + test 3 (tamper detection) via verify_chain."""

    def test_empty_chain_is_ok(self) -> None:
        assert verify_chain([]).ok

    def test_genesis_prev_hash_is_empty(self) -> None:
        chain = _chained(_event(event_id="e1"))
        assert chain[0].prev_hash == ""
        assert verify_chain(chain).ok

    def test_links_reference_previous_content_hash(self) -> None:
        chain = _chained(
            _event(event_id="e1"),
            _event(event_id="e2"),
            _event(event_id="e3"),
        )
        assert chain[1].prev_hash == chain[0].content_hash
        assert chain[2].prev_hash == chain[1].content_hash
        verdict = verify_chain(chain)
        assert verdict.ok
        assert verdict.broken_at is None

    def test_tampered_payload_breaks_chain(self) -> None:
        """Test 3: mutating a stored event is detected at that event."""
        chain = _chained(_event(event_id="e1"), _event(event_id="e2"))
        # Keep e2's stale content_hash but alter a field.
        chain[1] = chain[1].model_copy(update={"payload": {"ews_score": 99.0}})
        verdict = verify_chain(chain)
        assert not verdict.ok
        assert verdict.broken_at == "e2"

    def test_reordered_events_break_linkage(self) -> None:
        chain = _chained(_event(event_id="e1"), _event(event_id="e2"))
        reordered = [chain[1], chain[0]]
        verdict = verify_chain(reordered)
        assert not verdict.ok
        assert verdict.broken_at == "e2"  # first event whose prev_hash != ""


class TestInMemorySink:
    """Test 2 (ordered append/read) + append-only interface shape."""

    def test_append_read_preserves_order_per_thread(self) -> None:
        sink = InMemoryAuditSink()
        a = _event(event_id="a", thread_id="t1")
        b = _event(event_id="b", thread_id="t1")
        c = _event(event_id="c", thread_id="t2")
        sink.append(a)
        sink.append(b)
        sink.append(c)
        assert [e.event_id for e in sink.read("t1")] == ["a", "b"]
        assert [e.event_id for e in sink.read("t2")] == ["c"]
        assert sink.read("unknown") == []

    def test_read_returns_a_copy(self) -> None:
        """Mutating the returned list must not corrupt the store."""
        sink = InMemoryAuditSink()
        sink.append(_event(event_id="a", thread_id="t1"))
        got = sink.read("t1")
        got.clear()
        assert len(sink.read("t1")) == 1

    def test_sink_verify_chain_roundtrip(self) -> None:
        sink = InMemoryAuditSink()
        for ev in _chained(_event(event_id="e1"), _event(event_id="e2")):
            sink.append(ev)
        assert sink.verify_chain("thread-abc").ok

    def test_sink_has_no_update_or_delete(self) -> None:
        """Append-only at the interface: no mutation methods exist."""
        sink = InMemoryAuditSink()
        assert not hasattr(sink, "update")
        assert not hasattr(sink, "delete")


class TestNullSinkOfflineNoop:
    """Test 6: the offline default is a no-op sink."""

    def test_null_sink_discards_and_reads_empty(self) -> None:
        sink = NullAuditSink()
        sink.append(_event())
        assert sink.read("thread-abc") == []
        assert sink.verify_chain("thread-abc").ok

    def test_get_audit_sink_defaults_to_null_offline(self) -> None:
        """With no audit_dsn configured, the selector returns the Null sink."""
        from app.backend.audit.sink import NullAuditSink as _Null
        from app.backend.audit.sink import get_audit_sink
        from app.shared.settings import Settings

        sink = get_audit_sink(Settings(audit_dsn=""))
        assert isinstance(sink, _Null)

    def test_chainverdict_shape(self) -> None:
        v = ChainVerdict(ok=True)
        assert v.ok and v.broken_at is None and v.reason == ""
