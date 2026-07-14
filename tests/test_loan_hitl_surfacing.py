"""Tests for the advisory loan summary in the HITL interrupt payload."""

from __future__ import annotations

import datetime as dt

from app.backend.agents.turnaround_orchestrator import _loan_summary
from app.backend.state import SaiseiState
from app.shared.models.classification import FsaClass
from app.shared.models.loan import LoanEvent, LoanStatus

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)


def _performing_log() -> list[dict[str, object]]:
    chain = (
        LoanStatus.APPLIED,
        LoanStatus.UNDER_REVIEW,
        LoanStatus.APPROVED,
        LoanStatus.DISBURSED,
        LoanStatus.PERFORMING,
    )
    return [
        LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system").model_dump(mode="json")
        for i, s in enumerate(chain)
    ]


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


def test_none_when_no_loan_attached() -> None:
    assert _loan_summary(_state()) is None


def test_summary_status_and_principal() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_performing_log(),
        lender_stakes={"main_bank": 800_000_000, "sub_bank": 200_000_000},
        fsa_classification=FsaClass.SEIJOSAKI,
    )
    out = _loan_summary(state)
    assert out is not None
    assert out["status"] == LoanStatus.PERFORMING.value
    assert out["status_kanji"] == "正常"
    assert out["outstanding_principal"] == 1_000_000_000
    # Normal class implies no transition.
    assert out["proposed_transition"] is None


def test_summary_proposes_transition_and_provision_for_distress() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_performing_log(),
        lender_stakes={"main_bank": 800_000_000, "sub_bank": 200_000_000},
        fsa_classification=FsaClass.HATAN_KENENSAKI,
    )
    out = _loan_summary(state)
    assert out is not None
    # 破綻懸念先 is requires_turnaround (not requires_workout), so from a
    # PERFORMING facility it proposes RESTRUCTURED (条件変更) -- the canonical
    # mapping pinned by test_loan.py::test_turnaround_classes_propose_restructured.
    assert out["proposed_transition"] == LoanStatus.RESTRUCTURED.value
    assert out["proposed_transition_kanji"] == "条件変更"
    # 破綻懸念先 provision rate 0.70 on 1,000,000,000 -> 700,000,000.
    assert out["provision_amount"] == 700_000_000
    assert out["provision_amount_formatted"] == "\u00a5700,000,000"


def test_summary_special_attention_uses_heavier_provision() -> None:
    base = _state(
        loan_id="L-1",
        loan_events=_performing_log(),
        lender_stakes={"main_bank": 100_000_000},
        fsa_classification=FsaClass.YOCHUISAKI,
    )
    base_out = _loan_summary(base)
    special = _state(
        loan_id="L-1",
        loan_events=_performing_log(),
        lender_stakes={"main_bank": 100_000_000},
        fsa_classification=FsaClass.YOCHUISAKI,
        special_attention=True,
    )
    special_out = _loan_summary(special)
    assert base_out is not None and special_out is not None
    assert special_out["provision_amount"] > base_out["provision_amount"]


def test_summary_no_provision_without_principal() -> None:
    state = _state(
        loan_id="L-1",
        loan_events=_performing_log(),
        fsa_classification=FsaClass.HATAN_KENENSAKI,
    )
    out = _loan_summary(state)
    assert out is not None
    assert out["outstanding_principal"] == 0
    assert out["provision_amount"] is None
