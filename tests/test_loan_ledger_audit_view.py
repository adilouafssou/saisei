"""Verifier for the Audit-tab loan-ledger display read.

No CI here, so this pins ``_loan_ledger_rows``: it reads a facility's durable
loan-event ledger from the store and maps each event to a display dict (status
kanji/english, actor, note, timestamp), is a no-op for an empty loan_id, and
returns [] when the store has no history. The store is injected via a seeded
fake + SimpleNamespace settings patched at the lazily-imported source modules,
so the test is fully offline.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from app.frontend.state import _loan_ledger_rows
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


class _SeededStore:
    def __init__(self, loan_id: str, events: list[LoanEvent]) -> None:
        self._loan_id = loan_id
        self._events = events

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        return None

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        return list(self._events) if loan_id == self._loan_id else []


def _patch(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    import app.backend.portfolio.loan_store_postgres as store_mod
    import app.shared.settings as settings_mod

    monkeypatch.setattr(store_mod, "get_loan_store", lambda dsn: store)
    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(loan_dsn="postgresql://x", loan_tenant_default="t"),
    )


def test_empty_loan_id_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        _SeededStore(
            "L-1",
            [LoanEvent(status=LoanStatus.PERFORMING, at=_AT, actor="system")],
        ),
    )
    assert _loan_ledger_rows("") == []


def test_no_history_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _SeededStore("L-OTHER", []))
    assert _loan_ledger_rows("L-1") == []


def test_maps_events_to_display_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        LoanEvent(status=LoanStatus.PERFORMING, at=_AT, actor="system", note=""),
        LoanEvent(
            status=LoanStatus.WORKOUT,
            at=_AT + dt.timedelta(days=1),
            actor="system",
            note="FSA workout handoff",
        ),
    ]
    _patch(monkeypatch, _SeededStore("L-1", events))
    rows = _loan_ledger_rows("L-1")
    assert len(rows) == 2
    assert rows[0]["status_kanji"] == "正常"
    assert rows[0]["status_english"] == "Performing"
    assert rows[1]["status_kanji"] == "管理回収"
    assert rows[1]["status_english"] == "Workout"
    assert rows[1]["actor"] == "system"
    assert rows[1]["note"] == "FSA workout handoff"
    # Every row carries the five display keys.
    for row in rows:
        assert set(row) == {"at", "status_kanji", "status_english", "actor", "note"}
