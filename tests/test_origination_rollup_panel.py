"""Structure/smoke verifier for the origination credit-signal roll-up PANEL.

The data paths behind the roll-up are pinned by tests/test_portfolio_credit_
rollup.py (the state vars). This file closes the standing residual on the other
side: the panel COMPONENT builders themselves were never exercised, so a typo'd
state-var reference, a malformed ``rx.match``, a ``rx.foreach`` over the wrong
var, or a missing prop would only surface at app build / browser render time.

Reflex components are plain Python objects constructed eagerly when the builder
function is called -- no event loop, app wiring, or browser needed (the same
offline posture as tests/_bare_state and tests/test_explainability_report_ui).
So this simply CALLS each roll-up builder and asserts it constructs a real
``rx.Component`` without raising. It is a structural smoke test, not a
browser-level render assertion: it proves the JSX tree is well-formed and every
state-var / helper reference resolves, which is exactly the class of bug the
residual was about. It deliberately does NOT assert pixels / final HTML.

The builders are driven by the REAL ``SaiseiUIState`` vars (not hand-built
Vars), so each test exercises the exact ``rx.foreach`` / ``rx.cond`` / var-
reference wiring the page uses -- the paths where the residual's bugs would live.
"""

from __future__ import annotations

import reflex as rx
from app.frontend.components import portfolio_panel as panel
from app.frontend.state import SaiseiUIState


class TestRollupBuilders:
    """Each roll-up builder constructs a well-formed component tree offline."""

    def test_credit_distribution_overview_constructs(self) -> None:
        # A single bar, driven by the real distribution + count vars.
        comp = panel._credit_distribution_overview(
            "返済余力分布 (Debt-service capacity)",
            "Originated book across debt-service-capacity bands",
            SaiseiUIState.origination_capacity_distribution,
            SaiseiUIState.origination_book_count,
        )
        assert isinstance(comp, rx.Component)

    def test_book_table_constructs(self) -> None:
        # Exercises the rx.foreach over origination_book_view_rows + _book_row
        # (incl. the per-row rx.match on the recommendation accent).
        assert isinstance(panel._book_table(), rx.Component)

    def test_origination_rollup_constructs(self) -> None:
        # The whole section: heading + two bars + dividers + per-facility table.
        assert isinstance(panel._origination_rollup(), rx.Component)

    def test_portfolio_panel_constructs_with_rollup_wired_in(self) -> None:
        # The top-level page embeds the rollup under rx.cond; building it proves
        # the rollup is reachable from the real page component, not orphaned.
        assert isinstance(panel.portfolio_panel(), rx.Component)
