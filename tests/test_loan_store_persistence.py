"""Verifier for durable loan-event persistence (best-effort side-record).

No CI here, so this pins the ``_persist_loan_events`` helpers on both the
workout path and the HITL approve path: each must append the newly recorded
LoanEvent(s) to the loan store under the configured tenant + loan_id, must be a
no-op when no loan / no events, and must NEVER raise (a store failure is
swallowed). The store is injected via a capturing fake (no DB), and settings via
a simple namespace, so the test is fully offline.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import app.backend.nodes.workout as workout_mod
import pytest
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


class _CapturingStore:
    """In-memory loan store that records appends (no DB)."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, str, LoanEvent]] = []

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        self.appended.append((tenant_id, loan_id, event))

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        return [e for (t, lid, e) in self.appended if t == tenant_id and lid == loan_id]


class _ExplodingStore:
    """A loan store whose append always raises, to prove failures are swallowed."""

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        raise RuntimeError("db down")

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        return []


def _event(status: LoanStatus = LoanStatus.WORKOUT) -> dict[str, Any]:
    return LoanEvent(status=status, at=_AT, actor="system", note="test").model_dump(mode="json")


def _patch_store_and_settings(
    monkeypatch: pytest.MonkeyPatch,
    store: Any,
    *,
    loan_dsn: str = "postgresql://x",
    tenant: str = "tenant-a",
) -> None:
    """Patch get_loan_store + get_settings at the modules the helper imports from.

    The helper imports both lazily inside the function body, so patching the
    source modules (loan_store_postgres / app.shared.settings) is what the
    runtime lookup will see.
    """
    import app.backend.portfolio.loan_store_postgres as store_mod
    import app.shared.settings as settings_mod

    monkeypatch.setattr(store_mod, "get_loan_store", lambda dsn: store)
    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(loan_dsn=loan_dsn, loan_tenant_default=tenant),
    )


def _state(**kwargs: Any) -> Any:
    """A minimal state-like object for the helper (it only reads loan_id)."""
    base: dict[str, Any] = {"loan_id": "L-1"}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_persist_appends_events_with_tenant_and_loan_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _CapturingStore()
    _patch_store_and_settings(monkeypatch, store, tenant="tenant-x")
    events = [_event(LoanStatus.WORKOUT)]
    workout_mod._persist_loan_events(_state(loan_id="L-9"), events)
    assert len(store.appended) == 1
    tenant, loan_id, event = store.appended[0]
    assert tenant == "tenant-x"
    assert loan_id == "L-9"
    assert event.status is LoanStatus.WORKOUT


def test_persist_noop_without_events(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _CapturingStore()
    _patch_store_and_settings(monkeypatch, store)
    workout_mod._persist_loan_events(_state(), [])
    assert store.appended == []


def test_persist_noop_without_loan_id(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _CapturingStore()
    _patch_store_and_settings(monkeypatch, store)
    workout_mod._persist_loan_events(_state(loan_id=""), [_event()])
    assert store.appended == []


def test_persist_swallows_store_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # An exploding store must never propagate -- persistence is best-effort.
    _patch_store_and_settings(monkeypatch, _ExplodingStore())
    # Must not raise.
    workout_mod._persist_loan_events(_state(), [_event()])


def test_persist_appends_multiple_events(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _CapturingStore()
    _patch_store_and_settings(monkeypatch, store)
    events = [_event(LoanStatus.RESTRUCTURED), _event(LoanStatus.WORKOUT)]
    workout_mod._persist_loan_events(_state(loan_id="L-2"), events)
    assert [e.status for (_t, _l, e) in store.appended] == [
        LoanStatus.RESTRUCTURED,
        LoanStatus.WORKOUT,
    ]


def test_orchestrator_helper_shares_the_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The HITL orchestrator carries its own _persist_loan_events with the same
    # contract; assert it persists identically so both call sites agree.
    import app.backend.agents.turnaround_orchestrator as orch_mod

    store = _CapturingStore()
    _patch_store_and_settings(monkeypatch, store, tenant="tenant-hitl")
    orch_mod._persist_loan_events(_state(loan_id="L-7"), [_event(LoanStatus.RESTRUCTURED)])
    assert len(store.appended) == 1
    tenant, loan_id, event = store.appended[0]
    assert tenant == "tenant-hitl"
    assert loan_id == "L-7"
    assert event.status is LoanStatus.RESTRUCTURED
