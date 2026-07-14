"""Verifier for intake reconciliation against the durable loan ledger.

No CI here, so this pins the behaviour added on top of the intake bootstrap:
when the durable loan store holds a facility's history, intake must RESUME from
it (the true cross-run status, e.g. 管理回収) instead of re-seeding a fresh
PERFORMING chain; when there is no durable history (offline / empty), intake
must fall back to the bootstrap unchanged.

The store is injected via a capturing fake and settings via a SimpleNamespace,
patched at the lazily-imported source modules, so the test is fully offline.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from app.backend.nodes.financial_extraction import intake_node
from app.backend.state import SaiseiState
from app.shared.models.loan import LoanEvent, LoanStatus, current_status

# Fixture WITH lender_stakes -> intake attaches a loan (loan_id L-4000001000001).
_TDB_WITH_STAKES = "4000001"
_LOAN_ID = "L-4000001000001"

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)

_WORKOUT_HISTORY = (
    LoanStatus.APPLIED,
    LoanStatus.UNDER_REVIEW,
    LoanStatus.APPROVED,
    LoanStatus.DISBURSED,
    LoanStatus.PERFORMING,
    LoanStatus.WORKOUT,
)


class _SeededStore:
    """A loan store pre-seeded with one facility's durable history."""

    def __init__(self, loan_id: str, statuses: tuple[LoanStatus, ...]) -> None:
        self._loan_id = loan_id
        self._events = [
            LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system")
            for i, s in enumerate(statuses)
        ]

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        return None

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        return list(self._events) if loan_id == self._loan_id else []


class _ExplodingStore:
    """A store whose read raises, to prove intake falls back to the bootstrap."""

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        return None

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        raise RuntimeError("db down")


def _patch(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    """Patch get_loan_store + get_settings at the lazily-imported source modules."""
    import app.backend.portfolio.loan_store_postgres as store_mod
    import app.shared.settings as settings_mod

    monkeypatch.setattr(store_mod, "get_loan_store", lambda dsn: store)
    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(loan_dsn="postgresql://x", loan_tenant_default="t"),
    )


def test_resumes_from_durable_history(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _SeededStore(_LOAN_ID, _WORKOUT_HISTORY))
    out = intake_node(SaiseiState(tdb_code=_TDB_WITH_STAKES))
    assert out["loan_id"] == _LOAN_ID
    events = [LoanEvent.model_validate(e) for e in out["loan_events"]]
    # Resumed from the durable ledger -> current status is WORKOUT, not PERFORMING.
    assert current_status(events) is LoanStatus.WORKOUT
    assert len(events) == len(_WORKOUT_HISTORY)


def test_falls_back_to_bootstrap_without_durable_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Store returns [] for this facility -> intake seeds the fresh PERFORMING chain.
    _patch(monkeypatch, _SeededStore("L-OTHER", _WORKOUT_HISTORY))
    out = intake_node(SaiseiState(tdb_code=_TDB_WITH_STAKES))
    events = [LoanEvent.model_validate(e) for e in out["loan_events"]]
    assert current_status(events) is LoanStatus.PERFORMING


def test_read_failure_falls_back_to_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, _ExplodingStore())
    out = intake_node(SaiseiState(tdb_code=_TDB_WITH_STAKES))
    # A durable read failure must never break intake; it bootstraps instead.
    events = [LoanEvent.model_validate(e) for e in out["loan_events"]]
    assert current_status(events) is LoanStatus.PERFORMING
