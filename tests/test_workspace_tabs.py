"""Verifier for Feature 9 Phase 1 — the borrower tabbed workspace state.

Reflex UI is hard to unit-test, so (per FEATURE9_META_INTERFACE_SPEC §9) this
focuses on the PURE, assertable parts of the meta-interface state contract:

- ``set_active_tab`` accepts the four valid tabs, ignores unknown values, and
  the default is ``"assessment"``.
- ``effective_tab`` returns the banker's explicit pick when pinned, else the
  phase-implied tab (lifecycle auto-focus) — a pure table-driven fallback.
- The tab keys stay enum-like and forward-compatible (they must map 1:1 onto
  the Phase-2 route segments), so the nav ``TABS`` order matches the state's
  ``_VALID_TABS`` set exactly.

These assertions run the underlying methods directly on a plain instance so no
Reflex event loop / browser is needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.frontend.components.workspace_tabs import TABS
from app.frontend.state import SaiseiUIState

from tests._bare_state import bare_ui_state


def _fget(var: Any, inst: SaiseiUIState) -> Any:
    """Invoke a computed ``rx.var``'s runtime getter (``.fget``)."""
    return var.fget(inst)


def _fn(handler: Any, *args: Any) -> Any:
    """Invoke an ``rx.event`` handler's underlying function (``.fn``)."""
    return handler.fn(*args)


#: The canonical valid-tab set, read from the state's own contract.
_VALID = set(SaiseiUIState._VALID_TABS)


def _fresh() -> SaiseiUIState:
    """Return a state instance with default tab fields (no Reflex runtime)."""
    inst = bare_ui_state()
    inst.active_tab = "assessment"
    inst.tab_pinned = False
    inst.phase = "idle"
    inst.keikakusho_draft = ""
    return inst


def test_default_tab_is_assessment() -> None:
    """The workspace opens on the Assessment tab."""
    assert _fresh().active_tab == "assessment"


def test_valid_tabs_are_exactly_the_four_altitudes() -> None:
    """The enum-like tab set is the four Feature 9 borrower altitudes."""
    assert {"assessment", "meeting", "plan", "audit"} == _VALID


@pytest.mark.parametrize("tab", sorted(_VALID))
def test_set_active_tab_accepts_valid(tab: str) -> None:
    """Each valid tab is accepted and pins the banker's choice."""
    inst = _fresh()
    _fn(SaiseiUIState.set_active_tab, inst, tab)
    assert inst.active_tab == tab
    assert inst.tab_pinned is True


@pytest.mark.parametrize("bad", ["", "portfolio", "ASSESSMENT", "examiner", "x"])
def test_set_active_tab_ignores_unknown(bad: str) -> None:
    """Unknown tab values are ignored (state unchanged, not pinned)."""
    inst = _fresh()
    _fn(SaiseiUIState.set_active_tab, inst, bad)
    assert inst.active_tab == "assessment"
    assert inst.tab_pinned is False


def test_effective_tab_prefers_pinned_choice() -> None:
    """Once the banker pins a tab, lifecycle auto-focus stops overriding it."""
    inst = _fresh()
    inst.active_tab = "audit"
    inst.tab_pinned = True
    inst.phase = "meeting"
    assert _fget(SaiseiUIState.effective_tab, inst) == "audit"


@pytest.mark.parametrize(
    ("phase", "draft", "expected"),
    [
        ("idle", "", "assessment"),
        ("assessing", "", "assessment"),
        ("meeting", "", "meeting"),
        ("awaiting_decision", "", "meeting"),
        ("drafting", "", "plan"),
        ("done", "計画書…", "plan"),
        ("done", "", "plan"),
    ],
)
def test_effective_tab_phase_fallback(phase: str, draft: str, expected: str) -> None:
    """Unpinned, the rendered tab follows the run lifecycle (auto-focus)."""
    inst = _fresh()
    inst.phase = phase
    inst.keikakusho_draft = draft
    assert _fget(SaiseiUIState.effective_tab, inst) == expected


def test_nav_tab_order_matches_state_contract() -> None:
    """The nav tab keys match the state's valid set (forward-compatible routes)."""
    nav_keys = [key for key, _ja, _en, _icon in TABS]
    assert nav_keys == list(SaiseiUIState._VALID_TABS)
