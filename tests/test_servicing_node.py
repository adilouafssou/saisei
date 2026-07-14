"""Tests for the deterministic loan-servicing node (実行→正常→一部入金→完済).

Cover the servicing node's non-distress moves and its structural guarantees:

- 'confirm' records DISBURSED → PERFORMING (実行→正常);
- 'repay_amount' records a partial paydown (一部入金) as a principal_repaid
  self-event; when it zeroes the balance it appends a → CLOSED (完済) event;
- 'repay' (full payoff) records the full remaining balance as a repayment then
  → CLOSED, so even the binary payoff is a truthful repayment of the real balance;
- the recorded events are classifier-authored ('system'), append-only;
- an unknown / missing action yields an errors entry (never a transition);
- an unattached loan / empty log / illegal-from-current is an append-only no-op;
- the node can NEVER record a gated credit / distress transition.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.servicing import (
    SERVICING_ACTIONS,
    _servicing_events,
    servicing_node,
)
from app.backend.state import SaiseiState
from app.shared.models.loan import (
    HITL_GATED_TRANSITIONS,
    LoanEvent,
    LoanStatus,
    current_status,
    outstanding_principal_for_state,
)

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)

_DISBURSED_CHAIN = (
    LoanStatus.APPLIED,
    LoanStatus.UNDER_REVIEW,
    LoanStatus.APPROVED,
    LoanStatus.DISBURSED,
)
_PERFORMING_CHAIN = (*_DISBURSED_CHAIN, LoanStatus.PERFORMING)

#: A facility carrying a real principal baseline (intake sets principal =
#: sum of lender_stakes), so a repayment has a balance to draw down.
_STAKES = {"main_bank": 100_000_000}


def _events(*statuses: LoanStatus) -> list[dict[str, object]]:
    return [
        LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system").model_dump(mode="json")
        for i, s in enumerate(statuses)
    ]


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


def _merged_outstanding(state: SaiseiState, events: list[dict[str, object]]) -> int:
    """Outstanding after merging the node's just-recorded events into state."""
    merged = state.model_copy(update={"loan_events": [*state.loan_events, *events]})
    return outstanding_principal_for_state(merged)


# ---------------------------------------------------------------------------
# confirm: 実行 → 正常
# ---------------------------------------------------------------------------


def test_confirm_records_disbursed_to_performing() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_DISBURSED_CHAIN),
        servicing_action="confirm",
    )
    out = _servicing_events(state, "confirm")
    assert len(out) == 1
    assert out[0]["status"] == LoanStatus.PERFORMING.value
    assert out[0]["actor"] == "system"


def test_confirm_records_regardless_of_outstanding() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_DISBURSED_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="confirm",
    )
    out = _servicing_events(state, "confirm")
    assert len(out) == 1
    assert out[0]["status"] == LoanStatus.PERFORMING.value


# ---------------------------------------------------------------------------
# repay_amount: 一部入金 (partial paydown)
# ---------------------------------------------------------------------------


def test_partial_repayment_lowers_balance_without_closing() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay_amount",
        servicing_amount=30_000_000,
    )
    out = _servicing_events(state, "repay_amount")
    assert len(out) == 1  # a single repayment self-event, no close
    assert out[0]["status"] == LoanStatus.PERFORMING.value
    assert out[0]["principal_repaid"] == 30_000_000
    assert _merged_outstanding(state, out) == 70_000_000


def test_partial_repayment_to_zero_appends_close() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay_amount",
        servicing_amount=100_000_000,
    )
    out = _servicing_events(state, "repay_amount")
    assert len(out) == 2  # repayment self-event + the 完済 close
    assert out[0]["status"] == LoanStatus.PERFORMING.value
    assert out[0]["principal_repaid"] == 100_000_000
    assert out[1]["status"] == LoanStatus.CLOSED.value
    assert _merged_outstanding(state, out) == 0


def test_partial_repayment_is_capped_at_outstanding() -> None:
    # Over-payment is clamped to the balance: it closes, never over-repays.
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay_amount",
        servicing_amount=999_000_000,
    )
    out = _servicing_events(state, "repay_amount")
    assert out[0]["principal_repaid"] == 100_000_000  # capped at the balance
    assert out[-1]["status"] == LoanStatus.CLOSED.value


def test_partial_repayment_from_restructured() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN, LoanStatus.RESTRUCTURED),
        lender_stakes=_STAKES,
        servicing_action="repay_amount",
        servicing_amount=25_000_000,
    )
    out = _servicing_events(state, "repay_amount")
    assert out[0]["status"] == LoanStatus.RESTRUCTURED.value
    assert out[0]["principal_repaid"] == 25_000_000
    assert _merged_outstanding(state, out) == 75_000_000


