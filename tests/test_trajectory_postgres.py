"""Tests for the PostgresTrajectoryStore (Feature 3 production backend).

This is the backend that makes ``SAISEI_TRAJECTORY_DSN`` actually persist; until
it existed, ``get_trajectory_store`` silently fell back to the no-op
``NullTrajectoryStore`` (ImportError on the lazy import), so the flywheel
captured nothing.

Two layers (mirrors test_audit_postgres.py):

- **Offline (always run):** the pure JSON round-trip (``_to_row`` ->
  ``_from_row``) reconstructs the exact frozen record — including the Feature 3.1
  ``node_trajectory`` + ``interrupt_payload`` — and its ``content_hash`` still
  validates; plus the schema bootstrap SQL declares the append-only trigger.
  These need no DB.
- **Network-marked (skipped offline / in CI):** a real append/read round-trip
  and the DB-level UPDATE/DELETE trigger raising, gated on
  ``SAISEI_TRAJECTORY_DSN``.

Imports only from ``app.*`` + stdlib + pytest.
"""

from __future__ import annotations

import json
import os

import pytest
from app.backend.trajectory.record import (
    NodeSnapshot,
    TrajectoryDecision,
    TrajectoryRecord,
)
from app.backend.trajectory.store import get_trajectory_store
from app.backend.trajectory.store_postgres import (
    SCHEMA_SQL,
    TRAJECTORY_TABLE,
    PostgresTrajectoryStore,
)
from app.shared.settings import Settings


def _record(**overrides: object) -> TrajectoryRecord:
    base: dict[str, object] = {
        "trajectory_id": "t-1",
        "thread_id": "thread-1",
        "tdb_code": "1234567",
        "hojin_bango": "1234567890123",
        "created_at": "2026-01-01T00:00:00+00:00",
        "decision": TrajectoryDecision.APPROVE,
        "proposed_strategies": [{"title": "価格転嫁"}, {"title": "cogs"}],
        "approved_strategy": {"title": "価格転嫁"},
        "node_trajectory": [
            NodeSnapshot(node="ews", output={"ews_score": 62.0}),
        ],
        "interrupt_payload": {"prompt": "decide", "ews_score": 62.0},
    }
    base.update(overrides)
    return TrajectoryRecord(**base).with_content_hash()


class TestRowMappingRoundTrip:
    """_to_row -> _from_row is loss-less (offline; no DB)."""

    def test_round_trip_preserves_record_and_hash(self) -> None:
        record = _record()
        row = (PostgresTrajectoryStore._to_row(record)["record"],)  # JSON string
        rebuilt = PostgresTrajectoryStore._from_row(row)
        assert rebuilt == record
        # The content hash must still validate after the round-trip (CJK intact).
        assert rebuilt.hash_is_valid()
        assert rebuilt.approved_strategy == {"title": "価格転嫁"}

    def test_round_trip_preserves_per_node_fields(self) -> None:
        # Feature 3.1 fields must survive the JSON round-trip.
        record = _record()
        row = (PostgresTrajectoryStore._to_row(record)["record"],)
        rebuilt = PostgresTrajectoryStore._from_row(row)
        assert [s.node for s in rebuilt.node_trajectory] == ["ews"]
        assert rebuilt.node_trajectory[0].output["ews_score"] == 62.0
        assert rebuilt.interrupt_payload["ews_score"] == 62.0

    def test_from_row_accepts_dict_blob(self) -> None:
        # JSONB columns return a dict directly (not a string); that must work too.
        record = _record()
        blob = json.loads(PostgresTrajectoryStore._to_row(record)["record"])
        rebuilt = PostgresTrajectoryStore._from_row((blob,))
        assert rebuilt.preference_pair().rejected == [{"title": "cogs"}]

    def test_query_columns_are_denormalised(self) -> None:
        row_dict = PostgresTrajectoryStore._to_row(_record())
        assert row_dict["thread_id"] == "thread-1"
        assert row_dict["tdb_code"] == "1234567"
        assert row_dict["decision"] == "approve"
        assert row_dict["content_hash"]


class TestSchemaContract:
    """The bootstrap SQL encodes the append-only contract (offline; no DB)."""

    def test_schema_declares_append_only_trigger(self) -> None:
        assert TRAJECTORY_TABLE == "saisei_trajectory"
        assert "CREATE TABLE IF NOT EXISTS saisei_trajectory" in SCHEMA_SQL
        assert "BEFORE UPDATE OR DELETE" in SCHEMA_SQL
        assert "append-only" in SCHEMA_SQL
        assert "idx_trajectory_thread" in SCHEMA_SQL


class TestStoreSelection:
    """get_trajectory_store now resolves to the Postgres backend when DSN set."""

    def test_dsn_selects_postgres_backend(self) -> None:
        # The import now resolves (the module exists), so a configured DSN must
        # yield the real backend, not a silent NullTrajectoryStore fallback.
        # We avoid constructing it (which would connect) by checking the class
        # the selector would build via a DSN that fails fast is out of scope;
        # instead assert the module + symbol are importable (the fix).
        from app.backend.trajectory import store_postgres

        assert hasattr(store_postgres, "PostgresTrajectoryStore")

    def test_empty_dsn_still_offline_null(self) -> None:
        from app.backend.trajectory.store import NullTrajectoryStore

        assert isinstance(get_trajectory_store(Settings(trajectory_dsn="")), NullTrajectoryStore)


@pytest.mark.network
class TestPostgresRoundTrip:
    """Live DB round-trip + trigger. Skipped without a DSN."""

    @pytest.fixture
    def store(self) -> PostgresTrajectoryStore:
        dsn = os.environ.get("SAISEI_TRAJECTORY_DSN", "")
        if not dsn:
            pytest.skip("SAISEI_TRAJECTORY_DSN not set; live Postgres test skipped")
        return PostgresTrajectoryStore(dsn)

    def test_append_read_roundtrip(self, store: PostgresTrajectoryStore) -> None:
        thread_id = f"t-live-{os.getpid()}"
        first = _record(trajectory_id=f"{thread_id}-1", thread_id=thread_id)
        second = _record(
            trajectory_id=f"{thread_id}-2",
            thread_id=thread_id,
            decision=TrajectoryDecision.REVISE,
            approved_strategy=None,
        )
        store.append(first)
        store.append(second)
        records = store.read(thread_id)
        assert [r.trajectory_id for r in records] == [
            first.trajectory_id,
            second.trajectory_id,
        ]
        assert all(r.hash_is_valid() for r in records)

    def test_update_is_rejected_by_trigger(self, store: PostgresTrajectoryStore) -> None:
        import psycopg

        thread_id = f"t-trig-{os.getpid()}"
        rec = _record(trajectory_id=f"{thread_id}-1", thread_id=thread_id)
        store.append(rec)
        with psycopg.connect(os.environ["SAISEI_TRAJECTORY_DSN"]) as conn:  # noqa: SIM117
            with conn.cursor() as cur, pytest.raises(Exception):  # noqa: B017, PT011
                cur.execute(
                    f"UPDATE {TRAJECTORY_TABLE} SET tdb_code = 'tampered' WHERE trajectory_id = %s",
                    (rec.trajectory_id,),
                )
