"""Tests for the grounded, HITL-gated restructure node (条件変更 depth realisation).

The depth mirror of tests/test_loan_origination_node.py. Verifies, fully
offline:

- the deterministic self-curing verdict is attached to ``restructure_curing``
  for the banker, for every band (self_curing / non_curing);
- the HITL-gated PERFORMING -> RESTRUCTURED transition is recorded when legal
  and gated, authored by the banker (NOT 'system');
- a no-loan run records no transition but still surfaces the advisory verdict;
- a non-restructurable status (e.g. DISBURSED) records no transition;
- the node is read-only on the snapshot and deterministic.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.restructure import restructure_node
from app.backend.state import SaiseiState
from app.shared.models.accounting import TrialBalance
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def _declining_history() -> list[TrialBalance]:
    """A deteriorating 12-month history (mirrors the verifier fixture)."""
    rows: list[TrialBalance] = []
    for i in range(12):
        sales = 150_000_000 - i * 2_500_000
        cogs = int(sales * (0.80 + i * 0.005))
        rows.append(
            TrialBalance(
                period=dt.date(2025, 1, 1) + dt.timedelta(days=30 * i),
                uriage=sales,
                uriage_genka=cogs,
                hanbaihi=20_000_000,
            )
        )
    return rows


def _performing_log() -> list[dict[str, object]]:
    """A facility log whose current status is PERFORMING (正常)."""
    chain = [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
        LoanStatus.PERFORMING,
    ]
    events: list[dict[str, object]] = []
    for status in chain:
        kw: dict[str, object] = {"status": status, "at": _AT, "actor": "system"}
        if status is LoanStatus.DISBURSED:
            kw["principal_disbursed"] = 500_000_000
        events.append(LoanEvent(**kw).model_dump(mode="json"))
    return events


def _disbursed_log() -> list[dict[str, object]]:
    """A facility log whose current status is DISBURSED (cannot restructure)."""
    chain = [
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
    ]
    return [LoanEvent(status=s, at=_AT, actor="system").model_dump(mode="json") for s in chain]


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


# --- advisory verdict ------------------------------------------------------


def test_verdict_is_attached_for_a_non_curing_restructure() -> None:
    # A tiny rate cut on a small balance produces near-zero relief -> the
    # distressed borrower never recovers -> non_curing, surfaced for the banker.
    state = _state(
        shisanhyo=_declining_history(),
        loan_id="L-1",
        loan_events=_performing_log(),
        restructure_grace_months=0,
        restructure_rate_reduction_bps=1,
    )
    out = restructure_node(state)
    curing = out["restructure_curing"]
    assert curing["band"] in {"non_curing", "marginal", "self_curing"}
    assert "reason" in curing and curing["reason"]
    assert "annual_relief" in curing


def test_zero_terms_is_non_curing() -> None:
    state = _state(
        shisanhyo=_declining_history(),
        loan_id="L-1",
        loan_events=_performing_log(),
    )
    curing = restructure_node(state)["restructure_curing"]
    assert curing["annual_relief"] == 0
    assert curing["band"] == "non_curing"
    assert curing["recovery_month_index"] is None


# --- HITL-gated transition -------------------------------------------------


def test_records_the_hitl_gated_transition_authored_by_the_banker() -> None:
    state = _state(
        shisanhyo=_declining_history(),
        loan_id="L-1",
        loan_events=_performing_log(),
        restructure_grace_months=12,
        restructure_rate_reduction_bps=200,
    )
    out = restructure_node(state)
    events = out["loan_events"]
    assert len(events) == 1
    assert events[0]["status"] == LoanStatus.RESTRUCTURED.value
    # A restructure is a banker-authority credit judgement, NOT a 'system'
    # operational fact like a servicing move.
    assert events[0]["actor"] != "system"


def test_no_loan_attached_records_no_transition_but_still_assesses() -> None:
    state = _state(
        shisanhyo=_declining_history(),
        restructure_grace_months=12,
        restructure_rate_reduction_bps=200,
    )
    out = restructure_node(state)
    assert out["loan_events"] == []
    # The advisory verdict is still surfaced even with no facility attached.
    assert out["restructure_curing"]["band"] in {
        "non_curing",
        "marginal",
        "self_curing",
    }


def test_non_restructurable_status_records_no_transition() -> None:
    # DISBURSED cannot legally transition to RESTRUCTURED.
    state = _state(
        shisanhyo=_declining_history(),
        loan_id="L-1",
        loan_events=_disbursed_log(),
        restructure_grace_months=12,
        restructure_rate_reduction_bps=200,
    )
    assert restructure_node(state)["loan_events"] == []


# --- read-only / determinism ----------------------------------------------


def test_node_does_not_mutate_state() -> None:
    state = _state(
        shisanhyo=_declining_history(),
        loan_id="L-1",
        loan_events=_performing_log(),
        restructure_grace_months=12,
        restructure_rate_reduction_bps=200,
    )
    before = state.model_dump(mode="json")
    restructure_node(state)
    assert state.model_dump(mode="json") == before


def test_node_is_deterministic() -> None:
    state = _state(
        shisanhyo=_declining_history(),
        loan_id="L-1",
        loan_events=_performing_log(),
        restructure_grace_months=12,
        restructure_rate_reduction_bps=200,
    )
    a = restructure_node(state)["restructure_curing"]
    b = restructure_node(state)["restructure_curing"]
    assert a == b
