"""Verifier for durable origination loan-event persistence (side-record).

No CI here, so this pins the durable-ledger persistence on every origination
transition. Each event-producing node -- intake (APPLIED), loan_origination
(APPLIED -> UNDER_REVIEW), the HITL credit decision (UNDER_REVIEW ->
APPROVED/DECLINED), and disbursement (APPROVED -> DISBURSED) -- must append its
newly recorded LoanEvent(s) to the loan store under the configured tenant +
loan_id, must be a no-op when no loan / no events, and must NEVER raise (a store
failure is swallowed). The store is injected via a capturing fake (no DB) and
settings via a simple namespace, so the test is fully offline.

The shared seam under test is
``app.backend.portfolio.loan_store_postgres.persist_loan_events``; the four nodes
all route through it, so the bulk of the contract is proven once on the seam and
then confirmed end-to-end at one representative node (the HITL credit decision,
the transition that matters most).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import app.backend.portfolio.loan_store_postgres as store_mod
import pytest
from app.backend.portfolio.loan_store_postgres import persist_loan_events
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


def _event(status: LoanStatus) -> dict[str, Any]:
    return LoanEvent(status=status, at=_AT, actor="system", note="test").model_dump(mode="json")


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    store: Any,
    *,
    loan_dsn: str = "postgresql://x",
    tenant: str = "tenant-a",
) -> None:
    """Patch get_loan_store + get_settings at the modules the seam imports from.

    ``persist_loan_events`` looks up ``get_loan_store`` in its own module and
    imports ``get_settings`` lazily from ``app.shared.settings``, so patch both
    source modules (what the runtime lookup will see).
    """
    import app.shared.settings as settings_mod

    monkeypatch.setattr(store_mod, "get_loan_store", lambda dsn: store)
    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(loan_dsn=loan_dsn, loan_tenant_default=tenant),
    )


def _state(**kwargs: Any) -> Any:
    """A minimal state-like object (the seam only reads loan_id)."""
    base: dict[str, Any] = {"loan_id": "L-1"}
    base.update(kwargs)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# The shared seam
# ---------------------------------------------------------------------------


def test_persist_appends_with_tenant_and_loan_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _CapturingStore()
    _patch(monkeypatch, store, tenant="tenant-x")
    persist_loan_events(_state(loan_id="L-9"), [_event(LoanStatus.APPLIED)])
    assert len(store.appended) == 1
    tenant, loan_id, event = store.appended[0]
    assert tenant == "tenant-x"
    assert loan_id == "L-9"
    assert event.status is LoanStatus.APPLIED


def test_persist_appends_multiple_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _CapturingStore()
    _patch(monkeypatch, store)
    persist_loan_events(
        _state(loan_id="L-2"),
        [_event(LoanStatus.APPROVED), _event(LoanStatus.DISBURSED)],
    )
    assert [e.status for (_t, _l, e) in store.appended] == [
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
    ]


def test_persist_noop_without_events(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _CapturingStore()
    _patch(monkeypatch, store)
    persist_loan_events(_state(), [])
    assert store.appended == []


def test_persist_noop_without_loan_id(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _CapturingStore()
    _patch(monkeypatch, store)
    persist_loan_events(_state(loan_id=""), [_event(LoanStatus.APPLIED)])
    assert store.appended == []


def test_persist_swallows_store_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # An exploding store must never propagate -- persistence is best-effort.
    _patch(monkeypatch, _ExplodingStore())
    persist_loan_events(_state(), [_event(LoanStatus.APPLIED)])  # must not raise


def test_persist_offline_is_noop_without_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no loan_dsn the real factory returns NullLoanStore -> nothing stored.
    import app.shared.settings as settings_mod

    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(loan_dsn="", loan_tenant_default="default"),
    )
    # Does not raise and stores nothing (NullLoanStore.append is a no-op).
    persist_loan_events(_state(loan_id="L-3"), [_event(LoanStatus.APPLIED)])


# ---------------------------------------------------------------------------
# End-to-end: a node routes its recorded events through the seam
# ---------------------------------------------------------------------------


def test_hitl_credit_decision_persists_the_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HITL node persists the UNDER_REVIEW -> APPROVED credit transition.

    Drives ``origination_hitl_node`` past the interrupt with an approve decision
    against an UNDER_REVIEW facility and asserts the recorded credit event is
    appended to the store under the configured tenant + loan_id.
    """
    from app.backend.agents.origination_orchestrator import origination_hitl_node
    from app.backend.state import SaiseiState

    store = _CapturingStore()
    _patch(monkeypatch, store, tenant="tenant-hitl")

    applied = LoanEvent(status=LoanStatus.APPLIED, at=_AT, actor="system").model_dump(mode="json")
    under_review = LoanEvent(status=LoanStatus.UNDER_REVIEW, at=_AT, actor="system").model_dump(
        mode="json"
    )
    state = SaiseiState(
        tdb_code="1234567",
        loan_id="L-hitl",
        loan_events=[applied, under_review],
    )

    # Patch interrupt() so the node proceeds with the banker's approve decision
    # instead of pausing (we are unit-testing the node, not the graph pause).
    import app.backend.agents.origination_orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "interrupt", lambda _payload: {"decision": "approve"})

    out = origination_hitl_node(state)
    assert out["origination_decision"] == "approve"
    statuses = [e.status for (_t, _l, e) in store.appended]
    assert LoanStatus.APPROVED in statuses
    assert all(loan_id == "L-hitl" for (_t, loan_id, _e) in store.appended)
    assert all(tenant == "tenant-hitl" for (tenant, _l, _e) in store.appended)
