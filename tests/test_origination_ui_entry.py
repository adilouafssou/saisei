"""Verifier for the origination UI entry (融資組成 from the dashboard).

No CI here, so this pins the origination entry's logic without a Reflex runtime:

- the pure graph-driver helpers (``_run_origination_to_pause`` /
  ``_resume_origination``) drive the REAL origination graph offline (a shared
  MemorySaver patched into ``make_checkpointer``) to the 稟議 pause and through
  the banker's approve / decline, exactly as the HTTP surface does;
- the pure display helpers map a snapshot to the recommendation view + loan
  status kanji;
- ``_apply_origination_snapshot`` populates the display fields on a bare state;
- the ``origination_code_valid`` guard validates the 7-digit code.

Fully offline (MemorySaver; no DSNs). Mirrors tests/test_loan_origination_graph
+ tests/test_portfolio_watchlist conventions.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import app.frontend.state as state_mod
import pytest
from app.frontend.state import (
    SaiseiUIState,
    _origination_code_valid,
    _origination_loan_status_kanji,
    _origination_recommendation_view,
    _resume_origination,
    _run_origination_to_pause,
)
from app.shared.models.loan import LoanEvent, LoanStatus
from langgraph.checkpoint.memory import MemorySaver

from tests._bare_state import bare_ui_state

# A creditworthy applicant (normal_service_co): score 75 >= approve floor.
_TDB = "2000001"

#: ``_apply_origination_snapshot`` is a ``SaiseiUIState`` method; bind the
#: unbound function so the existing call sites (which pass ``inst`` explicitly)
#: stay type-correct without a module-level import.
_apply_origination_snapshot = SaiseiUIState._apply_origination_snapshot


def _fget(var: Any, inst: SaiseiUIState) -> Any:
    """Invoke a computed ``rx.var``'s runtime getter."""
    return var.fget(inst)


@pytest.fixture
def shared_memory_saver(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemorySaver]:
    """Patch ``make_checkpointer`` to yield ONE shared MemorySaver per test.

    Both helpers open ``make_checkpointer`` independently; pausing in one and
    resuming in another requires they share the SAME in-memory store, so this
    yields a single saver via a contextmanager shim. Fully offline (no DSN).
    """
    from contextlib import contextmanager

    saver = MemorySaver()

    @contextmanager
    def _fake_make_checkpointer() -> Iterator[MemorySaver]:
        yield saver

    monkeypatch.setattr(state_mod, "make_checkpointer", _fake_make_checkpointer)
    yield saver


# --- code validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "ok"),
    [
        ("2000001", True),
        ("123", False),
        ("12345678", False),
        ("abcdefg", False),
        ("", False),
        ("123456a", False),
    ],
)
def test_origination_code_valid(code: str, ok: bool) -> None:
    assert _origination_code_valid(code) is ok
    inst = bare_ui_state()
    inst.origination_code = code
    assert _fget(SaiseiUIState.origination_code_valid, inst) is ok


# --- display helpers -------------------------------------------------------


def test_recommendation_view_formats_an_approve() -> None:
    values = {
        "origination_recommendation": {
            "recommendation": "approve",
            "reason": "TDBスコア 75 ≥ 60 [tdb_score]",
            "grounded": True,
            "max_facility_amount": 600_000_000,
        }
    }
    view = _origination_recommendation_view(values)
    assert view["recommendation"] == "approve"
    assert view["grounded"] == "yes"
    assert view["max_facility"] != "—"  # a positive ceiling is formatted


def test_recommendation_view_decline_has_dash_ceiling() -> None:
    values = {
        "origination_recommendation": {
            "recommendation": "decline",
            "reason": "TDBスコア 41 < 60 [tdb_score]",
            "grounded": True,
            "max_facility_amount": 0,
        }
    }
    view = _origination_recommendation_view(values)
    assert view["recommendation"] == "decline"
    assert view["max_facility"] == "—"


def test_recommendation_view_empty_without_recommendation() -> None:
    view = _origination_recommendation_view({})
    assert view["recommendation"] == ""
    assert view["max_facility"] == "—"


def _log(*statuses: LoanStatus) -> list[dict[str, Any]]:
    import datetime as dt

    at = dt.datetime(2025, 4, 1, tzinfo=dt.UTC)
    return [LoanEvent(status=s, at=at, actor="system").model_dump(mode="json") for s in statuses]


def test_loan_status_kanji_from_snapshot() -> None:
    values = {"loan_events": _log(LoanStatus.APPLIED, LoanStatus.UNDER_REVIEW)}
    assert _origination_loan_status_kanji(values) == LoanStatus.UNDER_REVIEW.kanji


def test_loan_status_kanji_empty_and_malformed() -> None:
    assert _origination_loan_status_kanji({}) == ""
    assert _origination_loan_status_kanji({"loan_events": [{"x": 1}]}) == ""


# --- snapshot applier ------------------------------------------------------


def test_apply_origination_snapshot_populates_fields() -> None:
    inst = bare_ui_state()
    inst.origination_code = _TDB
    values = {
        "origination_recommendation": {
            "recommendation": "approve",
            "reason": "grounded reason [tdb_score]",
            "grounded": True,
            "max_facility_amount": 600_000_000,
        },
        "loan_events": _log(LoanStatus.APPLIED, LoanStatus.UNDER_REVIEW),
        "company_profile": {"name": "東京サービス株式会社"},
    }
    _apply_origination_snapshot(inst, values)
    assert inst.origination_recommendation == "approve"
    assert inst.origination_grounded == "yes"
    assert inst.origination_loan_status == LoanStatus.UNDER_REVIEW.kanji
    assert inst.origination_company == "東京サービス株式会社"


# --- pure graph drivers (real origination graph, offline) ------------------


def test_run_to_pause_surfaces_recommendation_and_under_review(
    shared_memory_saver: MemorySaver,
) -> None:
    """Driving to the 稟議 pause yields a grounded rec + an UNDER_REVIEW facility."""
    values = _run_origination_to_pause(_TDB, "ui-orig-pause")
    rec = values["origination_recommendation"]
    assert rec["recommendation"] == "approve"  # creditworthy applicant
    assert rec["grounded"] is True
    assert _origination_loan_status_kanji(values) == LoanStatus.UNDER_REVIEW.kanji


def test_approve_resume_disburses(shared_memory_saver: MemorySaver) -> None:
    """Approve drives the facility through to DISBURSED (実行)."""
    _run_origination_to_pause(_TDB, "ui-orig-approve")
    values = _resume_origination("ui-orig-approve", "approve")
    assert values["origination_decision"] == "approve"
    assert _origination_loan_status_kanji(values) == LoanStatus.DISBURSED.kanji


def test_decline_resume_is_terminal_declined(
    shared_memory_saver: MemorySaver,
) -> None:
    """Decline records DECLINED (謝絶) and never disburses."""
    _run_origination_to_pause(_TDB, "ui-orig-decline")
    values = _resume_origination("ui-orig-decline", "decline")
    assert values["origination_decision"] == "decline"
    assert _origination_loan_status_kanji(values) == LoanStatus.DECLINED.kanji
    statuses = [LoanEvent.model_validate(e).status for e in values["loan_events"]]
    assert LoanStatus.DISBURSED not in statuses
