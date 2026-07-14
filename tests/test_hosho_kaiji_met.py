"""Regression: kaiji_met must track the disclosure components, not a drifting weight.

The disclosure (kaiji) sub-points are hardcoded (10 + 10 + 5 = 25) but kaiji_met
was compared against the shared HOSHO_WEIGHT_KAIJI constant. They coincide today
(both 25), so the bug is latent: retuning HOSHO_WEIGHT_KAIJI (the constants file
even notes the pillar weights 'must sum to 100', inviting rebalancing) would
desync the 'complete disclosure' verdict from the score — met could read True
with a component missing (weight lowered) or never True with full disclosure
(weight raised). The fix pins kaiji_met to the components' own maximum.

This test would FAIL if kaiji_met were still tied to HOSHO_WEIGHT_KAIJI and that
constant were changed. Fully offline.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.backend.nodes.keieisha_hosho import assess_hosho_kaijo

_FULL_KWARGS: dict[str, Any] = {
    "avg_eigai_shueki": 500_000.0,
    "avg_eigai_hiyo": 300_000.0,
    "avg_uriage": 100_000_000.0,
    "ews_score": 20.0,
    "working_capital_gap": 5_000_000,
}


def test_full_disclosure_is_met_and_scores_max() -> None:
    cond = assess_hosho_kaijo(shisanhyo_count=12, tdb_score=75, error_count=0, **_FULL_KWARGS)
    assert cond.kaiji_score == 25.0
    assert cond.kaiji_met is True


@pytest.mark.parametrize(
    ("months", "tdb", "errors"),
    [
        (6, 75, 0),  # missing shisanhyo months
        (12, None, 0),  # missing TDB score
        (12, 75, 3),  # has errors
    ],
)
def test_incomplete_disclosure_is_not_met(months: int, tdb: int | None, errors: int) -> None:
    cond = assess_hosho_kaijo(
        shisanhyo_count=months, tdb_score=tdb, error_count=errors, **_FULL_KWARGS
    )
    assert cond.kaiji_score < 25.0
    assert cond.kaiji_met is False


def test_kaiji_met_decoupled_from_shared_weight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retuning HOSHO_WEIGHT_KAIJI must not flip kaiji_met for full disclosure."""
    import app.backend.nodes.keieisha_hosho as mod

    # Simulate a rebalance that raises the shared kaiji weight above the
    # components' hardcoded max (25). met must still be True for full disclosure.
    monkeypatch.setattr(mod, "_WEIGHT_KAIJI", 30.0, raising=False)
    cond = assess_hosho_kaijo(shisanhyo_count=12, tdb_score=75, error_count=0, **_FULL_KWARGS)
    assert cond.kaiji_score == 25.0
    assert cond.kaiji_met is True
