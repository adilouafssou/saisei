"""Verifier for HITL audit-actor resolution via the identity seam (slice 2).

No CI here, so this pins the contract of ``_resolve_actor`` on the human-decision
audit path:

- an explicit ``actor`` / ``banker_id`` in the resume payload wins (and is
  trimmed), so a future authenticated caller can attribute the decision;
- otherwise it falls back to the identity seam (``current_actor``) — NOT a
  direct settings read — so identity has one source of truth and OIDC plugs in
  in one place;
- it never raises on the decision path, even if identity resolution blows up.

The function is pure (takes a plain dict), so no graph / Reflex / DB is needed.
"""

from __future__ import annotations

import app.backend.agents.turnaround_orchestrator as orch
import pytest
from app.backend.agents.turnaround_orchestrator import _resolve_actor


def test_explicit_actor_wins() -> None:
    assert _resolve_actor({"actor": "banker-jane"}) == "banker-jane"


def test_banker_id_is_honoured_as_alias() -> None:
    assert _resolve_actor({"banker_id": "emp-007"}) == "emp-007"


def test_explicit_actor_takes_precedence_over_banker_id() -> None:
    assert _resolve_actor({"actor": "a", "banker_id": "b"}) == "a"


def test_explicit_actor_is_trimmed() -> None:
    assert _resolve_actor({"actor": "  alice  "}) == "alice"


@pytest.mark.parametrize("payload", [{}, {"actor": ""}, {"actor": "   "}, {"banker_id": ""}])
def test_falls_back_to_identity_seam(
    payload: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit actor, the value comes from the identity seam.

    Patching the seam's current_actor (imported lazily inside _resolve_actor)
    proves the fallback routes through the seam and not a direct settings read.
    """
    import app.backend.identity as identity

    monkeypatch.setattr(identity, "current_actor", lambda *a, **k: "seam-banker")
    assert _resolve_actor(payload) == "seam-banker"


def test_fallback_never_raises_on_decision_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If identity resolution blows up, _resolve_actor degrades, never raises."""
    import app.backend.identity as identity

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("identity backend down")

    monkeypatch.setattr(identity, "current_actor", _boom)
    # Must not propagate — a broken identity lookup can never break a decision.
    assert _resolve_actor({}) == "banker"


def test_default_fallback_is_the_placeholder() -> None:
    """Unpatched, the fallback is the shipped placeholder actor ('banker')."""
    # Sanity: with the default settings, the seam yields 'banker'.
    assert _resolve_actor({}) == "banker"
    # Guard against the old direct-settings import sneaking back in.
    assert not hasattr(orch, "get_settings"), (
        "_resolve_actor must resolve identity via the seam, not a direct get_settings import"
    )
