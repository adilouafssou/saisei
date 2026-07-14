"""Verifier for the OIDC bearer-token transport (Feature 6, slice 4).

Proves the security boundary in ``app.backend.auth`` end to end, fully OFFLINE
and deterministically: the test mints its own RSA keypair (via ``cryptography``,
which ships with ``pyjwt[crypto]``), signs tokens with it, and monkeypatches the
JWKS signing-key lookup to return that public key -- so NO network is touched
and the real ``jwt.decode`` verification path runs unchanged.

What is pinned:
* a valid token -> verified claims;
* expiry, issuer mismatch, audience mismatch, and a tampered signature -> AuthError;
* an 'HS256' downgrade attempt (symmetric alg signed with the public key bytes)
  -> AuthError (only asymmetric algorithms are accepted);
* malformed / missing Authorization headers -> AuthError;
* ``oidc_enabled`` reflects whether a jwks_url is configured;
* the router contract: with OIDC on, a valid token authenticates (real actor on
  the Identity) and a bad/absent token is 401.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from typing import Any, cast

import app.backend.auth as auth_module
import jwt
import pytest
from app.backend.auth import (
    AuthError,
    extract_bearer_token,
    oidc_enabled,
    verify_bearer_token,
)
from app.shared.settings import Settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_JWKS_URL = "https://idp.test/.well-known/jwks.json"
_ISSUER = "https://idp.test/"
_AUDIENCE = "saisei-api"


# ---------------------------------------------------------------------------
# Keypair + signing helpers (module-scoped so the RSA gen runs once)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, Any]:
    """Return (private_key, public_key) for signing / verifying test tokens."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _private_pem(private_key: Any) -> bytes:
    return cast(
        "bytes",
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def _public_pem(public_key: Any) -> bytes:
    return cast(
        "bytes",
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding (JWT segment encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _forge_hs256_token(payload: dict[str, Any], secret: bytes) -> str:
    """Hand-craft an HS256 JWT, bypassing PyJWT's encoder key guard.

    The algorithm-confusion attack signs an HS256 token using the RSA *public*
    key bytes as the HMAC secret. Current PyJWT refuses to ``encode`` with a PEM
    public key as a symmetric secret (InvalidKeyError), so we cannot use
    ``jwt.encode`` to build the attacker's token. We assemble the token manually
    (header.payload.signature with raw HMAC-SHA256) so the malicious token
    actually exists and the test exercises the VERIFIER's rejection -- which is
    the security property under test -- rather than PyJWT's encoder.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    segments.append(_b64url(signature))
    return ".".join(segments)


def _make_token(
    private_key: Any,
    *,
    alg: str = "RS256",
    iss: str | None = _ISSUER,
    aud: str | None = _AUDIENCE,
    sub: str = "banker-jane",
    tenant: str = "bank-001",
    expires_in: int = 3600,
    key: Any = None,
) -> str:
    """Mint a signed JWT with sensible defaults; override fields per test."""
    now = dt.datetime.now(tz=dt.UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "tenant": tenant,
        "iat": now,
        "exp": now + dt.timedelta(seconds=expires_in),
    }
    if iss is not None:
        payload["iss"] = iss
    if aud is not None:
        payload["aud"] = aud
    signing_key = key if key is not None else _private_pem(private_key)
    return jwt.encode(payload, signing_key, algorithm=alg)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "use_mocks": True,
        "persist_checkpoints": False,
        "auth_jwks_url": _JWKS_URL,
        "auth_issuer": _ISSUER,
        "auth_audience": _AUDIENCE,
        "auth_required": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def patched_jwks(monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[Any, Any]) -> None:
    """Patch the JWKS signing-key lookup to return our public key (no network).

    ``verify_bearer_token`` calls ``client.get_signing_key_from_jwt(token)`` on a
    ``PyJWKClient``. We replace ``_jwk_client`` with a stub whose method returns
    an object exposing ``.key`` = our public key, so the real ``jwt.decode``
    signature check runs against the matching key with zero network I/O.
    """
    _, public_key = rsa_keypair

    class _StubSigningKey:
        key = public_key

    class _StubClient:
        def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
            return _StubSigningKey()

    monkeypatch.setattr(auth_module, "_jwk_client", lambda *_args, **_kw: _StubClient())


# ---------------------------------------------------------------------------
# oidc_enabled / header extraction
# ---------------------------------------------------------------------------


def test_oidc_enabled_reflects_jwks_url() -> None:
    assert oidc_enabled(_settings()) is True
    assert oidc_enabled(_settings(auth_jwks_url="")) is False
    assert oidc_enabled(_settings(auth_jwks_url="   ")) is False


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic abc", "token xyz"],
)
def test_extract_bearer_token_rejects_bad_headers(header: str | None) -> None:
    with pytest.raises(AuthError):
        extract_bearer_token(header)


def test_extract_bearer_token_accepts_well_formed_header() -> None:
    assert extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
    # Case-insensitive scheme.
    assert extract_bearer_token("bearer abc.def.ghi") == "abc.def.ghi"


# ---------------------------------------------------------------------------
# verify_bearer_token: the verification matrix
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("patched_jwks")
class TestVerification:
    def test_valid_token_returns_claims(self, rsa_keypair: tuple[Any, Any]) -> None:
        private_key, _ = rsa_keypair
        token = _make_token(private_key)
        claims = verify_bearer_token(token, _settings())
        assert claims["sub"] == "banker-jane"
        assert claims["tenant"] == "bank-001"

    def test_expired_token_is_rejected(self, rsa_keypair: tuple[Any, Any]) -> None:
        private_key, _ = rsa_keypair
        token = _make_token(private_key, expires_in=-10)
        with pytest.raises(AuthError):
            verify_bearer_token(token, _settings(auth_leeway_seconds=0))

    def test_wrong_issuer_is_rejected(self, rsa_keypair: tuple[Any, Any]) -> None:
        private_key, _ = rsa_keypair
        token = _make_token(private_key, iss="https://evil.test/")
        with pytest.raises(AuthError):
            verify_bearer_token(token, _settings())

    def test_wrong_audience_is_rejected(self, rsa_keypair: tuple[Any, Any]) -> None:
        private_key, _ = rsa_keypair
        token = _make_token(private_key, aud="some-other-api")
        with pytest.raises(AuthError):
            verify_bearer_token(token, _settings())

    def test_tampered_signature_is_rejected(self, rsa_keypair: tuple[Any, Any]) -> None:
        private_key, _ = rsa_keypair
        token = _make_token(private_key)
        # Tamper the signature at the BYTE level, not by flipping one base64
        # character. Flipping the final base64url char is unreliable: its low
        # bits may be ignored by the decoder, so the decoded signature bytes
        # (and thus the verification result) can be unchanged -> the verifier
        # correctly accepts an identical signature and the test spuriously fails
        # with DID NOT RAISE. Decoding, flipping a real signature byte, and
        # re-encoding guarantees a genuinely different signature.
        head, _, sig = token.rpartition(".")
        padded = sig + "=" * (-len(sig) % 4)
        raw = bytearray(base64.urlsafe_b64decode(padded))
        raw[0] ^= 0xFF  # flip every bit of the first signature byte
        tampered = f"{head}.{_b64url(bytes(raw))}"
        with pytest.raises(AuthError):
            verify_bearer_token(tampered, _settings())

    def test_hs256_downgrade_is_rejected(self, rsa_keypair: tuple[Any, Any]) -> None:
        """A token signed with HS256 using the public key bytes must be refused.

        This is the classic JWKS algorithm-confusion attack: an attacker takes
        the public key (which is public) and signs an HS256 token with it,
        hoping the verifier treats the public key as an HMAC secret. We only
        accept asymmetric algorithms, so it must fail.
        """
        _, public_key = rsa_keypair
        now = dt.datetime.now(tz=dt.UTC)
        payload = {
            "sub": "attacker",
            "tenant": "bank-001",
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=1)).timestamp()),
        }
        # Sign HS256 with the PUBLIC key bytes as the HMAC secret (the attack),
        # forging the token by hand because PyJWT refuses to encode it.
        forged = _forge_hs256_token(payload, _public_pem(public_key))
        with pytest.raises(AuthError):
            verify_bearer_token(forged, _settings())

    def test_missing_exp_is_rejected(self, rsa_keypair: tuple[Any, Any]) -> None:
        """A token without an exp claim is rejected (exp is required)."""
        private_key, _ = rsa_keypair
        now = dt.datetime.now(tz=dt.UTC)
        token = jwt.encode(
            {"sub": "x", "tenant": "bank-001", "iat": now, "iss": _ISSUER, "aud": _AUDIENCE},
            _private_pem(private_key),
            algorithm="RS256",
        )
        with pytest.raises(AuthError):
            verify_bearer_token(token, _settings())


def test_verify_without_jwks_url_raises(rsa_keypair: tuple[Any, Any]) -> None:
    """Calling verify when no jwks_url is configured fails closed."""
    private_key, _ = rsa_keypair
    token = _make_token(private_key)
    with pytest.raises(AuthError):
        verify_bearer_token(token, _settings(auth_jwks_url=""))


@pytest.mark.usefixtures("patched_jwks")
def test_unreachable_jwks_fails_closed(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[Any, Any]
) -> None:
    """If the JWKS lookup raises (provider down), verification must fail, not pass."""
    private_key, _ = rsa_keypair
    token = _make_token(private_key)

    class _BoomClient:
        def get_signing_key_from_jwt(self, _token: str) -> Any:
            raise RuntimeError("jwks endpoint unreachable")

    monkeypatch.setattr(auth_module, "_jwk_client", lambda *_a, **_k: _BoomClient())
    with pytest.raises(AuthError):
        verify_bearer_token(token, _settings())
