"""Verifier for the servicing UI entry (貸出管理 from the dashboard).

No CI here, so this pins the servicing entry's logic without a Reflex runtime:

- the pure graph-driver helper (``_run_servicing``) drives the REAL servicing
  graph offline (a shared MemorySaver patched into ``make_checkpointer``)
  against a pre-seeded in-memory loan store, exactly as the HTTP surface does:
  'confirm' advances 実行→正常, 'repay' closes 正常→完済;
- the ``servicing_loan_id_valid`` guard validates a non-empty facility id;
- ``open_servicing`` pre-fills the current run's attached facility id.

Fully offline. Mirrors tests/test_origination_ui_entry conventions.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import app.backend.portfolio.loan_store_postgres as loan_store_mod
import app.frontend.state as state_mod
import app.shared.settings as settings_mod
import pytest
from app.frontend.state import (
    SaiseiUIState,
    _origination_loan_status_kanji,
    _run_servicing,
)
from app.shared.models.loan import LoanEvent, LoanStatus
from langgraph.checkpoint.memory import MemorySaver

from tests._bare_state import bare_ui_state

_LOAN_ID = "L-test-facility"
_TENANT = "t"
_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)

_DISBURSED_CHAIN = (
    LoanStatus.APPLIED,
    LoanStatus.UNDER_REVIEW,
    LoanStatus.APPROVED,
    LoanStatus.DISBURSED,
)

#: Principal baseline stamped onto the DISBURSED event by the in-memory store so
#: the facility's outstanding balance is recoverable from the ledger alone and a
#: full 'repay' (完済) has a real balance to pay off.
_DISBURSED_PRINCIPAL = 100_000_000


def _fget(var: Any, inst: SaiseiUIState) -> Any:
    """Invoke a computed ``rx.var``'s runtime getter."""
    return var.fget(inst)


