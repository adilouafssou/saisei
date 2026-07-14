"""Secret-provider seam (platform productionisation).

Every secret in the system is a plain field on
:class:`~app.shared.settings.Settings` (e.g. ``llm_api_key``, ``tdb_api_key``,
``audit_signing_private_key``, the various DSNs), read directly at many call
sites. That is correct for the offline / demo posture, where the value is a
literal, but it gives a real deployment no single place to swap ``.env`` for a
Vault / cloud secret manager.

This module is that single place. A secret is resolved through ONE provider
interface, so a deployment can keep secrets out of ``.env`` today (point a
setting at a mounted file or another env var) and drop a Vault / cloud backend
in later WITHOUT touching any call site.

Reference convention
--------------------
A settings value is either a literal secret (returned unchanged) or a
``@``-prefixed REFERENCE that the provider dereferences:

* ``@env:NAME``      -> the value of environment variable ``NAME``
* ``@file:/path``    -> the (stripped) contents of the file at ``/path``
* ``@/path``         -> shorthand for ``@file:/path`` (a leading ``@`` followed
                       by a path), preserving the prior ``audit_signing_*``
                       ``@/path`` PEM convention so existing config keeps working

Anything that is not a recognised reference -- including every plain literal,
which is the default everywhere -- is returned unchanged. This makes the seam
fully backward compatible: offline / demo / tests, which use literals, are
unaffected and make zero filesystem / env lookups beyond the literal itself.

Safety posture
--------------
* **Passthrough default.** Empty and plain values pass straight through, so the
  offline contract (empty -> mock) of the call sites is preserved exactly.
* **Best-effort resolution.** A reference that cannot be resolved (missing env
  var / unreadable file) logs and returns ``""`` rather than raising, so a
  misconfigured secret reference degrades to the same ``empty -> offline`` path
  the call site already handles, instead of breaking the workflow.
* **Offline + dependency-free.** No network, no third-party SDK; the Vault /
  cloud backend is the documented operational drop-in via
  :func:`set_secret_provider`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.shared.logging import get_logger

__all__ = [
    "SecretProvider",
    "EnvFileSecretProvider",
    "resolve_secret",
    "get_secret_provider",
    "set_secret_provider",
]

_log = get_logger(__name__)

#: Prefix marking a settings value as a reference rather than a literal secret.
_REF_PREFIX = "@"
#: Explicit reference schemes (``@env:`` / ``@file:``); ``@/path`` is shorthand.
_ENV_SCHEME = "env:"
_FILE_SCHEME = "file:"


@runtime_checkable
class SecretProvider(Protocol):
    """Seam that turns a configured secret value into its effective secret.

    Implementations receive the value already present in ``Settings`` and return
    the secret to actually use. The default resolves ``@``-references; a Vault /
    cloud implementation would instead look the secret up in its backend (it may
    still treat a plain literal as a passthrough so mixed config keeps working).
    """

    def resolve(self, value: str) -> str:
        """Return the effective secret for the configured ``value``."""
        ...


class EnvFileSecretProvider:
    """Default provider: dereference ``@env:`` / ``@file:`` / ``@/path`` refs.

    A plain (non-``@``) value -- the default everywhere -- is returned unchanged,
    so this provider is a strict passthrough for the offline / demo posture and
    only does work when a deployment opts in by configuring a reference.
    """

    def resolve(self, value: str) -> str:
        """Resolve ``value`` to its effective secret.

        Args:
            value: The configured settings value (literal secret or reference).

        Returns:
            The literal value unchanged when it is not a reference; otherwise the
            dereferenced secret, or ``""`` when the reference cannot be resolved
            (logged best-effort, so it degrades to the call site's offline path).
        """
        if not value or not value.startswith(_REF_PREFIX):
            return value

        body = value[len(_REF_PREFIX) :]
        try:
            if body.startswith(_ENV_SCHEME):
                name = body[len(_ENV_SCHEME) :].strip()
                return os.environ.get(name, "")
            if body.startswith(_FILE_SCHEME):
                return self._read_file(body[len(_FILE_SCHEME) :].strip())
            # ``@/path`` (or ``@path``) shorthand for ``@file:`` -- preserves the
            # prior audit-signing PEM ``@/path`` convention.
            return self._read_file(body.strip())
        except Exception as exc:  # noqa: BLE001 - degrade to offline, never break
            _log.warning("secrets.resolve_failed", reference=value, error=str(exc))
            return ""

    @staticmethod
    def _read_file(path: str) -> str:
        """Return the stripped contents of ``path`` (best-effort caller-guarded)."""
        if not path:
            return ""
        return Path(path).read_text(encoding="utf-8").strip()


#: Process-wide provider singleton. Defaults to the offline env/file resolver;
#: a deployment swaps in a Vault / cloud backend via ``set_secret_provider``.
_PROVIDER: SecretProvider = EnvFileSecretProvider()


def get_secret_provider() -> SecretProvider:
    """Return the active secret provider (the env/file resolver by default)."""
    return _PROVIDER


def set_secret_provider(provider: SecretProvider) -> None:
    """Install ``provider`` as the process-wide secret provider (Vault drop-in).

    This is the single wiring point a deployment uses to route every secret read
    through a Vault / cloud secret manager: implement :class:`SecretProvider`
    and call this once at startup. No call site changes -- they already read
    through :func:`resolve_secret`.
    """
    global _PROVIDER
    _PROVIDER = provider


def resolve_secret(value: str) -> str:
    """Resolve a configured secret ``value`` through the active provider.

    The one helper every secret consumer calls. For the default provider and a
    plain literal (the default everywhere) this returns ``value`` unchanged, so
    it is safe to wrap any existing secret read with it.

    Args:
        value: The configured settings value (literal secret or reference).

    Returns:
        The effective secret to use.
    """
    return _PROVIDER.resolve(value or "")
