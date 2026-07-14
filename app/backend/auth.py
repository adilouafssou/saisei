"""OIDC bearer-token verification transport (Feature 6, slice 4).

This is the deployment-owned half of OIDC that the identity seam always pointed
to. ``app.backend.identity`` already maps VERIFIED claims to an
:class:`~app.backend.identity.Identity` (``identity_from_claims``) and enforces
the production guard (``require_persistable``). What was missing was the
TRANSPORT that turns a raw ``Authorization: Bearer <jwt>`` header into verified
claims: JWKS discovery, signature verification, and the standard temporal /
issuer / audience checks. That is exactly, and only, what this module does.

Design / safety posture
-----------------------
* **Real crypto, not hand-rolled.** Verification uses ``PyJWT`` with
  ``cryptography`` (``pyjwt[crypto]``) and its ``PyJWKClient`` for JWKS fetch +
  key caching + rotation. We never parse or compare signatures ourselves.
* **Strict by default.** When a token is presented we require a valid signature
  and a non-expired token; issuer and audience are additionally enforced
  whenever they are configured. A token that fails ANY enabled check is
  rejected with :class:`AuthError` -- there is no "best effort" fallback to an
  unverified identity once a caller has presented a token.
* **Offline-safe / opt-in.** With no ``auth_jwks_url`` configured this module is
  inert: :func:`oidc_enabled` returns ``False`` and the API keeps its existing
  placeholder-identity behaviour (itself gated by ``auth_required``). So
  ``make verify`` and the single-tenant demo are completely unaffected, and the
  network (JWKS fetch) is only ever touched when a deployment has explicitly
  pointed at its identity provider.

The verified claim set is handed straight to ``identity_from_claims`` by the
router, so this module knows nothing about tenants/actors -- it only proves the
token is authentic and returns its claims.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, cast

import jwt
from jwt import PyJWKClient

from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = ["AuthError", "oidc_enabled", "verify_bearer_token", "extract_bearer_token"]

_log = get_logger(__name__)

#: Signing algorithms we accept. Restricted to asymmetric RS/ES families so a
#: provider can never downgrade us to a symmetric 'HS*' (which would let anyone
#: holding the public JWKS key forge a token) -- and 'none' is impossible by
#: construction. This is the standard hardening for JWKS-based verification.
_ALLOWED_ALGORITHMS: tuple[str, ...] = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")


class AuthError(Exception):
    """Raised when a bearer token is missing, malformed, or fails verification.

    The router maps this to HTTP 401. The message is intentionally generic at
    the boundary (it does not leak which specific check failed to the caller),
    while the structured log records the precise reason for operators.
    """


def oidc_enabled(settings: Settings | None = None) -> bool:
    """Return whether real OIDC token verification is configured.

    True iff an ``auth_jwks_url`` is set. When False the transport is inert and
    the caller falls back to the existing placeholder-identity path (gated by
    ``auth_required``), so offline / demo deployments are unchanged.
    """
    settings = settings or get_settings()
    return bool((settings.auth_jwks_url or "").strip())


def extract_bearer_token(authorization_header: str | None) -> str:
    """Extract the raw JWT from an ``Authorization: Bearer <token>`` header.

    Args:
        authorization_header: The raw header value (or None when absent).

    Returns:
        The token string.

    Raises:
        AuthError: If the header is missing or not a well-formed Bearer header.
    """
    if not authorization_header:
        raise AuthError("missing Authorization header")
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("malformed Authorization header (expected 'Bearer <token>')")
    return parts[1].strip()


@lru_cache(maxsize=8)
def _jwk_client(jwks_url: str, cache_seconds: int) -> PyJWKClient:
    """Return a cached :class:`PyJWKClient` for a JWKS URL.

    ``PyJWKClient`` caches the fetched signing keys internally
    (``lifespan=cache_seconds``) so key material is reused across requests and
    refreshed on provider rotation. We additionally memoise the client object
    itself per (url, lifespan) so we do not rebuild it on every request. Keyed by
    both args so a settings change (e.g. in tests) yields a fresh client.
    """
    return PyJWKClient(jwks_url, cache_keys=True, lifespan=cache_seconds)


def verify_bearer_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    """Verify a JWT against the configured OIDC provider and return its claims.

    Performs full verification: the signature against the provider's JWKS public
    key, expiry / not-before (with the configured leeway), and -- whenever they
    are configured -- the issuer and audience. Only asymmetric algorithms are
    accepted (see :data:`_ALLOWED_ALGORITHMS`).

    Args:
        token: The raw JWT (as extracted from the Bearer header).
        settings: Optional settings override (defaults to cached settings).

    Returns:
        The decoded, verified claim set.

    Raises:
        AuthError: If verification fails for any reason. The structured log
            captures the precise cause; the raised message stays generic.
    """
    settings = settings or get_settings()
    jwks_url = (settings.auth_jwks_url or "").strip()
    if not jwks_url:
        # Defensive: callers should gate on oidc_enabled() first.
        raise AuthError("OIDC verification requested but no auth_jwks_url configured")

    issuer = (settings.auth_issuer or "").strip()
    audience = (settings.auth_audience or "").strip()

    # Only require/verify aud/iss when they are configured; pyjwt would otherwise
    # demand their presence. This keeps a permissive (but signature-checked)
    # posture available for providers that do not set them, while a production
    # deployment is expected to configure both.
    options: dict[str, Any] = {
        "require": ["exp"],
        "verify_signature": True,
        "verify_exp": True,
        "verify_aud": bool(audience),
        "verify_iss": bool(issuer),
    }

    try:
        client = _jwk_client(jwks_url, int(settings.auth_jwks_cache_seconds))
        signing_key = client.get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=list(_ALLOWED_ALGORITHMS),
            audience=audience or None,
            issuer=issuer or None,
            leeway=int(settings.auth_leeway_seconds),
            options=cast("Any", options),
        )
    except AuthError:
        raise
    except jwt.PyJWTError as exc:
        _log.warning("auth.token_rejected", reason=type(exc).__name__, detail=str(exc))
        raise AuthError("invalid token") from exc
    except Exception as exc:  # noqa: BLE001 - JWKS fetch / unexpected client errors
        # A JWKS endpoint that is unreachable / returns garbage must FAIL CLOSED:
        # we never admit an unverified token because the provider was down.
        _log.warning("auth.verification_error", reason=type(exc).__name__, detail=str(exc))
        raise AuthError("token verification unavailable") from exc

    return claims
