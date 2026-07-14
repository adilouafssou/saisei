"""Verifier for guided manual entry (Feature 8 channel 4 — the no-data case).

Guided entry seeds blank staged rows into the EXISTING upload staging seam so
the same editable preview, J-GAAP validation, and ``confirm_upload`` path the
file parser feeds also handle a hand-typed trial balance. These tests pin that
contract on a bare ``SaiseiUIState`` instance (pure field reads/writes; no Reflex
runtime or backend needed), mirroring ``test_portfolio_watchlist.py``.

What is pinned:
- seeding N blank rows with trailing month-end periods (oldest first / newest
  last), zeroed yen, and the guided flag set;
- the month count is clamped to a sane range;
- the period cell is editable ONLY in guided mode;
- money cells edit + re-validate exactly as in the upload flow;
- a fully-filled valid book passes ``upload_is_valid``;
- cancel clears staging and the guided flag.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.frontend.state import SaiseiUIState
from app.shared.models.money import format_jpy

from tests._bare_state import bare_ui_state


def _fget(var: Any, inst: SaiseiUIState) -> Any:
    """Invoke a computed ``rx.var``'s runtime getter (``.fget``)."""
    return var.fget(inst)


def _fn(handler: Any, *args: Any) -> Any:
    """Invoke an ``rx.event`` handler's underlying function (``.fn``)."""
    return handler.fn(*args)


def _fresh() -> SaiseiUIState:
    inst = bare_ui_state()
    inst.upload_preview_rows = []
    inst.upload_serialised = []
    inst.upload_warnings = []
    inst.upload_processing = False
    inst.upload_is_guided = False
    return inst


def test_seeds_requested_number_of_blank_rows() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 6)
    assert len(inst.upload_serialised) == 6
    assert len(inst.upload_preview_rows) == 6
    assert inst.upload_is_guided is True
    # Every seeded money figure starts at zero.
    for row in inst.upload_serialised:
        assert row["uriage"] == 0
        assert row["uriage_genka"] == 0
        assert row["hanbaihi"] == 0


def test_periods_are_trailing_month_ends_oldest_first() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 3)
    periods = [dt.date.fromisoformat(r["period"]) for r in inst.upload_serialised]
    # Strictly increasing (oldest first, newest last).
    assert periods == sorted(periods)
    assert periods[0] < periods[-1]
    # Each is a month-end (the day after is the first of the next month).
    for p in periods:
        assert (p + dt.timedelta(days=1)).day == 1


def test_month_count_is_clamped() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 0)
    assert len(inst.upload_serialised) == 1  # clamped up to 1
    inst2 = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst2, 999)
    assert len(inst2.upload_serialised) == 36  # clamped down to 36


def test_period_editable_only_in_guided_mode() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 2)
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "period", "2024-01-31")
    assert inst.upload_serialised[0]["period"] == "2024-01-31"
    assert inst.upload_preview_rows[0]["period"] == "2024-01-31"

    # Outside guided mode (e.g. a parsed file) the period stays immutable.
    inst.upload_is_guided = False
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "period", "1999-12-31")
    assert inst.upload_serialised[0]["period"] == "2024-01-31"  # unchanged


def test_money_cell_edit_and_validation() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 1)
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "uriage", "100000000")
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "uriage_genka", "70000000")
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "hanbaihi", "20000000")
    assert inst.upload_serialised[0]["uriage"] == 100_000_000
    # A fully-filled, consistent row is valid -> Confirm enabled.
    assert _fget(SaiseiUIState.upload_is_valid, inst) is True
    errors = _fget(SaiseiUIState.upload_row_errors, inst)
    assert errors == [""]


def test_cogs_exceeds_sales_flags_invalid() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 1)
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "uriage", "10")
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "uriage_genka", "99")
    assert _fget(SaiseiUIState.upload_is_valid, inst) is False
    assert _fget(SaiseiUIState.upload_row_errors, inst)[0] != ""


def test_non_numeric_money_cell_does_not_leave_stale_keijo_preview() -> None:
    """Typing garbage into a money cell refreshes (not strands) the keijo preview.

    Regression: edit_upload_cell stored the raw non-numeric text (so the row is
    flagged invalid) but the old single-try display refresh aborted on the first
    non-numeric term, leaving a STALE keijo_rieki next to the bad cell. The
    preview must instead reflect the current input (invalid term -> 0 for
    display), while the row stays invalid so Confirm remains blocked and no
    invalid figure can reach the pipeline.
    """
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 1)
    # Seed a valid row so the preview holds a real, non-zero keijo first.
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "uriage", "100")
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "uriage_genka", "40")
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "hanbaihi", "10")
    stale = inst.upload_preview_rows[0]["keijo_rieki"]
    assert stale == format_jpy(50)  # 100 - 40 - 10

    # Now the banker fat-fingers a non-numeric sales figure.
    _fn(SaiseiUIState.edit_upload_cell, inst, 0, "uriage", "abc")

    # The serialised value keeps the raw text so the row is flagged invalid...
    assert inst.upload_serialised[0]["uriage"] == "abc"
    assert _fget(SaiseiUIState.upload_is_valid, inst) is False
    assert _fget(SaiseiUIState.upload_row_errors, inst)[0] != ""
    # ...and the keijo preview is refreshed (invalid sales -> 0 for display),
    # NOT left at the stale +¥50 from before the bad edit.
    refreshed = inst.upload_preview_rows[0]["keijo_rieki"]
    assert refreshed != stale
    assert refreshed == format_jpy(0 - 40 - 10)  # -¥50


def test_cancel_clears_guided_state() -> None:
    inst = _fresh()
    _fn(SaiseiUIState.start_guided_entry, inst, 4)
    _fn(SaiseiUIState.cancel_upload, inst)
    assert inst.upload_serialised == []
    assert inst.upload_preview_rows == []
    assert inst.upload_is_guided is False
