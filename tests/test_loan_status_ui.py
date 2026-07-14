"""Verifier for the loan-lifecycle case-file display fields.

No CI here, so this pins ``SaiseiUIState._apply_loan_summary``: it must derive
the current loan status (kanji + english) from the append-only ``loan_events``
log and the deterministic loan-loss provision (貸倒引当金) from outstanding
principal (sum of ``lender_stakes``) and the FSA class -- the figures the
terminal workout path persists but never surfaces via a HITL interrupt payload.

Runs on a bare state instance with no Reflex runtime (see tests/_bare_state).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.shared.models.classification import FsaClass
from app.shared.models.loan import LoanEvent, LoanStatus
from app.shared.models.money import format_jpy

from tests._bare_state import bare_ui_state

_AT = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)

_PERFORMING_CHAIN = (
    LoanStatus.APPLIED,
    LoanStatus.UNDER_REVIEW,
    LoanStatus.APPROVED,
    LoanStatus.DISBURSED,
    LoanStatus.PERFORMING,
)


def _events(*statuses: LoanStatus) -> list[dict[str, Any]]:
    return [
        LoanEvent(status=s, at=_AT + dt.timedelta(days=i), actor="system").model_dump(mode="json")
        for i, s in enumerate(statuses)
    ]


def test_no_loan_clears_fields() -> None:
    inst = bare_ui_state()
    inst._apply_loan_summary({})
    assert inst.loan_status_kanji == ""
    assert inst.loan_status_english == ""
    assert inst.loan_provision_display == "—"


def test_performing_status_and_light_provision() -> None:
    inst = bare_ui_state()
    inst._apply_loan_summary(
        {
            "loan_events": _events(*_PERFORMING_CHAIN),
            "lender_stakes": {"main_bank": 800_000_000, "sub_bank": 200_000_000},
            "fsa_classification": FsaClass.SEIJOSAKI,
        }
    )
    assert inst.loan_status_kanji == "正常"
    assert inst.loan_status_english == "Performing"
    # 正常先 reserve ratio 0.002 on 1,000,000,000 -> 2,000,000.
    assert inst.loan_provision_display == format_jpy(2_000_000)


def test_workout_status_and_full_provision() -> None:
    # The status the terminal workout path records, with a bankrupt FSA class:
    # provision is the full outstanding balance (reserve ratio 1.0).
    inst = bare_ui_state()
    inst._apply_loan_summary(
        {
            "loan_events": _events(*_PERFORMING_CHAIN, LoanStatus.WORKOUT),
            "lender_stakes": {"main_bank": 550_000_000, "sub_bank": 450_000_000},
            "fsa_classification": FsaClass.HATANSAKI,
        }
    )
    assert inst.loan_status_kanji == "管理回収"
    assert inst.loan_status_english == "Workout"
    assert inst.loan_provision_display == format_jpy(1_000_000_000)


def test_status_derived_with_string_fsa_value() -> None:
    # Rehydrated snapshots may carry the FSA class as a plain string.
    inst = bare_ui_state()
    inst._apply_loan_summary(
        {
            "loan_events": _events(*_PERFORMING_CHAIN, LoanStatus.WORKOUT),
            "lender_stakes": {"main_bank": 300_000_000},
            "fsa_classification": FsaClass.JISSHITSU_HATANSAKI.value,
        }
    )
    assert inst.loan_status_kanji == "管理回収"
    assert inst.loan_provision_display == format_jpy(300_000_000)


def test_provision_dash_without_stakes() -> None:
    inst = bare_ui_state()
    inst._apply_loan_summary(
        {
            "loan_events": _events(*_PERFORMING_CHAIN),
            "fsa_classification": FsaClass.HATANSAKI,
        }
    )
    # Status still derives, but with no outstanding balance the provision is —.
    assert inst.loan_status_kanji == "正常"
    assert inst.loan_provision_display == "—"


def test_malformed_loan_events_clears_fields() -> None:
    inst = bare_ui_state()
    inst._apply_loan_summary({"loan_events": [{"not": "a valid event"}]})
    assert inst.loan_status_kanji == ""
    assert inst.loan_provision_display == "—"
