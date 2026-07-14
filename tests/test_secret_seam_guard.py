"""Offline guardrail: retire the raw-LLM/LangSmith-key secret-seam bug class.

The recurring bug (fixed at ``faithfulness``, ``feasibility``, ``_persona``,
``saisei_chat``, and ``observability``): a call site reads ``llm_api_key`` /
``langsmith_api_key`` DIRECTLY off ``Settings`` and interpolates it into an auth
header (``Authorization: Bearer ...`` / ``x-api-key``) WITHOUT routing through
the secret seam. On a seam-configured deployment (a ``@env:`` / ``@file:`` /
``@/path`` reference) the literal reference string is then sent as the token,
401s, and silently degrades a best-effort feature -- the worst kind of failure
for a regulated product, because offline / demo / CI (which use literals) stay
green and never surface it.

This static check walks the real ``app/`` tree and fails if any line embeds one
of those two secret fields in a header value without a ``resolve_secret(`` on
the SAME line. It is intentionally FIELD-SPECIFIC, not a blanket ban on Bearer
headers: the other API clients (``core_banking_client`` / ``hojin_bango`` /
``tdb_client``) legitimately build ``Bearer`` headers from DIFFERENT secret
fields and already wrap them in ``resolve_secret``, so they must not trip this.

The canonical, allowed way to build LLM auth is ``app.backend.llm`` --
``llm_configured`` / ``llm_auth_headers`` / ``resolved_llm_key`` -- which a new
call site should import instead of rolling its own header.

Fully offline: this is pure source inspection (no import, no network), mirroring
the ``test_prompt_registry_callsites`` guardrail idiom.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Repository ``app/`` root, resolved relative to this test file
#: (``<repo>/tests/test_secret_seam_guard.py`` -> ``<repo>/app``).
_APP_ROOT = Path(__file__).resolve().parent.parent / "app"

#: The two secret fields that gate the LLM-as-judge / persona / companion / RAG
#: embedding LLM calls and LangSmith tracing. These are the ONLY fields the
#: recurring bug touched; the other secrets (core_banking_api_key,
#: hojin_bango_app_id, tdb_api_key, audit_signing_*) have their own correct,
#: separately-tested call sites and are out of scope for this specific class.
_GUARDED_FIELDS: tuple[str, ...] = ("llm_api_key", "langsmith_api_key")

#: A line is SUSPECT when it both (a) builds an auth-header value and (b) names a
#: guarded secret field. We then require ``resolve_secret(`` on that same line;
#: its absence is the bug. Matching ``<word>.<field>`` (e.g. ``settings.``,
#: ``cfg.``, ``s.``) keeps the check robust to the local variable name.
_HEADER_HINT = re.compile(r"Bearer|x-api-key", re.IGNORECASE)
_GUARDED_ACCESS = re.compile(r"\.(?:" + "|".join(re.escape(f) for f in _GUARDED_FIELDS) + r")\b")


def _python_sources() -> list[Path]:
    """Every ``.py`` file under ``app/`` (the production surface)."""
    return sorted(_APP_ROOT.rglob("*.py"))


def test_app_root_is_discoverable() -> None:
    """Sanity: the walked tree exists and is non-empty (guards a bad path)."""
    assert _APP_ROOT.is_dir(), f"app root not found at {_APP_ROOT}"
    assert _python_sources(), "no Python sources discovered under app/"


def test_no_raw_llm_or_langsmith_key_in_auth_header() -> None:
    """No header embeds a guarded secret field without the secret seam.

    Fails with the exact file:line:source of every offending site so a
    regression is actionable, and points at the canonical fix.
    """
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not _HEADER_HINT.search(line):
                continue
            if not _GUARDED_ACCESS.search(line):
                continue
            if "resolve_secret(" in line:
                continue
            rel = path.relative_to(_APP_ROOT.parent)
            offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Raw llm_api_key / langsmith_api_key used in an auth header without the "
        "secret seam (resolve_secret). Build LLM auth via app.backend.llm "
        "(llm_auth_headers / llm_configured); resolve a LangSmith key with "
        "resolve_secret(...). Offending sites:\n  " + "\n  ".join(offenders)
    )
