"""Tests for the portfolio credit-signal roll-up (capacity / coverage bands).

The state-layer verifier for the book-level origination roll-up: it proves the
per-facility capacity (返済余力) and coverage (担保・保証) bands captured at each
稟議 run aggregate into a deterministic band distribution for the whole session's
originated book — the origination twin of ``portfolio_distribution`` (EWS bands).

Pure and offline: it drives the PURE parts of ``SaiseiUIState`` via the shared
``bare_ui_state`` helper (no Reflex runtime / event loop), appends book rows the
same way ``_apply_origination_snapshot`` does, and reads the roll-up computed
vars. Computes no figure; it only bins already-computed bands.
"""

from __future__ import annotations

from tests._bare_state import bare_ui_state


def _row(
    tdb_code: str,
    capacity_band: str,
    coverage_band: str,
    recommendation: str = "approve",
) -> dict[str, str]:
    """A book row as ``_capture_origination_book_row`` records it."""
    return {
        "tdb_code": tdb_code,
        "company": f"Co {tdb_code}",
        "recommendation": recommendation,
        "capacity_band": capacity_band,
        "coverage_band": coverage_band,
    }


def _counts(distribution: list[dict[str, str]]) -> dict[str, int]:
    """Map a distribution's rows to {band_key: count} for assertions."""
    return {row["key"]: int(row["count"]) for row in distribution}


class TestCaptureBookRow:
    """_capture_origination_book_row records / replaces the per-facility row."""

    def test_appends_a_row_with_both_bands(self) -> None:
        state = bare_ui_state()
        state.origination_book = []
        state.origination_code = "1234567"
        state.origination_company = "Test KK"
        state._capture_origination_book_row(
            {
                "recommendation": "approve",
                "capacity_band": "stretch",
                "coverage_band": "partial",
            }
        )
        assert len(state.origination_book) == 1
        row = state.origination_book[0]
        assert row["capacity_band"] == "stretch"
        assert row["coverage_band"] == "partial"

    def test_skips_when_no_recommendation(self) -> None:
        # An empty / errored run yields no recommendation -> no book row.
        state = bare_ui_state()
        state.origination_book = []
        state.origination_code = "1234567"
        state._capture_origination_book_row({"recommendation": ""})
        assert state.origination_book == []

    def test_latest_run_per_code_wins(self) -> None:
        # Re-originating the same applicant replaces its row (no duplicate).
        state = bare_ui_state()
        state.origination_book = []
        state.origination_code = "1234567"
        state.origination_company = "Test KK"
        state._capture_origination_book_row(
            {
                "recommendation": "approve",
                "capacity_band": "over_capacity",
                "coverage_band": "uncovered",
            }
        )
        state._capture_origination_book_row(
            {
                "recommendation": "approve",
                "capacity_band": "within_capacity",
                "coverage_band": "well_covered",
            }
        )
        assert len(state.origination_book) == 1
        assert state.origination_book[0]["capacity_band"] == "within_capacity"
        assert state.origination_book[0]["coverage_band"] == "well_covered"


class TestCapacityDistribution:
    """The capacity roll-up tallies the book into the three capacity bands."""

    def test_empty_book_is_all_zero(self) -> None:
        state = bare_ui_state()
        state.origination_book = []
        counts = _counts(state.origination_capacity_distribution)
        assert counts == {"within_capacity": 0, "stretch": 0, "over_capacity": 0}

    def test_tallies_each_band(self) -> None:
        state = bare_ui_state()
        state.origination_book = [
            _row("1", "within_capacity", "well_covered"),
            _row("2", "within_capacity", "partial"),
            _row("3", "stretch", "uncovered"),
            _row("4", "over_capacity", "uncovered"),
        ]
        counts = _counts(state.origination_capacity_distribution)
        assert counts == {"within_capacity": 2, "stretch": 1, "over_capacity": 1}

    def test_rows_without_a_capacity_band_are_skipped(self) -> None:
        state = bare_ui_state()
        state.origination_book = [
            _row("1", "", "uncovered"),  # a DECLINE may omit the capacity band
            _row("2", "over_capacity", "uncovered"),
        ]
        counts = _counts(state.origination_capacity_distribution)
        assert counts == {"within_capacity": 0, "stretch": 0, "over_capacity": 1}

    def test_widths_are_proportional(self) -> None:
        # Two facilities, one in each of two bands -> 50% width segments.
        state = bare_ui_state()
        state.origination_book = [
            _row("1", "within_capacity", "well_covered"),
            _row("2", "over_capacity", "uncovered"),
        ]
        widths = {r["key"]: float(r["width_pct"]) for r in state.origination_capacity_distribution}
        assert widths["within_capacity"] == 50.0
        assert widths["over_capacity"] == 50.0
        assert widths["stretch"] == 0.0


