"""Tests for the PostgresAuditSink (Feature 7, spec §10 test 9).

Two layers:

- **Offline (always run):** the pure row-mapping round-trip
  (``_to_row`` → ``_from_row``) preserves every field and a CJK payload, and the
  schema bootstrap SQL declares the append-only trigger. These need no DB.
- **Network-marked (skipped offline / in CI):** a real append/read round-trip
  and the DB-level UPDATE/DELETE trigger raising, gated on ``SAISEI_AUDIT_DSN``
  — mirroring the existing live-client posture (the live branch is confirmed
  against a real Postgres separately).

Imports only from ``app.*`` + stdlib + pytest.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.sink_postgres import (
    AUDIT_TABLE,
    SCHEMA_SQL,
    PostgresAuditSink,
)


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "event_id": "e-1",
        "thread_id": "t-1",
        "tdb_code": "1234567",
        "hojin_bango": "1234567890123",
        "event_type": AuditEventType.CLASSIFICATION,
        "created_at": dt.datetime(2025, 6, 30, tzinfo=dt.UTC).isoformat(),
        "actor": "system",
        "payload": {"fsa_classification": "要注意先", "ews_score": 62.5},
        "data_version": "abc123",
        "thresholds_version": "def456",
    }
    base.update(overrides)
    return AuditEvent(**base).with_content_hash()


class TestRowMappingRoundTrip:
    """_to_row -> _from_row is loss-less (offline; no DB)."""

    def test_round_trip_preserves_all_fields(self) -> None:
        event = _event(prev_hash="prev-hash-xyz")
        row_dict = PostgresAuditSink._to_row(event)
        # Build a SELECT-shaped tuple in the documented column order.
        row = (
            row_dict["event_id"],
            row_dict["thread_id"],
            row_dict["tdb_code"],
            row_dict["hojin_bango"],
            row_dict["event_type"],
            row_dict["actor"],
            row_dict["created_at"],
            row_dict["payload"],  # JSON string; _from_row normalises it
            row_dict["data_version"],
            row_dict["thresholds_version"],
            row_dict["content_hash"],
            row_dict["prev_hash"],
        )
        rebuilt = PostgresAuditSink._from_row(row)
        assert rebuilt == event
        # The content hash must still validate after the round-trip (CJK intact).
        assert rebuilt.hash_is_valid()
        assert rebuilt.payload["fsa_classification"] == "要注意先"

    def test_from_row_accepts_dict_payload(self) -> None:
        # JSONB columns return a dict directly (not a string); that must work too.
        event = _event()
        d = PostgresAuditSink._to_row(event)
        row = (
            d["event_id"],
            d["thread_id"],
            d["tdb_code"],
            d["hojin_bango"],
            d["event_type"],
            d["actor"],
            d["created_at"],
            {"fsa_classification": "要注意先", "ews_score": 62.5},  # dict, not str
            d["data_version"],
            d["thresholds_version"],
            d["content_hash"],
            d["prev_hash"],
        )
        rebuilt = PostgresAuditSink._from_row(row)
        assert rebuilt.payload["ews_score"] == 62.5


class TestSchemaContract:
    """The bootstrap SQL encodes the append-only contract (offline; no DB)."""

    def test_schema_declares_append_only_trigger(self) -> None:
        assert AUDIT_TABLE == "saisei_audit_log"
        assert "CREATE TABLE IF NOT EXISTS saisei_audit_log" in SCHEMA_SQL
        assert "BEFORE UPDATE OR DELETE" in SCHEMA_SQL
        assert "append-only" in SCHEMA_SQL
        # Idempotent index creation.
        assert "idx_audit_thread" in SCHEMA_SQL


@pytest.mark.network
class TestPostgresRoundTrip:
    """Live DB round-trip + trigger (spec test 9). Skipped without a DSN."""

    @pytest.fixture
    def sink(self) -> PostgresAuditSink:
        dsn = os.environ.get("SAISEI_AUDIT_DSN", "")
        if not dsn:
            pytest.skip("SAISEI_AUDIT_DSN not set; live Postgres test skipped")
        return PostgresAuditSink(dsn)

    def test_append_read_roundtrip_and_chain(self, sink: PostgresAuditSink) -> None:
        thread_id = f"t-live-{os.getpid()}"
        first = _event(event_id=f"{thread_id}-1", thread_id=thread_id)
        second = _event(
            event_id=f"{thread_id}-2",
            thread_id=thread_id,
            event_type=AuditEventType.GUARANTEE_RELEASE,
            prev_hash=first.content_hash,
        )
        sink.append(first)
        sink.append(second)
        events = sink.read(thread_id)
        assert [e.event_id for e in events] == [first.event_id, second.event_id]
        assert sink.verify_chain(thread_id).ok

    def test_update_is_rejected_by_trigger(self, sink: PostgresAuditSink) -> None:
        import psycopg

        thread_id = f"t-trig-{os.getpid()}"
        ev = _event(event_id=f"{thread_id}-1", thread_id=thread_id)
        sink.append(ev)
        with psycopg.connect(os.environ["SAISEI_AUDIT_DSN"]) as conn:  # noqa: SIM117
            with conn.cursor() as cur, pytest.raises(Exception):  # noqa: B017, PT011
                cur.execute(
                    f"UPDATE {AUDIT_TABLE} SET actor = 'tampered' WHERE event_id = %s",
                    (ev.event_id,),
                )
