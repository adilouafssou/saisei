"""Tests for the loan-servicing graph (START → servicing → END).

The servicing graph is a single-node, non-pausing StateGraph: it records the
deterministic transition implied by ``servicing_action`` and completes. These
tests drive the REAL compiled graph offline (a shared MemorySaver) and assert:

- 'confirm' on a DISBURSED facility advances it to PERFORMING (実行→正常);
- 'repay' on a PERFORMING facility closes it (正常→完済);
- the graph never pauses (state.next is empty after a run);
- an illegal-from-current action is an append-only no-op (ledger unchanged);
- an unknown action records an error and no transition.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from app.backend.graph_servicing import compile_servicing_graph
from app.backend.state import SaiseiState
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)

_DISBURSED_CHAIN = (
    LoanStatus.APPLIED,
    LoanStatus.UNDER_REVIEW,
    LoanStatus.APPROVED,
    LoanStatus.DISBURSED,
)
_PERFORMING_CHAIN = (*_DISBURSED_CHAIN, LoanStatus.PERFORMING)


def _events(*statuses: LoanStatus) -> list[dict[str, object]]:
    return [
        LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system").model_dump(mode="json")
        for i, s in enumerate(statuses)
    ]


def _cfg(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _invoke(thread_id: str, **state: object) -> dict[str, Any]:
    app = compile_servicing_graph(checkpointer=MemorySaver())
    base: dict[str, object] = {"tdb_code": "1234567", "loan_id": "L-1"}
    base.update(state)
    app.invoke(cast("SaiseiState", base), config=_cfg(thread_id))
    return dict(app.get_state(_cfg(thread_id)).values)


def test_confirm_advances_disbursed_to_performing() -> None:
    values = _invoke(
        "svc-confirm",
        loan_events=_events(*_DISBURSED_CHAIN),
        servicing_action="confirm",
    )
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    assert current_status(events) is LoanStatus.PERFORMING


def test_repay_closes_a_performing_facility() -> None:
    values = _invoke(
        "svc-repay",
        loan_events=_events(*_PERFORMING_CHAIN),
        # A repayment draws down a principal baseline; supply the facility's
        # outstanding (as intake does from lender_stakes) so 'repay' has a real
        # balance to pay off -- without it a repayment is a no-op (see
        # test_servicing_node.test_repay_without_balance_is_noop).
        lender_stakes={"main_bank": 100_000_000},
        servicing_action="repay",
    )
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    assert current_status(events) is LoanStatus.CLOSED


def test_graph_never_pauses() -> None:
    app = compile_servicing_graph(checkpointer=MemorySaver())
    cfg = _cfg("svc-no-pause")
    app.invoke(
        cast(
            "SaiseiState",
            {
                "tdb_code": "1234567",
                "loan_id": "L-1",
                "loan_events": _events(*_DISBURSED_CHAIN),
                "servicing_action": "confirm",
            },
        ),
        config=cfg,
    )
    # A non-pausing graph: there is no next node awaiting after completion.
    assert not app.get_state(cfg).next


def test_illegal_action_is_a_noop_ledger_unchanged() -> None:
    # 'repay' is not legal from DISBURSED (正常→完済 needs PERFORMING first).
    before = _events(*_DISBURSED_CHAIN)
    values = _invoke(
        "svc-illegal",
        loan_events=before,
        servicing_action="repay",
    )
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    assert current_status(events) is LoanStatus.DISBURSED
    assert len(events) == len(before)


def test_unknown_action_records_error_no_transition() -> None:
    values = _invoke(
        "svc-unknown",
        loan_events=_events(*_DISBURSED_CHAIN),
        servicing_action="refinance",
    )
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    assert current_status(events) is LoanStatus.DISBURSED
    assert any("Invalid servicing action" in e for e in values["errors"])
