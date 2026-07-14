"""End-to-end proof that origination and assessment are ONE loan lifecycle.

The product's core innovation claim is that a new facility and its later
turnaround assessment are not two disconnected workflows but a single,
continuous, auditable loan record. This test proves it across the TWO graphs on
ONE durable ledger, fully offline:

    origination graph (融資組成)            assessment graph (再生)
    申込 → 審査中 → 承認 → 実行   ......▶   intake resumes the SAME facility
    (APPLIED..DISBURSED, persisted)         from the durable ledger (not re-seeded)

A creditworthy applicant is originated through the compiled origination graph
with a banker APPROVE, driving the facility to DISBURSED and persisting every
transition to a shared in-memory loan store. A SECOND, independent run -- a fresh
turnaround assessment of the SAME borrower on a brand-new checkpointer -- must
then attach that exact facility from the durable ledger at intake, seeing the
originated DISBURSED history rather than re-seeding a fresh PERFORMING bootstrap.

The two graphs are wired to the same store through the one
``get_loan_store(settings.loan_dsn)`` seam both the origination persist side and
the assessment intake read side call lazily, so patching the source modules
makes every call site share THIS store (mirrors tests/test_loan_lifecycle_e2e).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from app.backend.graph import build_graph
from app.backend.graph_origination import compile_origination_graph
from app.backend.state import SaiseiState
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

# A healthy applicant (normal_service_co): TDB score 75 (>= the approve floor),
# anti-social clear -> the 稟議 recommendation is approve; classifies 正常先 on the
# assessment side so the second run routes straight to END (monitor-only).
_TDB = "2000001"
_LOAN_ID = "L-2000001000001"  # L-<hojin_bango> (the shared lifecycle key)
_TENANT = "t"


class _InMemoryLoanStore:
    """A real (in-memory) append-only loan store shared across both graph runs."""

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

    Both the origination persist side and the assessment intake read side call
    ``get_loan_store(settings.loan_dsn)`` and read ``loan_tenant_default``
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
        lambda: SimpleNamespace(loan_dsn="postgresql://x", loan_tenant_default=_TENANT),
    )
    return store


def _originate_to_disbursed(thread_id: str) -> None:
    """Drive the origination graph 申込 -> 実行 with a banker APPROVE."""
    app = compile_origination_graph(checkpointer=MemorySaver())
    app.invoke(cast("SaiseiState", {"tdb_code": _TDB}), config=_cfg(thread_id))
    # Banker approves at the 稟議 interrupt -> APPROVED -> DISBURSED.
    app.invoke(Command(resume={"decision": "approve"}), config=_cfg(thread_id))


def test_origination_persists_the_full_applied_to_disbursed_arc(
    wired_store: _InMemoryLoanStore,
) -> None:
    """Originating an approved facility persists APPLIED..DISBURSED durably."""
    _originate_to_disbursed("orig-arc")
    persisted = wired_store.read(_TENANT, _LOAN_ID)
    statuses = [e.status for e in persisted]
    assert statuses == [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
    ]
    assert current_status(persisted) is LoanStatus.DISBURSED


def test_disbursed_event_stamps_the_principal_baseline(
    wired_store: _InMemoryLoanStore,
) -> None:
    """The DISBURSED event carries principal_disbursed, so the balance is
    recoverable from the ledger alone."""
    _originate_to_disbursed("orig-stamp")
    persisted = wired_store.read(_TENANT, _LOAN_ID)
    disbursed = next(e for e in persisted if e.status is LoanStatus.DISBURSED)
    assert disbursed.principal_disbursed > 0


def test_servicing_repays_by_loan_id_alone(
    wired_store: _InMemoryLoanStore,
) -> None:
    """The ledger-principal payoff: a servicing run keyed ONLY by loan_id (no
    lender_stakes) can draw down the balance, because the DISBURSED event stamped
    it. Confirm to PERFORMING, then a partial repayment lowers the balance."""
    from app.backend.graph_servicing import compile_servicing_graph
    from app.shared.models.loan import outstanding_principal_for_state

    _originate_to_disbursed("orig-svc")
    baseline = next(
        e.principal_disbursed
        for e in wired_store.read(_TENANT, _LOAN_ID)
        if e.status is LoanStatus.DISBURSED
    )
    assert baseline > 0

    svc = compile_servicing_graph(checkpointer=MemorySaver())
    # confirm -> PERFORMING (loan_id only).
    svc.invoke(
        cast("SaiseiState", {"loan_id": _LOAN_ID, "servicing_action": "confirm"}),
        config=_cfg("svc-confirm"),
    )
    # repay_amount -> a 一部入金, balance drawn down WITHOUT any lender_stakes.
    svc.invoke(
        cast(
            "SaiseiState",
            {
                "loan_id": _LOAN_ID,
                "servicing_action": "repay_amount",
                "servicing_amount": baseline // 4,
            },
        ),
        config=_cfg("svc-repay"),
    )
    persisted = wired_store.read(_TENANT, _LOAN_ID)
    assert current_status(persisted) is LoanStatus.PERFORMING
    repaid = sum(e.principal_repaid for e in persisted)
    assert repaid == baseline // 4
    # The seam derives the declining balance from the ledger alone (no stakes).
    state = SimpleNamespace(
        lender_stakes={},
        loan_events=[e.model_dump(mode="json") for e in persisted],
    )
    assert outstanding_principal_for_state(state) == baseline - baseline // 4


def test_assessment_resumes_the_originated_facility(
    wired_store: _InMemoryLoanStore,
) -> None:
    """The SAME facility originated by the breadth graph is resumed by depth.

    The cross-graph reconciliation the lifecycle spine exists for: origination
    drives the facility to DISBURSED and persists it; a fresh turnaround
    assessment of the same borrower (new graph, new checkpointer -- only the
    durable ledger carries history across the two) must attach THAT facility at
    intake from the durable ledger, seeing the originated DISBURSED history
    instead of re-seeding a fresh PERFORMING bootstrap.
    """
    # Phase 1 -- originate to DISBURSED (breadth graph).
    _originate_to_disbursed("orig-arc")
    assert current_status(wired_store.read(_TENANT, _LOAN_ID)) is LoanStatus.DISBURSED

    # Phase 2 -- assess the same borrower (depth graph, brand-new checkpointer).
    assess = build_graph().compile(checkpointer=MemorySaver())
    assess.invoke(cast("SaiseiState", {"tdb_code": _TDB}), config=_cfg("assess"))
    values = assess.get_state(_cfg("assess")).values

    # Same facility id -- the originated record, not a new one.
    assert values["loan_id"] == _LOAN_ID
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    statuses = [e.status for e in events]
    # Intake resumed from the durable ledger: the originated arc is present and
    # the chain was NOT reset to a fresh APPLIED->...->PERFORMING bootstrap.
    assert LoanStatus.DISBURSED in statuses
    assert statuses[:4] == [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
    ]
    # A healthy borrower assessment is monitor-only (正常先 -> END): it does not
    # fabricate a distress transition on the freshly-disbursed facility, so the
    # resumed status is unchanged.
    assert current_status(events) is LoanStatus.DISBURSED


def test_offline_without_store_does_not_connect_the_graphs() -> None:
    """Without a wired store (NullLoanStore), the loop is a no-op -- byte-stable.

    No monkeypatch fixture: the real factory returns NullLoanStore for the empty
    default DSN, so origination persists nothing durably and a later assessment
    cannot resume the originated facility -- it falls back to its own intake
    bootstrap. This pins that the cross-graph loop is strictly opt-in (requires
    SAISEI_LOAN_DSN) and the offline default is unchanged.
    """
    orig = compile_origination_graph(checkpointer=MemorySaver())
    orig.invoke(cast("SaiseiState", {"tdb_code": _TDB}), config=_cfg("o"))
    orig.invoke(Command(resume={"decision": "approve"}), config=_cfg("o"))
    # In-run state still reflects DISBURSED (checkpointer), but nothing durable.
    orig_values = orig.get_state(_cfg("o")).values
    orig_events = [LoanEvent.model_validate(e) for e in orig_values["loan_events"]]
    assert current_status(orig_events) is LoanStatus.DISBURSED

    # A fresh assessment cannot see the originated facility (no durable ledger);
    # it bootstraps its own monitoring chain for an existing performing facility.
    assess = build_graph().compile(checkpointer=MemorySaver())
    assess.invoke(cast("SaiseiState", {"tdb_code": _TDB}), config=_cfg("a"))
    assess_values = assess.get_state(_cfg("a")).values
    assess_events = [LoanEvent.model_validate(e) for e in assess_values.get("loan_events", [])]
    # The assessment's loan log (if any) is its OWN bootstrap, never the
    # originated APPROVED->DISBURSED credit arc carried across graphs.
    assert LoanStatus.APPROVED not in [e.status for e in assess_events]
