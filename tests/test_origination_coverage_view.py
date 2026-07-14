"""Tests for surfacing the collateral/guarantee coverage band at the 稟議 gate.

The UI-side verifier for the coverage annotation (breadth #6, the twin of
test_origination_capacity_view.py): it proves the nested ``coverage`` block the
deterministic ``loan_origination_node`` writes onto ``origination_recommendation``
survives the snapshot -> display-view round-trip the origination dialog reads
through, and degrades safely when absent.

Pure and offline: it exercises the module-level ``_origination_recommendation_view``
mapping directly (no Reflex runtime, no graph, no network), exactly as the state
funnel (``_apply_origination_snapshot``) consumes it.
"""

from __future__ import annotations

from app.frontend.state import _origination_recommendation_view


def _approve_snapshot(band: str) -> dict[str, object]:
    """An APPROVE snapshot carrying a coverage block in the given band."""
    return {
        "origination_recommendation": {
            "recommendation": "approve",
            "max_facility_amount": 100_000_000,
            "reason": "TDB score clears the floor [tdb_score]",
            "grounded": True,
            "coverage": {
                "band": band,
                "covered_amount": 60_000_000,
                "uncovered_amount": 40_000_000,
                "ratio": 0.6,
                "reason": "カバー額 vs 融資額 ・ 一部保全 (partial)",
            },
        }
    }


class TestCoverageViewMapping:
    """The coverage block is parsed into display strings for the card."""

    def test_band_and_reason_surface(self) -> None:
        view = _origination_recommendation_view(_approve_snapshot("partial"))
        assert view["coverage_band"] == "partial"
        assert "一部保全" in view["coverage_reason"]

    def test_figures_are_formatted_jpy(self) -> None:
        view = _origination_recommendation_view(_approve_snapshot("partial"))
        # format_jpy renders integer yen with separators; assert the digits
        # survive rather than pinning the exact glyphs.
        assert "60,000,000" in view["coverage_covered"]
        assert "40,000,000" in view["coverage_uncovered"]

    def test_each_band_passes_through(self) -> None:
        for band in ("well_covered", "partial", "uncovered"):
            view = _origination_recommendation_view(_approve_snapshot(band))
            assert view["coverage_band"] == band


class TestCoverageViewDegradesSafely:
    """A missing / partial block never raises and yields empty display fields."""

    def test_no_block_yields_empty_band_and_dash_figures(self) -> None:
        # A DECLINE may omit the coverage block entirely.
        snapshot = {
            "origination_recommendation": {
                "recommendation": "decline",
                "max_facility_amount": 0,
                "reason": "below the origination approval floor [tdb_score]",
                "grounded": True,
            }
        }
        view = _origination_recommendation_view(snapshot)
        assert view["coverage_band"] == ""
        assert view["coverage_reason"] == ""
        assert view["coverage_covered"] == "\u2014"
        assert view["coverage_uncovered"] == "\u2014"

    def test_zero_figures_render_as_dash(self) -> None:
        # A well_covered 0-facility (DECLINE) carries zero amounts -> dashes.
        snapshot = {
            "origination_recommendation": {
                "recommendation": "decline",
                "max_facility_amount": 0,
                "reason": "r",
                "grounded": True,
                "coverage": {
                    "band": "well_covered",
                    "covered_amount": 0,
                    "uncovered_amount": 0,
                    "ratio": None,
                    "reason": "融資なし (no facility)",
                },
            }
        }
        view = _origination_recommendation_view(snapshot)
        assert view["coverage_band"] == "well_covered"
        assert view["coverage_covered"] == "\u2014"
        assert view["coverage_uncovered"] == "\u2014"

    def test_empty_snapshot_does_not_raise(self) -> None:
        view = _origination_recommendation_view({})
        assert view["coverage_band"] == ""
        assert view["recommendation"] == ""


class TestCoverageAndCapacityCoexist:
    """Both advisory blocks surface together (the two credit lenses)."""

    def test_both_blocks_map_independently(self) -> None:
        snapshot = {
            "origination_recommendation": {
                "recommendation": "approve",
                "max_facility_amount": 100_000_000,
                "reason": "r",
                "grounded": True,
                "debt_capacity": {
                    "band": "over_capacity",
                    "annual_debt_service": 12_500_000,
                    "prudent_service_ceiling": 6_000_000,
                    "ratio": 2.08,
                    "reason": "余力超過 (over capacity)",
                },
                "coverage": {
                    "band": "well_covered",
                    "covered_amount": 110_000_000,
                    "uncovered_amount": 0,
                    "ratio": 1.1,
                    "reason": "保全十分 (well covered)",
                },
            }
        }
        view = _origination_recommendation_view(snapshot)
        # An over-capacity facility can still be well covered: both lenses show.
        assert view["capacity_band"] == "over_capacity"
        assert view["coverage_band"] == "well_covered"
