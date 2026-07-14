"""Secret-resolution seam (productionise: one provider for every secret).

Secrets today are plain :class:`~app.shared.settings.Settings` fields
(``llm_api_key``, ``tdb_api_key``, the DSNs, the audit signing key, ...). Reading
them inline is fine for a single ``.env`` deployment, but a bank running in
production wants its secrets in a Vault / cloud secret manager, never on disk in
plaintext. This module is the ONE seam that makes that swap a config change, not
a code change: every secret is resolved through :func:`resolve_secret`, and a
real manager drops in behind the :class:`SecretProvider` protocol without
touching a single call site.

Reference convention
--------------------
A secret value is either a LITERAL or a REFERENCE. A reference is a string of the
form ``@scheme:locator`` that the provider dereferences at read time; anything
else (the default everywhere) is returned verbatim. The built-in
:class:`EnvFileSecretProvider` understands two dependency-free schemes that
already cover the common "keep it off disk / out of the committed env" needs:

* ``@env:NAME``  — read the secret from environment variable ``NAME`` (e.g. one
  injected by the orchestrator / k8s secret), so the value is never written into
  ``.env`` at all.
* ``@file:/path`` — read the secret from a file (e.g. a mounted Docker / k8s
  secret or a Vault Agent sidecar's rendered file), trimming a trailing newline.

A real ``@vault:...`` / ``@aws-sm:...`` / ``@gcp-sm:...`` scheme is the
operational drop-in: implement :class:`SecretProvider` for it and register it;
the reference convention and every call site stay identical.

Offline / default posture
-------------------------
A plain (non-``@``) value resolves to itself with ZERO I/O, so ``make verify``,
the demo, and every existing test are byte-for-byte unaffected — the seam only
does work when a deployment opts in by using a reference. A reference that cannot
be resolved raises :class:`SecretResolutionError` rather than silently returning
the raw reference string (which would leak ``@file:/...`` as if it were a key).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.shared.logging import get_logger

__all__ = [
    "SecretResolutionError",
    "SecretProvider",
    "EnvFileSecretProvider",
    "resolve_secret",
    "get_secret_provider",
    "set_secret_provider",
    "is_reference",
]

_log = get_logger(__name__)

#: The prefix that marks a settings value as a secret REFERENCE rather than a
#: literal. Plain values never start with this, so they pass straight through.
_REFERENCE_PREFIX = "@"


class SecretResolutionError(RuntimeError):
    """Raised when a secret reference is malformed or cannot be dereferenced.

    Deliberately a hard error: a deployment that asked for ``@file:/run/secret``
    must NOT silently fall back to using that literal string as the secret — that
    would send a path where a key belongs. Resolution failure is a
    misconfiguration the operator must see.
    """


def is_reference(value: str) -> bool:
    """Return whether ``value`` is a secret reference (``@scheme:locator``)."""
    return value.startswith(_REFERENCE_PREFIX)


@runtime_checkable
class SecretProvider(Protocol):
    """Resolves a (possibly-referenced) secret value to its effective value."""

    def resolve(self, value: str) -> str:
        """Return the effective secret for ``value`` (literal or dereferenced)."""
        ...


class EnvFileSecretProvider:
    """Default provider: passthrough literals; resolve ``@env:`` / ``@file:``.

    Dependency-free and offline-safe. A literal value is returned unchanged with
    no I/O. A reference is dereferenced by scheme; an unknown scheme or a missing
    target raises :class:`SecretResolutionError`.
    """

    def resolve(self, value: str) -> str:  # noqa: D102 - see class doc
        if not value or not is_reference(value):
            return value

        body = value[len(_REFERENCE_PREFIX) :]
        scheme, _, locator = body.partition(":")
        scheme = scheme.strip().lower()
        locator = locator.strip()
        if not locator:
            raise SecretResolutionError(f"secret reference has no locator: {value!r}")

        if scheme == "env":
            resolved = os.environ.get(locator)
            if resolved is None:
                raise SecretResolutionError(f"secret env var {locator!r} is not set")
            return resolved

        if scheme == "file":
            try:
                # Trim a single trailing newline (common when a secret file is
                # written with a terminating newline); preserve all other bytes.
                return Path(locator).read_text(encoding="utf-8").rstrip("\n")
            except OSError as exc:
                raise SecretResolutionError(f"cannot read secret file {locator!r}: {exc}") from exc

        raise SecretResolutionError(
            f"unknown secret reference scheme {scheme!r} in {value!r} (supported: env, file)"
        )


# ---------------------------------------------------------------------------
# Process-wide provider (swappable), mirroring the other seam singletons.
# ---------------------------------------------------------------------------

_PROVIDER: SecretProvider | None = None
_LOCK = threading.Lock()


def get_secret_provider() -> SecretProvider:
    """Return the process-wide secret provider (the env/file default)."""
    global _PROVIDER
    if _PROVIDER is None:
        with _LOCK:
            if _PROVIDER is None:
                _PROVIDER = EnvFileSecretProvider()
    return _PROVIDER


def set_secret_provider(provider: SecretProvider | None) -> None:
    """Install a custom secret provider (e.g. a Vault backend), or reset to None.

    This is the registration point a deployment uses to plug a real secret
    manager into the seam; passing ``None`` resets to the lazily-created default
    (used by tests for isolation).
    """
    global _PROVIDER
    with _LOCK:
        _PROVIDER = provider


def resolve_secret(value: str | None) -> str:
    """Resolve a (possibly-referenced) secret value to its effective value.

    The single helper every secret read should go through. A falsy value returns
    ``""`` (an unset secret stays unset); a literal returns unchanged; a
    ``@scheme:locator`` reference is dereferenced by the configured provider.

    Args:
        value: The raw settings value (literal, reference, None, or empty).

    Returns:
        The effective secret string (``""`` when unset).

    Raises:
        SecretResolutionError: If a reference cannot be resolved.
    """
    if not value:
        return ""
    return get_secret_provider().resolve(value)
