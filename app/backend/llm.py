"""Single chokepoint for LLM configuration + auth, through the secret seam.

Every LLM call site in the codebase needs the same two things:

* a truthiness gate — "is an LLM configured?" — and
* an ``Authorization: Bearer <key>`` header,

and BOTH must read ``llm_api_key`` through :func:`app.backend.secrets.resolve_secret`
so a ``@env:`` / ``@file:`` / ``@/path`` reference (the whole point of the secret
seam) resolves to its real value before use. Historically each call site rolled
its own configured-gate and Authorization header from the raw setting WITHOUT
the ``resolve_secret`` step — so a referenced key read as "configured" but was
sent as the literal reference string, 401'd, and silently degraded a best-effort
feature. (The exact offending shape is pinned by ``tests/test_secret_seam_guard``
rather than reproduced here, so this very docstring does not trip that guard.)

This module is the ONE place that knowledge lives. Call sites import
:func:`llm_configured` and :func:`llm_auth_headers` and can no longer bypass the
seam by construction. Offline / literal keys are unaffected (``resolve_secret``
passes plain values straight through).
"""

from __future__ import annotations

from app.backend.secrets import resolve_secret
from app.shared.settings import Settings, get_settings

__all__ = ["llm_configured", "llm_auth_headers", "resolved_llm_key"]


def resolved_llm_key(settings: Settings | None = None) -> str:
    """Return the LLM API key resolved through the secret seam ("" when unset).

    A literal key passes through unchanged; a ``@env:`` / ``@file:`` / ``@/path``
    reference is dereferenced. An unresolvable reference resolves to "".
    """
    cfg = settings or get_settings()
    return resolve_secret(cfg.llm_api_key)


def llm_configured(settings: Settings | None = None) -> bool:
    """Return whether an LLM is configured (resolved key present AND a model set).

    The single source of truth for the "is an LLM available?" gate. Reads the key
    through the secret seam, so a referenced key is correctly recognised as
    configured only when it actually resolves — never on the literal reference
    string alone.
    """
    cfg = settings or get_settings()
    return bool(resolved_llm_key(cfg) and cfg.llm_model)


def llm_auth_headers(settings: Settings | None = None) -> dict[str, str]:
    """Return the ``Authorization`` header for an LLM call (seam-resolved key).

    The ONLY supported way to build the Bearer header for an OpenAI-compatible
    endpoint, so no call site can accidentally send an unresolved secret
    reference as the token.
    """
    return {"Authorization": f"Bearer {resolved_llm_key(settings)}"}
