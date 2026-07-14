"""Tests for the loan-event store (offline + network-marked).

Two layers, mirroring tests/test_audit_postgres.py:

- **Offline (always run):** the NullLoanStore no-op contract, the get_loan_store
  factory selection, the row-mapping round-trip (_to_row -> _from_row), and the
  append-only trigger declared in the schema bootstrap SQL. These need no DB.
- **Network-marked (skipped offline / in CI):** a real append/read round-trip
  gated on SAISEI_LOAN_DSN, mirroring the live-client posture.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from app.backend.portfolio.loan_store_postgres import (
    LOAN_EVENT_TABLE,
    SCHEMA_SQL,
    NullLoanStore,
    PostgresLoanStore,
    get_loan_store,
)
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def _event(status: LoanStatus = LoanStatus.PERFORMING) -> LoanEvent:
    return LoanEvent(status=status, at=_AT, actor="banker-1", note="条件変更")


# --- offline (always run) -----------------------------------------------


def test_null_store_is_noop() -> None:
    store = NullLoanStore()
    store.append("tenant-a", "L-1", _event())
    assert store.read("tenant-a", "L-1") == []


def test_factory_returns_null_without_dsn() -> None:
    assert isinstance(get_loan_store(None), NullLoanStore)
    assert isinstance(get_loan_store(""), NullLoanStore)


def test_row_mapping_round_trip() -> None:
    event = _event(LoanStatus.RESTRUCTURED)
    row = PostgresLoanStore._to_row("tenant-a", "L-1", event)
    assert row["tenant_id"] == "tenant-a"
    assert row["loan_id"] == "L-1"
    assert row["status"] == LoanStatus.RESTRUCTURED.value
    rebuilt = PostgresLoanStore._from_row((row["status"], row["at"], row["actor"], row["note"]))
    assert rebuilt.status is LoanStatus.RESTRUCTURED
    assert rebuilt.actor == "banker-1"
    assert rebuilt.note == "条件変更"
    assert rebuilt.at == _AT


def test_schema_declares_append_only_trigger() -> None:
    assert LOAN_EVENT_TABLE in SCHEMA_SQL
    assert "BEFORE UPDATE OR DELETE" in SCHEMA_SQL
    assert "append-only" in SCHEMA_SQL


# --- network-marked (skipped offline / in CI) ---------------------------

_DSN = os.environ.get("SAISEI_LOAN_DSN", "")


@pytest.mark.skipif(not _DSN, reason="SAISEI_LOAN_DSN not set (offline)")
def test_append_read_round_trip_live() -> None:  # pragma: no cover - network
    store = PostgresLoanStore(_DSN)
    store.append("tenant-live", "L-live", _event(LoanStatus.APPLIED))
    store.append("tenant-live", "L-live", _event(LoanStatus.UNDER_REVIEW))
    events = store.read("tenant-live", "L-live")
    assert [e.status for e in events][:2] == [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
    ]