def test_zero_amount_repay_is_noop() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay_amount",
        servicing_amount=0,
    )
    assert _servicing_events(state, "repay_amount") == []


# ---------------------------------------------------------------------------
# repay: 完済 (full payoff, recorded truthfully)
# ---------------------------------------------------------------------------


def test_repay_records_full_balance_then_closes() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay",
    )
    out = _servicing_events(state, "repay")
    # Truthful: a repayment of the full balance, then the close.
    assert out[0]["status"] == LoanStatus.PERFORMING.value
    assert out[0]["principal_repaid"] == 100_000_000
    assert out[-1]["status"] == LoanStatus.CLOSED.value
    assert _merged_outstanding(state, out) == 0


def test_repay_from_restructured_closes() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN, LoanStatus.RESTRUCTURED),
        lender_stakes=_STAKES,
        servicing_action="repay",
    )
    out = _servicing_events(state, "repay")
    assert out[-1]["status"] == LoanStatus.CLOSED.value


def test_repay_without_balance_is_noop() -> None:
    # No stake baseline -> no balance to repay -> nothing recorded.
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        servicing_action="repay",
    )
    assert _servicing_events(state, "repay") == []


# ---------------------------------------------------------------------------
# no-ops + guards
# ---------------------------------------------------------------------------


def test_no_loan_attached_is_noop() -> None:
    assert _servicing_events(_state(servicing_action="confirm"), "confirm") == []


def test_empty_log_is_noop() -> None:
    state = _state(loan_id="L-1", servicing_action="confirm")
    assert _servicing_events(state, "confirm") == []


def test_confirm_illegal_from_performing_is_noop() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        servicing_action="confirm",
    )
    assert _servicing_events(state, "confirm") == []


def test_repay_illegal_from_disbursed_is_noop() -> None:
    # Not yet PERFORMING: a repayment self-loop is not legal from DISBURSED.
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_DISBURSED_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay",
    )
    assert _servicing_events(state, "repay") == []


def test_repay_from_closed_terminal_is_noop() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN, LoanStatus.CLOSED),
        lender_stakes=_STAKES,
        servicing_action="repay",
    )
    assert _servicing_events(state, "repay") == []


# ---------------------------------------------------------------------------
# node-level behaviour
# ---------------------------------------------------------------------------


def test_node_confirm_appends_transition_to_return() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_DISBURSED_CHAIN),
        servicing_action="confirm",
    )
    out = servicing_node(state)
    assert len(out["loan_events"]) == 1
    assert out["loan_events"][0]["status"] == LoanStatus.PERFORMING.value


def test_node_partial_repay_appends_self_event() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay_amount",
        servicing_amount=40_000_000,
    )
    out = servicing_node(state)
    assert out["loan_events"][0]["principal_repaid"] == 40_000_000
    assert (
        current_status([LoanEvent.model_validate(e) for e in out["loan_events"]])
        is LoanStatus.PERFORMING
    )


def test_node_repay_appends_close_to_return() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_PERFORMING_CHAIN),
        lender_stakes=_STAKES,
        servicing_action="repay",
    )
    out = servicing_node(state)
    assert out["loan_events"][-1]["status"] == LoanStatus.CLOSED.value


def test_node_unknown_action_records_error_not_transition() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_events(*_DISBURSED_CHAIN),
        servicing_action="refinance",
    )
    out = servicing_node(state)
    assert "loan_events" not in out
    assert any("Invalid servicing action" in e for e in out["errors"])


def test_node_missing_action_records_error() -> None:
    state = _state(loan_id="L-1", loan_events=_events(*_DISBURSED_CHAIN))
    out = servicing_node(state)
    assert any("Invalid servicing action" in e for e in out["errors"])


def test_servicing_actions_set() -> None:
    assert frozenset({"confirm", "repay", "repay_amount"}) == SERVICING_ACTIONS


def test_node_never_records_a_gated_transition() -> None:
    # Drive every action from every reachable status; assert no recorded
    # transition is ever a HITL-gated credit / distress move.
    chains = {
        LoanStatus.DISBURSED: _DISBURSED_CHAIN,
        LoanStatus.PERFORMING: _PERFORMING_CHAIN,
        LoanStatus.RESTRUCTURED: (*_PERFORMING_CHAIN, LoanStatus.RESTRUCTURED),
    }
    for current, chain in chains.items():
        prior = chain[-1]
        assert prior is current
        for action in SERVICING_ACTIONS:
            state = _state(
                loan_id="L-1",
                loan_events=_events(*chain),
                lender_stakes=_STAKES,
                servicing_amount=10_000_000,
            )
            out = _servicing_events(state, action)
            running = current
            for ev in out:
                target = LoanStatus(ev["status"])
                assert (running, target) not in HITL_GATED_TRANSITIONS
                running = target
