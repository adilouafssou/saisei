"""Verifier for the ephemeral Portfolio watchlist state (Feature 8.1).

No CI here, so this pins the governance-light watchlist contract: snapshots are
captured from already-computed display fields, accumulate across borrowers in
the session, the latest assessment of a borrower replaces its prior row, the
"just crossed the 要注意 floor" flag is detected vs a prior in-session snapshot,
the ranking is worst-first, and the sparkline series uses only real figures.

The methods are pure (they read/write plain fields on a bare instance), so no
Reflex runtime or backend is needed.
"""

from __future__ import annotations

from typing import Any

from app.frontend.state import SaiseiUIState
from app.shared.constants import EWS_SUBSTANDARD

from tests._bare_state import bare_ui_state


def _fget(var: Any, inst: SaiseiUIState) -> Any:
    """Invoke a computed ``rx.var``'s runtime getter (``.fget``)."""
    return var.fget(inst)


def _fn(handler: Any, *args: Any) -> Any:
    """Invoke an ``rx.event`` handler's underlying function (``.fn``)."""
    return handler.fn(*args)


def _fresh() -> SaiseiUIState:
    inst = bare_ui_state()
    inst.portfolio_rows = []
    inst.portfolio_filter = "all"
    inst.show_portfolio = False
    inst.tdb_code = ""
    inst.company_name = ""
    inst.ews_score = 0.0
    inst.fsa_kanji = ""
    inst.recovery_serialised = {}
    return inst


def _capture(inst: SaiseiUIState, *, code: str, name: str, ews: float, kanji: str) -> None:
    inst.tdb_code = code
    inst.company_name = name
    inst.ews_score = ews
    inst.fsa_kanji = kanji
    inst._capture_portfolio_snapshot()


def test_snapshot_appends_a_row() -> None:
    inst = _fresh()
    _capture(inst, code="1234567", name="製造アイチ", ews=72.0, kanji="破綻懸念先")
    assert len(inst.portfolio_rows) == 1
    row = inst.portfolio_rows[0]
    assert row["tdb_code"] == "1234567"
    assert row["company_name"] == "製造アイチ"
    assert row["ews"] == "72.00"
    assert row["fsa_kanji"] == "破綻懸念先"


def test_snapshot_accumulates_across_borrowers() -> None:
    inst = _fresh()
    _capture(inst, code="1111111", name="A", ews=30.0, kanji="正常先")
    _capture(inst, code="2222222", name="B", ews=80.0, kanji="破綻懸念先")
    assert {r["tdb_code"] for r in inst.portfolio_rows} == {"1111111", "2222222"}


def test_latest_assessment_replaces_prior_row() -> None:
    inst = _fresh()
    _capture(inst, code="1234567", name="X", ews=30.0, kanji="正常先")
    _capture(inst, code="1234567", name="X", ews=55.0, kanji="要注意先")
    rows = [r for r in inst.portfolio_rows if r["tdb_code"] == "1234567"]
    assert len(rows) == 1  # not duplicated
    assert rows[0]["ews"] == "55.00"


def test_crossed_flag_set_when_moving_above_floor() -> None:
    """Re-assessing a borrower that rose from below to >= the 要注意 floor flags it."""
    inst = _fresh()
    below = float(EWS_SUBSTANDARD) - 5.0
    above = float(EWS_SUBSTANDARD) + 5.0
    _capture(inst, code="1234567", name="X", ews=below, kanji="正常先")
    _capture(inst, code="1234567", name="X", ews=above, kanji="要注意先")
    row = next(r for r in inst.portfolio_rows if r["tdb_code"] == "1234567")
    assert row["crossed"] == "yes"


def test_not_crossed_when_already_above() -> None:
    inst = _fresh()
    above = float(EWS_SUBSTANDARD) + 5.0
    _capture(inst, code="1234567", name="X", ews=above, kanji="要注意先")
    _capture(inst, code="1234567", name="X", ews=above + 3.0, kanji="要注意先")
    row = next(r for r in inst.portfolio_rows if r["tdb_code"] == "1234567")
    assert row["crossed"] == "no"


def test_first_assessment_is_never_crossed() -> None:
    """A borrower seen for the first time has no prior, so cannot be 'crossed'."""
    inst = _fresh()
    _capture(inst, code="1234567", name="X", ews=float(EWS_SUBSTANDARD) + 10.0, kanji="要注意先")
    row = inst.portfolio_rows[0]
    assert row["crossed"] == "no"


