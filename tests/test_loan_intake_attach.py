"""Tests for the loan-lifecycle attachment at intake.

Verify that ``intake_node`` seeds the loan-lifecycle log for a borrower whose
credit report carries a truthful outstanding balance (sum of ``lender_stakes``),
and stays a backward-compatible no-op when it does not or when the caller has
already supplied a loan.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.financial_extraction import intake_node
from app.backend.state import SaiseiState
from app.shared.models.loan import LoanEvent, LoanStatus, current_status

# A syndicate fixture WITH lender_stakes (main 800M + sub 200M = 1B).
_TDB_WITH_STAKES = "4000001"
# A fixture WITHOUT lender_stakes.
_TDB_NO_STAKES = "2000001"

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def test_loan_attached_when_stakes_present() -> None:
    state = SaiseiState(tdb_code=_TDB_WITH_STAKES)
    out = intake_node(state)
    assert out["loan_id"] == "L-4000001000001"
    events = [LoanEvent.model_validate(e) for e in out["loan_events"]]
    assert current_status(events) is LoanStatus.PERFORMING
    assert events[0].status is LoanStatus.APPLIED
    assert all(e.actor == "system" for e in events)


def test_no_loan_attached_without_stakes() -> None:
    state = SaiseiState(tdb_code=_TDB_NO_STAKES)
    out = intake_node(state)
    assert "loan_id" not in out
    assert "loan_events" not in out


def test_caller_supplied_loan_is_not_overwritten() -> None:
    existing = [
        LoanEvent(status=LoanStatus.APPLIED, at=_AT, actor="caller").model_dump(mode="json")
    ]
    state = SaiseiState(
        tdb_code=_TDB_WITH_STAKES,
        loan_id="L-CALLER",
        loan_events=existing,
    )
    out = intake_node(state)
    # intake must not clobber a caller-provided loan.
    assert "loan_id" not in out
    assert "loan_events" not in out
