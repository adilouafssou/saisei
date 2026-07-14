"""Tests for the deterministic cross-signal realism check (depth step 4 pt 3).

assess_realism reconciles the two independent deterministic bands the
feasibility critic produces (execution-risk ``achievability`` vs magnitude
``uplift_credibility``). These tests pin the verdict for every band combination,
the no-assessment empty case, and determinism. Offline, pure, no I/O.
"""

from __future__ import annotations

import pytest
from app.backend.nodes.critics.realism import assess_realism


class TestAssessRealism:
    def test_easy_execution_implausible_payoff_is_optimistic(self) -> None:
        for achievability in ("high", "medium"):
            flag, note = assess_realism(achievability, "implausible")
            assert flag == "optimistic_uplift"
            assert "楽観的" in note
            assert "optimistic" not in note.lower() or "payoff" in note.lower()

    def test_hard_execution_grounded_payoff_is_pessimistic(self) -> None:
        flag, note = assess_realism("low", "grounded")
        assert flag == "pessimistic_uplift"
        assert "慎重" in note

    @pytest.mark.parametrize(
        "achievability,credibility",
        [
            ("high", "grounded"),
            ("high", "stretch"),
            ("medium", "grounded"),
            ("medium", "stretch"),
            ("low", "stretch"),
        ],
    )
    def test_non_contradictory_combinations_are_consistent(
        self, achievability: str, credibility: str
    ) -> None:
        flag, note = assess_realism(achievability, credibility)
        assert flag == "consistent"
        assert "整合あり" in note

    def test_hard_and_implausible_is_consistently_weak(self) -> None:
        """Design-review refinement: BOTH lenses condemn the strategy -> its own
        loud verdict, NOT the reassuring 'consistent'."""
        flag, note = assess_realism("low", "implausible")
        assert flag == "consistently_weak"
        assert "要警戒" in note
        assert "両面不足" in note

    def test_unassessed_band_is_empty(self) -> None:
        assert assess_realism("", "grounded") == ("", "")
        assert assess_realism("high", "") == ("", "")
        assert assess_realism("", "") == ("", "")

    def test_deterministic(self) -> None:
        assert assess_realism("high", "implausible") == assess_realism("high", "implausible")
