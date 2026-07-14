"""Caller identity seam (Feature 6, first slice — the OIDC plug point).

Real multi-tenant persistence (the opt-in Portfolio store) and the immutable
audit ledger both need a real caller identity: a ``tenant_id`` (which bank /
branch's book this is, the storage isolation key) and an ``actor`` (who took a
human decision, recorded on audit events). Until Feature 6's auth/OIDC lands,
those are configured PLACEHOLDERS — ``portfolio_tenant_default`` ("default") and
``audit_actor_default`` ("banker").

The problem this module fixes is not the placeholder values (those are correct
for a single-tenant demo); it is that identity was resolved in SCATTERED,
INCONSISTENT ways — the UI read ``settings.portfolio_tenant_default`` inline,
and the audit path defaulted the actor at the ``record_event`` signature. This
centralises both into ONE pure resolver so that, when OIDC arrives, wiring the
real banker/bank identity is a single-file change here — every call site already
flows through :func:`resolve_identity`.

Karpathy discipline: this slice is pure, offline, and BEHAVIOUR-PRESERVING — it
returns exactly the same placeholder strings the call sites used before, so no
verdict, figure, route, tenant scoping, or audit actor changes today. It only
establishes the seam (Spec → Verifier → Environment) that the real auth feature
plugs into next.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.shared.settings import get_settings

__all__ = [
    "Identity",
    "IdentityError",
    "SettingsLike",
    "resolve_identity",
    "identity_from_claims",
    "require_persistable",
    "current_tenant_id",
    "current_actor",
]


@runtime_checkable
class SettingsLike(Protocol):
    """Structural type for the identity-relevant settings fields.

    The identity functions only read a handful of attributes (via ``getattr``
    with safe fallbacks), so they accept anything that structurally exposes
    them. The real :class:`~app.shared.settings.Settings` satisfies this, and
    so can lightweight test doubles — without weakening the production contract
    to ``Any``.
    """

    portfolio_tenant_default: str
    audit_actor_default: str
    auth_required: bool
    auth_tenant_claim: str
    auth_actor_claim: str


class IdentityError(ValueError):
    """Raised when an identity cannot be established or is not permitted.

    Used by :func:`identity_from_claims` for malformed/insufficient OIDC claims
    and by :func:`require_persistable` when the production auth guard rejects an
    unauthenticated identity. A ``ValueError`` subclass so existing best-effort
    ``except Exception`` guards on the write paths still swallow it (the write is
    skipped), while callers that want to enforce auth can catch it explicitly.
    """


@dataclass(frozen=True)
class Identity:
    """The resolved caller identity for storage scoping and audit attribution.

    Attributes:
        tenant_id: The owning tenant (bank / branch). The isolation key for the
            tenant-scoped Portfolio store — one tenant can never read another's
            book. Today the configured placeholder; the real bank id under OIDC.
        actor: Who is acting. Recorded as the ``actor`` on human-decision audit
            events. Today the configured placeholder; the real banker id (e.g.
            an OIDC subject) under Feature 6.
        authenticated: Whether this identity came from a real auth context
            (True) or is the configured placeholder (False). False today; the
            flag exists so call sites and tests can already distinguish the two,
            and so a future production guard can refuse to persist under an
            unauthenticated identity if the bank requires it.
    """

    tenant_id: str
    actor: str
    authenticated: bool = False


def resolve_identity(settings: SettingsLike | None = None) -> Identity:
    """Resolve the current caller identity (the single OIDC plug point).

    Today this returns the configured placeholders
    (``portfolio_tenant_default`` / ``audit_actor_default``) with
    ``authenticated=False``, exactly reproducing the prior inline behaviour so
    nothing changes. When Feature 6 (auth/OIDC) lands, this is the ONE function
    that reads the authenticated session/token and returns the real bank +
    banker identity (``authenticated=True``); every call site already routes
    through here, so no other code needs to change.

    The resolved ids are coerced to non-empty strings, falling back to the
    safe placeholders, so a misconfigured-to-empty setting can never silently
    produce an empty tenant key (which would collapse tenant isolation).

    Args:
        settings: Optional settings override (defaults to cached settings).

    Returns:
        The resolved :class:`Identity`.
    """
    settings = settings or get_settings()
    tenant_id = (getattr(settings, "portfolio_tenant_default", "") or "").strip() or "default"
    actor = (getattr(settings, "audit_actor_default", "") or "").strip() or "banker"
    return Identity(tenant_id=tenant_id, actor=actor, authenticated=False)


def current_tenant_id(settings: SettingsLike | None = None) -> str:
    """Return just the resolved tenant id (storage-scoping convenience)."""
    return resolve_identity(settings).tenant_id


def current_actor(settings: SettingsLike | None = None) -> str:
    """Return just the resolved actor id (audit-attribution convenience)."""
    return resolve_identity(settings).actor


def identity_from_claims(
    claims: Mapping[str, Any], settings: SettingsLike | None = None
) -> Identity:
    """Map verified OIDC claims to an authenticated :class:`Identity`.

    This is the application-side half of OIDC. The transport/token-validation
    layer (provider discovery, JWKS signature check, expiry/audience checks) is
    deployment-owned and lives OUTSIDE this offline core; once it has VERIFIED a
    token it passes the decoded claim set here, and this function deterministically
    derives the ``(tenant_id, actor)`` the rest of the system already flows
    through — so wiring a real IdP needs no change beyond calling this.

    The claim names are configurable (``auth_tenant_claim`` / ``auth_actor_claim``)
    so the same adapter fits providers that namespace their claims differently.
    Both resolved values must be non-empty strings; a missing/blank required
    claim raises :class:`IdentityError` rather than silently degrading to a
    placeholder, because an authenticated identity with an empty tenant key would
    break tenant isolation.

    IMPORTANT: this function trusts that ``claims`` are ALREADY VERIFIED. It does
    NOT validate a signature or expiry — never call it on an unverified token.

    Args:
        claims: The decoded, already-verified OIDC claim set.
        settings: Optional settings override (defaults to cached settings).

    Returns:
        An authenticated :class:`Identity` (``authenticated=True``).

    Raises:
        IdentityError: If the configured tenant or actor claim is missing/blank.
    """
    settings = settings or get_settings()
    tenant_claim = (getattr(settings, "auth_tenant_claim", "") or "tenant").strip()
    actor_claim = (getattr(settings, "auth_actor_claim", "") or "sub").strip()

    tenant_id = str(claims.get(tenant_claim, "") or "").strip()
    actor = str(claims.get(actor_claim, "") or "").strip()

    if not tenant_id:
        raise IdentityError(f"OIDC claims missing a non-empty tenant claim {tenant_claim!r}")
    if not actor:
        raise IdentityError(f"OIDC claims missing a non-empty actor claim {actor_claim!r}")
    return Identity(tenant_id=tenant_id, actor=actor, authenticated=True)


def require_persistable(identity: Identity, settings: SettingsLike | None = None) -> Identity:
    """Return ``identity`` if it may persist/attribute, else raise (prod guard).

    When ``auth_required`` is True, a deployment has declared that real auth is
    mandatory, so an UNAUTHENTICATED (placeholder) identity must NOT be used to
    scope a persisted book or attribute an audit decision — doing so would write
    rows under the shared 'default' tenant / 'banker' actor, defeating isolation
    and attribution. This raises :class:`IdentityError` in that case.

    When ``auth_required`` is False (the default single-tenant demo posture) the
    placeholder identity is permitted and returned unchanged, so existing
    behaviour is untouched.

    Args:
        identity: The identity to check (typically from :func:`resolve_identity`).
        settings: Optional settings override (defaults to cached settings).

    Returns:
        The same ``identity`` when permitted.

    Raises:
        IdentityError: If ``auth_required`` and the identity is unauthenticated.
    """
    settings = settings or get_settings()
    if getattr(settings, "auth_required", False) and not identity.authenticated:
        raise IdentityError(
            "auth_required is set but the identity is unauthenticated "
            "(placeholder); refusing to persist/attribute under it"
        )
    return identity
