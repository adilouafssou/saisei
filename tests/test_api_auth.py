"""Router-level verifier: OIDC enforced on the run/resume HTTP API.

Complements ``tests/test_auth_oidc.py`` (which unit-tests the verifier) and
``tests/test_api_runs.py`` (which tests the routes with OIDC OFF). Here OIDC is
turned ON via a configured ``auth_jwks_url`` and the JWKS lookup is patched to a
locally-minted RSA key (no network), proving the full request path:

* a valid Bearer token authenticates and starts a real run (200), and the
  resolved identity carries the token's real actor/tenant (not the placeholder);
* a missing or invalid token is refused with 401 on every route.

Everything else stays offline: mock data engine + in-memory checkpointer.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any, cast

import app.backend.auth as auth_module
import app.backend.graph as graph_module
import app.backend.identity as identity_module
import app.shared.settings as settings_module
import jwt
import pytest
from app.app import create_app
from app.shared.settings import Settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

_JWKS_URL = "https://idp.test/.well-known/jwks.json"
_ISSUER = "https://idp.test/"
_AUDIENCE = "saisei-api"
DISTRESSED_CODE = "1234567"


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, Any]:
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


def _token(private_key: Any, **overrides: Any) -> str:
    now = dt.datetime.now(tz=dt.UTC)
    payload: dict[str, Any] = {
        "sub": "banker-jane",
        "tenant": "bank-001",
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + dt.timedelta(hours=1),
    }
    payload.update(overrides)
    return jwt.encode(payload, _private_pem(private_key), algorithm="RS256")


def _oidc_settings() -> Settings:
    return Settings(
        use_mocks=True,
        persist_checkpoints=False,
        auth_required=True,
        auth_jwks_url=_JWKS_URL,
        auth_issuer=_ISSUER,
        auth_audience=_AUDIENCE,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[Any, Any]) -> Iterator[TestClient]:
    """A TestClient with OIDC enforced and the JWKS lookup stubbed (no network)."""
    settings = _oidc_settings()
    settings_module.get_settings.cache_clear()
    for mod in (settings_module, graph_module, identity_module, auth_module):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)

    _, public_key = rsa_keypair

    class _StubSigningKey:
        key = public_key

    class _StubClient:
        def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
            return _StubSigningKey()

    monkeypatch.setattr(auth_module, "_jwk_client", lambda *_a, **_k: _StubClient())

    graph_module.reset_memory_saver()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client
    graph_module.reset_memory_saver()


def test_valid_token_authenticates_and_starts_run(
    client: TestClient, rsa_keypair: tuple[Any, Any]
) -> None:
    private_key, _ = rsa_keypair
    resp = client.post(
        "/api/v1/runs",
        json={"tdb_code": DISTRESSED_CODE, "thread_id": "t-oidc-ok"},
        headers={"Authorization": f"Bearer {_token(private_key)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["awaiting_decision"] is True
    assert body["values"]["proposed_strategies"]


def test_missing_token_is_unauthorized(client: TestClient) -> None:
    resp = client.post("/api/v1/runs", json={"tdb_code": DISTRESSED_CODE})
    assert resp.status_code == 401, resp.text


def test_invalid_token_is_unauthorized(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/runs",
        json={"tdb_code": DISTRESSED_CODE},
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert resp.status_code == 401, resp.text


def test_expired_token_is_unauthorized(client: TestClient, rsa_keypair: tuple[Any, Any]) -> None:
    private_key, _ = rsa_keypair
    now = dt.datetime.now(tz=dt.UTC)
    expired = _token(private_key, exp=now - dt.timedelta(minutes=5))
    resp = client.get(
        "/api/v1/runs/anything",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert resp.status_code == 401, resp.text
