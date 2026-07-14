"""End-to-end integration test for the full loan-lifecycle arc.

No CI here, so this ties the whole arc together in ONE run -- the guard the
per-slice unit tests don't give on their own:

    record (workout_node)  ->  persist (loan store)  ->  reconcile (intake)

A genuinely-insolvent borrower is driven through the compiled graph with a real
in-memory loan store wired into the factory. The graph records the WORKOUT
transition and persists it; a SECOND assessment of the same borrower must then
resume from the persisted ledger (status WORKOUT) instead of re-seeding a fresh
PERFORMING chain. Fully offline (in-memory store + MemorySaver checkpointer).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from app.backend.graph import build_graph
from app.backend.state import SaiseiState
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

# The Aichi fixture: net_worth override drives it to 実質破綻先 (-> workout),
# and it carries lender_stakes so a loan attaches at intake.
_INSOLVENT_TDB = "1234567"
_LOAN_ID = "L-1234567890123"


class _InMemoryLoanStore:
    """A real (in-memory) append-only loan store shared across runs in a test."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], list[LoanEvent]] = {}

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        self._by_key.setdefault((tenant_id, loan_id), []).append(event)

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        return list(self._by_key.get((tenant_id, loan_id), []))


def _cfg(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


@pytest.fixture
def wired_store(monkeypatch: pytest.MonkeyPatch) -> _InMemoryLoanStore:
    """Wire a single shared in-memory loan store + a loan DSN into all seams.

    The persistence side-records (workout / HITL) and the intake reconciliation
    both call ``get_loan_store(settings.loan_dsn)`` and read ``loan_tenant_default``
    lazily, so patching the source modules makes every call site share THIS
    store and a non-empty DSN (so it is not the offline NullLoanStore path).
    """
    store = _InMemoryLoanStore()
    import app.backend.portfolio.loan_store_postgres as store_mod
    import app.shared.settings as settings_mod

    monkeypatch.setattr(store_mod, "get_loan_store", lambda dsn: store)
    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(loan_dsn="postgresql://x", loan_tenant_default="t"),
    )
    return store


def test_workout_run_records_and_persists(wired_store: _InMemoryLoanStore) -> None:
    """An insolvent run records WORKOUT and persists it to the durable store."""
    app = build_graph().compile(checkpointer=MemorySaver())
    app.invoke(
        cast("SaiseiState", {"tdb_code": _INSOLVENT_TDB, "net_worth": -5_000_000}),
        config=_cfg("arc-run-1"),
    )
    persisted = wired_store.read("t", _LOAN_ID)
    assert persisted, "workout run must have persisted the loan log"
    assert current_status(persisted) is LoanStatus.WORKOUT


def test_reassessment_reconciles_from_persisted_ledger(
    wired_store: _InMemoryLoanStore,
) -> None:
    """A second assessment resumes from the persisted WORKOUT status.

    This is the cross-run reconciliation the arc exists for: run 1 moves the
    facility to WORKOUT and persists it; run 2 (a fresh thread / checkpointer)
    must attach the loan from the DURABLE ledger at intake, not re-seed a fresh
    PERFORMING chain.
    """
    # Run 1: drive to workout + persist.
    app1 = build_graph().compile(checkpointer=MemorySaver())
    app1.invoke(
        cast("SaiseiState", {"tdb_code": _INSOLVENT_TDB, "net_worth": -5_000_000}),
        config=_cfg("arc-run-1"),
    )
    assert current_status(wired_store.read("t", _LOAN_ID)) is LoanStatus.WORKOUT

    # Run 2: a brand-new checkpointer (no shared run state) -- only the durable
    # loan store carries history across the two runs.
    app2 = build_graph().compile(checkpointer=MemorySaver())
    app2.invoke(
        cast("SaiseiState", {"tdb_code": _INSOLVENT_TDB, "net_worth": -5_000_000}),
        config=_cfg("arc-run-2"),
    )
    values = app2.get_state(_cfg("arc-run-2")).values
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    # Intake resumed from the durable ledger: the log already contains WORKOUT
    # (it was not reset to a fresh PERFORMING bootstrap).
    assert any(e.status is LoanStatus.WORKOUT for e in events), (
        "run 2 must reconcile the persisted WORKOUT history at intake"
    )


def test_offline_without_store_is_noop() -> None:
    """Without the wired store (NullLoanStore), nothing persists -- byte-stable.

    No monkeypatch fixture here: the real factory returns NullLoanStore for the
    empty default DSN, so the run completes with no durable side-effect and the
    loan log is the in-run bootstrap-then-WORKOUT chain only.
    """
    app = build_graph().compile(checkpointer=MemorySaver())
    app.invoke(
        cast("SaiseiState", {"tdb_code": _INSOLVENT_TDB, "net_worth": -5_000_000}),
        config=_cfg("arc-offline"),
    )
    values = app.get_state(_cfg("arc-offline")).values
    # The in-run state still reflects the WORKOUT transition (checkpointer),
    # proving the offline path is unaffected by the absence of a durable store.
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    assert current_status(events) is LoanStatus.WORKOUT