class TestCoverageDistribution:
    """The coverage roll-up tallies the book into the three coverage bands."""

    def test_empty_book_is_all_zero(self) -> None:
        state = bare_ui_state()
        state.origination_book = []
        counts = _counts(state.origination_coverage_distribution)
        assert counts == {"well_covered": 0, "partial": 0, "uncovered": 0}

    def test_tallies_each_band(self) -> None:
        state = bare_ui_state()
        state.origination_book = [
            _row("1", "within_capacity", "well_covered"),
            _row("2", "stretch", "partial"),
            _row("3", "over_capacity", "uncovered"),
            _row("4", "within_capacity", "uncovered"),
        ]
        counts = _counts(state.origination_coverage_distribution)
        assert counts == {"well_covered": 1, "partial": 1, "uncovered": 2}

    def test_count_reflects_the_book_size(self) -> None:
        state = bare_ui_state()
        state.origination_book = [
            _row("1", "within_capacity", "well_covered"),
            _row("2", "stretch", "partial"),
        ]
        assert state.origination_book_count == 2


class TestTwoLensesAreIndependent:
    """Capacity and coverage roll-ups bin the SAME book on different axes."""

    def test_over_capacity_can_be_well_covered(self) -> None:
        # A facility the P&L cannot service yet is fully collateralised: it sits
        # in over_capacity on one axis and well_covered on the other.
        state = bare_ui_state()
        state.origination_book = [_row("1", "over_capacity", "well_covered")]
        cap = _counts(state.origination_capacity_distribution)
        cov = _counts(state.origination_coverage_distribution)
        assert cap["over_capacity"] == 1
        assert cov["well_covered"] == 1


class TestBookViewRows:
    """origination_book_view_rows maps raw rows to display strings, worst-first."""

    def test_empty_book_is_empty(self) -> None:
        state = bare_ui_state()
        state.origination_book = []
        assert state.origination_book_view_rows == []

    def test_maps_bands_to_labels_and_accents(self) -> None:
        state = bare_ui_state()
        state.origination_book = [_row("1", "stretch", "partial")]
        view = state.origination_book_view_rows[0]
        assert view["capacity_label"] == "余力上限"
        assert view["capacity_accent"] == "warn"
        assert view["coverage_label"] == "一部保全"
        assert view["coverage_accent"] == "warn"
        assert view["recommendation_label"] == "承認"
        assert view["recommendation_accent"] == "positive"

    def test_decline_recommendation_maps_to_fail(self) -> None:
        state = bare_ui_state()
        state.origination_book = [
            _row("1", "within_capacity", "well_covered", recommendation="decline")
        ]
        view = state.origination_book_view_rows[0]
        assert view["recommendation_label"] == "謝絶"
        assert view["recommendation_accent"] == "fail"

    def test_missing_band_degrades_to_dash_and_neutral_accent(self) -> None:
        # A DECLINE may omit a band: the label is an em-dash, accent neutral.
        state = bare_ui_state()
        state.origination_book = [_row("1", "", "")]
        view = state.origination_book_view_rows[0]
        assert view["capacity_label"] == "—"
        assert view["capacity_accent"] == "chrome"
        assert view["coverage_label"] == "—"
        assert view["coverage_accent"] == "chrome"

    def test_preserves_tdb_code_and_company_for_drill_in(self) -> None:
        state = bare_ui_state()
        state.origination_book = [_row("7654321", "within_capacity", "well_covered")]
        view = state.origination_book_view_rows[0]
        assert view["tdb_code"] == "7654321"
        assert view["company"] == "Co 7654321"

    def test_worst_first_ordering_by_more_severe_lens(self) -> None:
        # A well-covered, within-capacity facility (all positive) must sort BELOW
        # one that is over_capacity on either lens. Ranking keys off the more
        # severe of the two lenses (fail > warn > positive).
        state = bare_ui_state()
        state.origination_book = [
            _row("safe", "within_capacity", "well_covered"),
            _row("risky", "within_capacity", "uncovered"),  # uncovered == fail
            _row("mid", "stretch", "well_covered"),  # stretch == warn
        ]
        order = [r["tdb_code"] for r in state.origination_book_view_rows]
        assert order == ["risky", "mid", "safe"]
