"""Verifier for the secret-provider seam (platform productionisation).

No CI here, so this pins the seam's contract offline (no network / Vault):

- a plain literal -- the default everywhere -- passes through unchanged, so the
  offline / demo / test posture is unaffected;
- ``@env:NAME`` / ``@file:/path`` / ``@/path`` references dereference correctly,
  the ``@/path`` shorthand preserving the prior audit-signing PEM convention;
- an unresolvable reference degrades to ``""`` (the call site's offline path)
  rather than raising;
- a custom provider can be installed (the Vault / cloud drop-in point) and is
  used by ``resolve_secret``.

This is the guardrail that adding a real secret manager later stays a single
seam change and never alters the literal-passthrough behaviour call sites rely on.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from app.backend import secrets as secrets_mod
from app.backend.secrets import (
    EnvFileSecretProvider,
    get_secret_provider,
    resolve_secret,
    set_secret_provider,
)
from app.shared.settings import Settings


@pytest.fixture(autouse=True)
def _restore_provider() -> Iterator[None]:
    """Restore the default provider after any test that swaps it in."""
    original = get_secret_provider()
    yield
    set_secret_provider(original)


def test_plain_literal_passes_through_unchanged() -> None:
    """A non-@ value (the default everywhere) is returned verbatim."""
    assert resolve_secret("sk-literal-key") == "sk-literal-key"


def test_empty_value_passes_through() -> None:
    """Empty stays empty, preserving the call sites' empty -> offline contract."""
    assert resolve_secret("") == ""
    assert resolve_secret(None) == ""  # type: ignore[arg-type]


def test_env_reference_is_dereferenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """``@env:NAME`` resolves to the environment variable's value."""
    monkeypatch.setenv("SAISEI_TEST_SECRET", "from-env")
    assert resolve_secret("@env:SAISEI_TEST_SECRET") == "from-env"


def test_missing_env_reference_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable @env reference degrades to '' (offline path), not raise."""
    monkeypatch.delenv("SAISEI_TEST_MISSING", raising=False)
    assert resolve_secret("@env:SAISEI_TEST_MISSING") == ""


def test_file_reference_is_read_and_stripped(tmp_path: Path) -> None:
    """``@file:/path`` returns the stripped file contents."""
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("  file-secret\n", encoding="utf-8")
    assert resolve_secret(f"@file:{secret_file}") == "file-secret"


def test_at_path_shorthand_reads_file(tmp_path: Path) -> None:
    """``@/path`` shorthand reads the file (prior audit-signing PEM convention)."""
    pem_file = tmp_path / "key.pem"
    pem_file.write_text("PEM-BODY\n", encoding="utf-8")
    assert resolve_secret(f"@{pem_file}") == "PEM-BODY"


def test_missing_file_reference_degrades_to_empty(tmp_path: Path) -> None:
    """An unreadable file reference degrades to '' rather than raising."""
    missing = tmp_path / "does-not-exist.pem"
    assert resolve_secret(f"@file:{missing}") == ""


def test_default_provider_is_env_file_provider() -> None:
    """The process default is the offline env/file resolver."""
    assert isinstance(get_secret_provider(), EnvFileSecretProvider)


def test_custom_provider_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """A swapped-in provider (the Vault drop-in point) is used by resolve_secret."""

    class _StubVault:
        def resolve(self, value: str) -> str:
            return f"vault::{value}"

    set_secret_provider(_StubVault())
    assert resolve_secret("db-dsn") == "vault::db-dsn"
    assert secrets_mod.get_secret_provider().__class__.__name__ == "_StubVault"


def test_file_reference_enables_tdb_live_gate(tmp_path: Path) -> None:
    """End-to-end: a @file: TDB key reference enables the client's live gate.

    Pins that routing a data-source secret through the seam means a file/env
    reference activates the live path exactly like a literal would, so a
    deployment can keep the key out of .env entirely.
    """
    from app.backend.tools.tdb_client import TdbClient

    key_file = tmp_path / "tdb.key"
    key_file.write_text("live-tdb-key\n", encoding="utf-8")

    class _Cfg:
        tdb_api_key = f"@file:{key_file}"
        tdb_circuit_breaker_threshold = 5

    assert TdbClient(settings=cast("Settings", _Cfg())).live_enabled is True

    # An empty (offline default) key leaves the live gate closed.
    class _CfgEmpty:
        tdb_api_key = ""
        tdb_circuit_breaker_threshold = 5

    assert TdbClient(settings=cast("Settings", _CfgEmpty())).live_enabled is False
