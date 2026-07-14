"""Tests for surfacing the debt-service-capacity band at the 稟議 gate.

The UI-side verifier for the !1 debt-capacity annotation: it proves the nested
``debt_capacity`` block the deterministic ``loan_origination_node`` writes onto
``origination_recommendation`` survives the snapshot -> display-view round-trip
the origination dialog reads through, and degrades safely when absent.

Pure and offline: it exercises the module-level ``_origination_recommendation_view``
mapping directly (no Reflex runtime, no graph, no network), exactly as the state
funnel (``_apply_origination_snapshot``) consumes it.
"""

from __future__ import annotations

from app.frontend.state import _origination_recommendation_view


def _approve_snapshot(band: str) -> dict[str, object]:
    """An APPROVE snapshot carrying a debt_capacity block in the given band."""
    return {
        "origination_recommendation": {
            "recommendation": "approve",
            "max_facility_amount": 100_000_000,
            "reason": "TDB score clears the floor [tdb_score]",
            "grounded": True,
            "debt_capacity": {
                "band": band,
                "annual_debt_service": 12_500_000,
                "prudent_service_ceiling": 6_000_000,
                "ratio": 2.08,
                "reason": "想定年間返済額 vs 健全返済余力 ・ 余力超過 (over capacity)",
            },
        }
    }


class TestCapacityViewMapping:
    """The debt_capacity block is parsed into display strings for the card."""

    def test_band_and_reason_surface(self) -> None:
        view = _origination_recommendation_view(_approve_snapshot("over_capacity"))
        assert view["capacity_band"] == "over_capacity"
        assert "余力超過" in view["capacity_reason"]

    def test_figures_are_formatted_jpy(self) -> None:
        view = _origination_recommendation_view(_approve_snapshot("stretch"))
        # format_jpy renders integer yen with separators + the ¥ unit; assert the
        # digits survive rather than pinning the exact glyphs.
        assert "12,500,000" in view["capacity_debt_service"]
        assert "6,000,000" in view["capacity_ceiling"]

    def test_each_band_passes_through(self) -> None:
        for band in ("within_capacity", "stretch", "over_capacity"):
            view = _origination_recommendation_view(_approve_snapshot(band))
            assert view["capacity_band"] == band


class TestCapacityViewDegradesSafely:
    """A missing / partial block never raises and yields empty display fields."""

    def test_no_block_yields_empty_band_and_dash_figures(self) -> None:
        # A DECLINE carries a 0 ceiling and may omit the debt_capacity block.
        snapshot = {
            "origination_recommendation": {
                "recommendation": "decline",
                "max_facility_amount": 0,
                "reason": "below the origination approval floor [tdb_score]",
                "grounded": True,
            }
        }
        view = _origination_recommendation_view(snapshot)
        assert view["capacity_band"] == ""
        assert view["capacity_reason"] == ""
        assert view["capacity_debt_service"] == "\u2014"
        assert view["capacity_ceiling"] == "\u2014"

    def test_zero_figures_render_as_dash(self) -> None:
        snapshot = {
            "origination_recommendation": {
                "recommendation": "approve",
                "max_facility_amount": 0,
                "reason": "r",
                "grounded": True,
                "debt_capacity": {
                    "band": "within_capacity",
                    "annual_debt_service": 0,
                    "prudent_service_ceiling": 0,
                    "ratio": 0.0,
                    "reason": "返済負担なし (within capacity)",
                },
            }
        }
        view = _origination_recommendation_view(snapshot)
        assert view["capacity_band"] == "within_capacity"
        assert view["capacity_debt_service"] == "\u2014"
        assert view["capacity_ceiling"] == "\u2014"

    def test_empty_snapshot_does_not_raise(self) -> None:
        view = _origination_recommendation_view({})
        assert view["capacity_band"] == ""
        assert view["recommendation"] == ""