class _InMemoryLoanStore:
    """A real (in-memory) append-only loan store shared across graph opens."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], list[LoanEvent]] = {}

    def seed(self, loan_id: str, *statuses: LoanStatus) -> None:
        self._by_key[(_TENANT, loan_id)] = [
            LoanEvent(
                status=s,
                at=_AT + dt.timedelta(days=i),
                actor="system",
                # Stamp the principal baseline onto the DISBURSED (実行) event so
                # the facility's outstanding balance is recoverable from the
                # ledger ALONE (no external lender_stakes snapshot). A bare
                # 'repay' then has a real balance to pay off and can close to
                # 完済 -- matching the durable, self-contained ledger the HTTP
                # surface persists at disbursement.
                principal_disbursed=(_DISBURSED_PRINCIPAL if s is LoanStatus.DISBURSED else 0),
            )
            for i, s in enumerate(statuses)
        ]

    def append(self, tenant_id: str, loan_id: str, event: LoanEvent) -> None:
        self._by_key.setdefault((tenant_id, loan_id), []).append(event)

    def read(self, tenant_id: str, loan_id: str) -> list[LoanEvent]:
        return list(self._by_key.get((tenant_id, loan_id), []))


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[_InMemoryLoanStore]:
    """Shared MemorySaver + a pre-seeded in-memory loan store wired into all seams.

    ``_run_servicing`` opens ``make_checkpointer`` and the servicing graph reads
    / persists through ``get_loan_store(settings.loan_dsn)`` lazily, so patch
    both: one MemorySaver for the run, and one in-memory store (seeded to
    DISBURSED) plus a truthy loan DSN so it is not the offline NullLoanStore.
    """
    saver = MemorySaver()

    @contextmanager
    def _fake_make_checkpointer() -> Iterator[MemorySaver]:
        yield saver

    store = _InMemoryLoanStore()
    store.seed(_LOAN_ID, *_DISBURSED_CHAIN)

    monkeypatch.setattr(state_mod, "make_checkpointer", _fake_make_checkpointer)
    monkeypatch.setattr(loan_store_mod, "get_loan_store", lambda dsn: store)
    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(loan_dsn="postgresql://x", loan_tenant_default=_TENANT),
    )
    yield store


# --- loan-id validation ----------------------------------------------------


@pytest.mark.parametrize(
    ("loan_id", "ok"),
    [("L-1", True), ("  L-1  ", True), ("", False), ("   ", False)],
)
def test_servicing_loan_id_valid(loan_id: str, ok: bool) -> None:
    inst = bare_ui_state()
    inst.servicing_loan_id = loan_id.strip()
    assert _fget(SaiseiUIState.servicing_loan_id_valid, inst) is ok


# --- pure graph driver (real servicing graph, offline) ---------------------


def test_confirm_advances_disbursed_to_performing(
    wired: _InMemoryLoanStore,
) -> None:
    values = _run_servicing(_LOAN_ID, "confirm", "ui-svc-confirm")
    assert _origination_loan_status_kanji(values) == LoanStatus.PERFORMING.kanji
    # And the transition was persisted to the durable ledger.
    last = wired.read(_TENANT, _LOAN_ID)[-1]
    assert last.status is LoanStatus.PERFORMING


def test_repay_closes_a_performing_facility(wired: _InMemoryLoanStore) -> None:
    # Move to PERFORMING first, then repay -> CLOSED.
    _run_servicing(_LOAN_ID, "confirm", "ui-svc-a")
    values = _run_servicing(_LOAN_ID, "repay", "ui-svc-b")
    assert _origination_loan_status_kanji(values) == LoanStatus.CLOSED.kanji
    assert wired.read(_TENANT, _LOAN_ID)[-1].status is LoanStatus.CLOSED


def test_illegal_action_leaves_status_unchanged(
    wired: _InMemoryLoanStore,
) -> None:
    # 'repay' is not legal from DISBURSED (needs PERFORMING first): a no-op.
    values = _run_servicing(_LOAN_ID, "repay", "ui-svc-illegal")
    assert _origination_loan_status_kanji(values) == LoanStatus.DISBURSED.kanji


def test_partial_repayment_stays_performing(wired: _InMemoryLoanStore) -> None:
    # Confirm to PERFORMING, then a 一部入金 with a principal baseline: stays 正常.
    _run_servicing(_LOAN_ID, "confirm", "ui-svc-pa")
    values = _run_servicing(
        _LOAN_ID,
        "repay_amount",
        "ui-svc-pb",
        amount=30_000_000,
        lender_stakes={"main_bank": 100_000_000},
    )
    assert _origination_loan_status_kanji(values) == LoanStatus.PERFORMING.kanji
    repaid = sum(e.principal_repaid for e in wired.read(_TENANT, _LOAN_ID))
    assert repaid == 30_000_000


def test_full_repayment_closes(wired: _InMemoryLoanStore) -> None:
    _run_servicing(_LOAN_ID, "confirm", "ui-svc-fa")
    values = _run_servicing(
        _LOAN_ID,
        "repay",
        "ui-svc-fb",
        lender_stakes={"main_bank": 100_000_000},
    )
    assert _origination_loan_status_kanji(values) == LoanStatus.CLOSED.kanji


# --- open_servicing pre-fill ----------------------------------------------


def test_open_servicing_prefills_current_facility() -> None:
    inst = bare_ui_state()
    inst.loan_id_display = "L-9999999999999"
    SaiseiUIState.open_servicing.fn(inst)  # type: ignore[attr-defined]
    assert inst.servicing_loan_id == "L-9999999999999"
    assert inst.show_servicing is True


def test_open_servicing_does_not_overwrite_typed_id() -> None:
    inst = bare_ui_state()
    inst.loan_id_display = "L-current"
    inst.servicing_loan_id = "L-typed"
    SaiseiUIState.open_servicing.fn(inst)  # type: ignore[attr-defined]
    assert inst.servicing_loan_id == "L-typed"
