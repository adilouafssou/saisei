"""Verifier for the caller identity seam (Feature 6, first slice).

No CI here, so this pins the seam's contract on plain settings objects (no
Reflex runtime / network / DB):

- it returns the configured placeholders today, with authenticated=False, so the
  slice is behaviour-preserving;
- empty/whitespace-only settings can never collapse to an empty tenant key
  (which would break tenant isolation) — they fall back to the safe defaults;
- the convenience accessors agree with the full resolver.

The Identity is the single OIDC plug point; these tests are the guardrail that
the plug point keeps its safe defaults when auth wiring is added later.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from app.backend.identity import (
    Identity,
    IdentityError,
    current_actor,
    current_tenant_id,
    identity_from_claims,
    require_persistable,
    resolve_identity,
)


class _FakeSettings:
    """Minimal settings stand-in exposing the identity + auth fields."""

    def __init__(
        self,
        tenant: str = "default",
        actor: str = "banker",
        *,
        auth_required: bool = False,
        auth_tenant_claim: str = "tenant",
        auth_actor_claim: str = "sub",
    ) -> None:
        self.portfolio_tenant_default = tenant
        self.audit_actor_default = actor
        self.auth_required = auth_required
        self.auth_tenant_claim = auth_tenant_claim
        self.auth_actor_claim = auth_actor_claim


def test_resolves_configured_placeholders() -> None:
    """The seam returns the configured tenant/actor, unauthenticated, today."""
    ident = resolve_identity(_FakeSettings("bank-001", "banker-jane"))
    assert ident == Identity(tenant_id="bank-001", actor="banker-jane", authenticated=False)


def test_default_placeholders_match_prior_inline_behaviour() -> None:
    """With the shipped defaults, the seam yields the exact prior placeholders."""
    ident = resolve_identity(_FakeSettings("default", "banker"))
    assert ident.tenant_id == "default"
    assert ident.actor == "banker"
    assert ident.authenticated is False


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_tenant_falls_back_to_safe_default(blank: str) -> None:
    """An empty/whitespace tenant never collapses isolation; it falls back."""
    ident = resolve_identity(_FakeSettings(blank, blank))
    assert ident.tenant_id == "default"
    assert ident.actor == "banker"


def test_values_are_stripped() -> None:
    """Surrounding whitespace is trimmed so keys are stable."""
    ident = resolve_identity(_FakeSettings("  bank-002  ", "  alice  "))
    assert ident.tenant_id == "bank-002"
    assert ident.actor == "alice"


def test_convenience_accessors_agree_with_resolver() -> None:
    s = _FakeSettings("bank-003", "bob")
    assert current_tenant_id(s) == resolve_identity(s).tenant_id == "bank-003"
    assert current_actor(s) == resolve_identity(s).actor == "bob"


def test_identity_is_immutable() -> None:
    """Identity is frozen so a resolved identity can't be mutated downstream."""
    ident = resolve_identity(_FakeSettings("bank-004", "carol"))
    with pytest.raises(FrozenInstanceError):
        ident.tenant_id = "other"  # type: ignore[misc]


# --- identity_from_claims: the OIDC adapter (slice 3) ---------------------


def test_claims_map_to_authenticated_identity() -> None:
    """Verified claims map to an authenticated Identity via the configured claims."""
    s = _FakeSettings()
    ident = identity_from_claims({"sub": "jane@bank", "tenant": "bank-001"}, s)
    assert ident == Identity(tenant_id="bank-001", actor="jane@bank", authenticated=True)


def test_claims_respect_custom_claim_names() -> None:
    """Custom claim names are honoured so namespaced providers fit the adapter."""
    s = _FakeSettings(auth_tenant_claim="org", auth_actor_claim="email")
    ident = identity_from_claims({"email": "bob@bank", "org": "bank-002"}, s)
    assert ident.tenant_id == "bank-002"
    assert ident.actor == "bob@bank"
    assert ident.authenticated is True


def test_claims_values_are_stripped() -> None:
    s = _FakeSettings()
    ident = identity_from_claims({"sub": "  bob  ", "tenant": "  bank-3  "}, s)
    assert ident.tenant_id == "bank-3"
    assert ident.actor == "bob"


@pytest.mark.parametrize(
    "claims",
    [{}, {"sub": "x"}, {"sub": "x", "tenant": ""}, {"sub": "x", "tenant": "  "}],
)
def test_missing_tenant_claim_raises(claims: dict[str, str]) -> None:
    """A missing/blank tenant claim raises, never silently degrades to default."""
    with pytest.raises(IdentityError):
        identity_from_claims(claims, _FakeSettings())


@pytest.mark.parametrize("claims", [{"tenant": "bank-1"}, {"tenant": "bank-1", "sub": ""}])
def test_missing_actor_claim_raises(claims: dict[str, str]) -> None:
    """A missing/blank actor claim raises."""
    with pytest.raises(IdentityError):
        identity_from_claims(claims, _FakeSettings())


# --- require_persistable: the production guard (slice 3) ------------------


def test_guard_off_allows_placeholder_identity() -> None:
    """With auth_required off (default), the placeholder identity is permitted."""
    s = _FakeSettings(auth_required=False)
    ident = resolve_identity(s)
    assert require_persistable(ident, s) is ident


def test_guard_on_rejects_unauthenticated_identity() -> None:
    """With auth_required on, an unauthenticated placeholder identity is refused."""
    s = _FakeSettings(auth_required=True)
    ident = resolve_identity(s)  # authenticated=False
    with pytest.raises(IdentityError):
        require_persistable(ident, s)


def test_guard_on_allows_authenticated_identity() -> None:
    """With auth_required on, a real authenticated identity passes the guard."""
    s = _FakeSettings(auth_required=True)
    ident = identity_from_claims({"sub": "jane", "tenant": "bank-1"}, s)
    assert require_persistable(ident, s) is ident


def test_identity_error_is_a_value_error() -> None:
    """IdentityError subclasses ValueError so best-effort except-guards skip it."""
    assert issubclass(IdentityError, ValueError)