def test_ews_series_single_point_without_projection() -> None:
    inst = _fresh()
    _capture(inst, code="1234567", name="X", ews=63.0, kanji="要注意先")
    assert inst.portfolio_rows[0]["ews_series"] == "63.00"


def test_ews_series_uses_projection_when_present() -> None:
    """With a recovery projection, the series is baseline + projected EWS (real)."""
    inst = _fresh()
    inst.recovery_serialised = {
        "baseline_ews": 70.0,
        "months": [
            {"ews_score": 60.0},
            {"ews_score": 45.0},
        ],
    }
    _capture(inst, code="1234567", name="X", ews=70.0, kanji="破綻懸念先")
    assert inst.portfolio_rows[0]["ews_series"] == "70.00,60.00,45.00"


def test_no_capture_without_code() -> None:
    """An empty TDB code captures nothing (defensive)."""
    inst = _fresh()
    inst._capture_portfolio_snapshot()
    assert inst.portfolio_rows == []


def test_ranked_is_worst_first() -> None:
    inst = _fresh()
    _capture(inst, code="1111111", name="low", ews=30.0, kanji="正常先")
    _capture(inst, code="2222222", name="high", ews=88.0, kanji="実質破綻先")
    _capture(inst, code="3333333", name="mid", ews=60.0, kanji="要注意先")
    ranked = _fget(SaiseiUIState.portfolio_ranked, inst)
    assert [r["tdb_code"] for r in ranked] == ["2222222", "3333333", "1111111"]


def test_counts() -> None:
    inst = _fresh()
    below = float(EWS_SUBSTANDARD) - 5.0
    above = float(EWS_SUBSTANDARD) + 5.0
    _capture(inst, code="1234567", name="X", ews=below, kanji="正常先")
    _capture(inst, code="1234567", name="X", ews=above, kanji="要注意先")  # crosses
    _capture(inst, code="7654321", name="Y", ews=20.0, kanji="正常先")
    assert _fget(SaiseiUIState.portfolio_count, inst) == 2
    assert _fget(SaiseiUIState.portfolio_crossed_count, inst) == 1


def test_clear_portfolio_empties_the_view() -> None:
    inst = _fresh()
    _capture(inst, code="1234567", name="X", ews=50.0, kanji="要注意先")
    _fn(SaiseiUIState.clear_portfolio, inst)
    assert inst.portfolio_rows == []


def test_drill_in_sets_code_and_closes_without_running() -> None:
    """Drilling in sets the TDB code and leaves the watchlist; no auto-run."""
    inst = _fresh()
    inst.show_portfolio = True
    _fn(SaiseiUIState.open_borrower_from_portfolio, inst, "9999999")
    assert inst.tdb_code == "9999999"
    assert inst.show_portfolio is False


def _dist(inst: SaiseiUIState) -> dict[str, dict[str, str]]:
    """Return the portfolio_distribution rows keyed by band for assertions."""
    return {b["key"]: b for b in _fget(SaiseiUIState.portfolio_distribution, inst)}


def test_distribution_empty_book_all_zero() -> None:
    inst = _fresh()
    dist = _dist(inst)
    assert {k: dist[k]["count"] for k in dist} == {
        "normal": "0",
        "attention": "0",
        "doubtful": "0",
        "danger": "0",
    }


def test_distribution_bins_by_authoritative_thresholds() -> None:
    """Each borrower lands in the FSA band its EWS implies (constants-driven)."""
    inst = _fresh()
    _capture(inst, code="1111111", name="A", ews=20.0, kanji="正常先")  # < 40 normal
    _capture(inst, code="2222222", name="B", ews=55.0, kanji="要注意先")  # 40-70 attention
    _capture(inst, code="3333333", name="C", ews=78.0, kanji="破綻懸念先")  # 70-85 doubtful
    _capture(inst, code="4444444", name="D", ews=92.0, kanji="実質破綻先")  # >= 85 danger
    dist = _dist(inst)
    assert dist["normal"]["count"] == "1"
    assert dist["attention"]["count"] == "1"
    assert dist["doubtful"]["count"] == "1"
    assert dist["danger"]["count"] == "1"


def test_distribution_widths_sum_to_100_when_populated() -> None:
    inst = _fresh()
    _capture(inst, code="1111111", name="A", ews=20.0, kanji="正常先")
    _capture(inst, code="2222222", name="B", ews=55.0, kanji="要注意先")
    _capture(inst, code="3333333", name="C", ews=78.0, kanji="破綻懸念先")
    dist = _dist(inst)
    total = round(sum(float(b["width_pct"]) for b in dist.values()), 2)
    assert total == 100.0


