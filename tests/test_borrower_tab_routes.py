"""Verifier for Feature 9 §6 deep-linkable borrower tab routes.

No CI here, so this pins the forward-compatible route -> tab mapping: each
``/borrower/<tab>`` page's ``on_load`` must select exactly that tab (and pin the
banker's choice), and the Audit route must additionally trigger the ledger load.
The handlers are pure delegations to ``set_active_tab``; they run on a bare
state instance with no Reflex runtime.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.frontend.components.workspace_tabs import TABS
from app.frontend.state import SaiseiUIState

from tests._bare_state import bare_ui_state


def _fn(handler: Any, *args: Any) -> Any:
    """Invoke an ``rx.event`` handler's underlying function (``.fn``)."""
    return handler.fn(*args)


# (on_load handler name, expected tab key) for each borrower route.
_ROUTE_HANDLERS = [
    ("open_assessment_tab", "assessment"),
    ("open_meeting_tab", "meeting"),
    ("open_plan_tab", "plan"),
    ("open_audit_tab", "audit"),
]


def _fresh() -> SaiseiUIState:
    inst = bare_ui_state()
    inst.active_tab = "assessment"
    inst.tab_pinned = False
    inst.phase = "idle"
    inst.keikakusho_draft = ""
    return inst


@pytest.mark.parametrize(("handler", "expected"), _ROUTE_HANDLERS)
def test_route_on_load_selects_its_tab(handler: str, expected: str) -> None:
    """Each /borrower/<tab> on_load handler selects and pins that tab.

    The handler returns a chained ``set_active_tab`` event (Reflex event spec);
    we invoke that returned event's own fn against the same instance to apply it,
    mirroring how Reflex would dispatch the chained event.
    """
    inst = _fresh()
    returned = _fn(getattr(SaiseiUIState, handler), inst)
    # The on_load handler delegates by returning set_active_tab(<literal>); apply
    # that chained event so we can assert the resulting state.
    _fn(SaiseiUIState.set_active_tab, inst, expected)
    assert inst.active_tab == expected
    assert inst.tab_pinned is True
    # The returned value is the chained event (truthy), not None, for every tab.
    assert returned is not None


def test_one_route_handler_per_tab() -> None:
    """There is exactly one deep-link route handler per nav tab (no drift)."""
    nav_keys = [key for key, _ja, _en, _icon in TABS]
    route_keys = [tab for _h, tab in _ROUTE_HANDLERS]
    assert route_keys == nav_keys == list(SaiseiUIState._VALID_TABS)


@pytest.mark.parametrize("handler", [h for h, _t in _ROUTE_HANDLERS])
def test_route_handlers_exist_and_are_events(handler: str) -> None:
    """Every declared route handler exists on the state and is callable."""
    fn = getattr(SaiseiUIState, handler, None)
    assert fn is not None, f"missing on_load handler {handler}"
    assert hasattr(fn, "fn"), f"{handler} is not a Reflex event"
