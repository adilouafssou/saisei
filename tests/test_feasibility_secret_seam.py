"""Regression: the feasibility critic must read llm_api_key via the secret seam.

The secret-seam wiring (!1/!2) routed llm_api_key through resolve_secret in
kaizen_generation and embeddings, but missed the feasibility critic. With a
referenced key (e.g. ``@env:NAME``) the old code (a) treated the literal
reference as "configured" and (b) sent it verbatim as the Bearer token — a 401
that silently disabled the advisory AND the LLM-vs-floor reconciliation gate.

These pin that _llm_configured resolves the reference, fully offline (no HTTP).
"""

from __future__ import annotations

from typing import cast

from app.backend.nodes.critics import feasibility
from app.backend.nodes.critics.feasibility import _llm_configured
from app.shared.settings import Settings


class _Cfg:
    def __init__(self, llm_api_key: str, llm_model: str = "gpt-x") -> None:
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model


def test_unresolvable_reference_is_not_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A reference that resolves to empty must read as NOT configured."""
    monkeypatch.setattr(feasibility, "resolve_secret", lambda v: "")
    assert _llm_configured(cast("Settings", _Cfg("@env:MISSING_KEY"))) is False


def test_resolved_reference_is_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A reference that resolves to a real key must read as configured."""
    monkeypatch.setattr(feasibility, "resolve_secret", lambda v: "sk-real" if v == "@env:K" else v)
    assert _llm_configured(cast("Settings", _Cfg("@env:K"))) is True


def test_no_model_is_not_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Even a resolved key is not configured without a model."""
    monkeypatch.setattr(feasibility, "resolve_secret", lambda v: "sk-real")
    assert _llm_configured(cast("Settings", _Cfg("sk-real", llm_model=""))) is False


def test_plain_literal_still_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A plain literal passes through resolve_secret unchanged (back-compat)."""
    monkeypatch.setattr(feasibility, "resolve_secret", lambda v: v)
    assert _llm_configured(cast("Settings", _Cfg("sk-literal"))) is True