def test_distribution_band_boundaries_are_inclusive_lower() -> None:
    """EWS exactly on a floor lands in the higher (worse) band."""
    inst = _fresh()
    _capture(inst, code="1111111", name="A", ews=float(EWS_SUBSTANDARD), kanji="要注意先")
    dist = _dist(inst)
    assert dist["attention"]["count"] == "1"
    assert dist["normal"]["count"] == "0"


def _book_of_three(inst: SaiseiUIState) -> None:
    """A 3-borrower book: one normal, one distressed, one that just crossed."""
    below = float(EWS_SUBSTANDARD) - 5.0
    above = float(EWS_SUBSTANDARD) + 5.0
    _capture(inst, code="1111111", name="normal", ews=20.0, kanji="正常先")
    _capture(inst, code="2222222", name="distressed", ews=above, kanji="要注意先")
    # Crosses: first below the floor, then above on re-assessment.
    _capture(inst, code="3333333", name="crosser", ews=below, kanji="正常先")
    _capture(inst, code="3333333", name="crosser", ews=above, kanji="要注意先")


def test_filter_default_is_all() -> None:
    inst = _fresh()
    assert inst.portfolio_filter == "all"
    _book_of_three(inst)
    assert _fget(SaiseiUIState.portfolio_filtered_count, inst) == 3


def test_filter_crossed_keeps_only_crossed() -> None:
    inst = _fresh()
    _book_of_three(inst)
    inst.portfolio_filter = "crossed"
    ranked = _fget(SaiseiUIState.portfolio_ranked, inst)
    assert [r["tdb_code"] for r in ranked] == ["3333333"]
    assert _fget(SaiseiUIState.portfolio_filtered_count, inst) == 1


def test_filter_distressed_keeps_at_or_above_floor() -> None:
    inst = _fresh()
    _book_of_three(inst)
    inst.portfolio_filter = "distressed"
    ranked = _fget(SaiseiUIState.portfolio_ranked, inst)
    # Both the distressed and the crosser are >= the floor; the normal is not.
    assert {r["tdb_code"] for r in ranked} == {"2222222", "3333333"}


def test_set_portfolio_filter_ignores_unknown() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.set_portfolio_filter, inst, "crossed")
    assert inst.portfolio_filter == "crossed"
    _fn(SaiseiUIState.set_portfolio_filter, inst, "bogus")
    assert inst.portfolio_filter == "crossed"  # unchanged


def test_clear_resets_filter_to_all() -> None:
    inst = _fresh()
    _book_of_three(inst)
    inst.portfolio_filter = "crossed"
    _fn(SaiseiUIState.clear_portfolio, inst)
    assert inst.portfolio_rows == []
    assert inst.portfolio_filter == "all"


def test_row_carries_updated_at() -> None:
    inst = _fresh()
    _capture(inst, code="1234567", name="X", ews=50.0, kanji="要注意先")
    assert inst.portfolio_rows[0]["updated_at"] != ""


def test_row_carries_loan_status() -> None:
    """The watchlist row surfaces the facility's current loan-lifecycle status.

    loan_status_kanji is already derived from the snapshot's loan_events by
    _apply_loan_summary; the capture must copy it onto the row so the unified
    book can show where each facility sits in its arc.
    """
    inst = _fresh()
    inst.loan_status_kanji = "実行"  # Disbursed (a freshly-originated facility)
    _capture(inst, code="1234567", name="X", ews=50.0, kanji="要注意先")
    assert inst.portfolio_rows[0]["loan_status"] == "実行"


def test_row_loan_status_empty_without_facility() -> None:
    """With no attached facility, the row's loan_status is blank (renders '—')."""
    inst = _fresh()
    inst.loan_status_kanji = ""
    _capture(inst, code="1234567", name="X", ews=50.0, kanji="要注意先")
    assert inst.portfolio_rows[0]["loan_status"] == ""


def test_view_rows_preserve_loan_status() -> None:
    """The rendered projection keeps loan_status through the ranking/sparkline map."""
    inst = _fresh()
    inst.loan_status_kanji = "条件変更"  # Restructured (a turnaround case)
    _capture(inst, code="1234567", name="X", ews=66.0, kanji="要注意先")
    view = _fget(SaiseiUIState.portfolio_view_rows, inst)
    assert view[0]["loan_status"] == "条件変更"
