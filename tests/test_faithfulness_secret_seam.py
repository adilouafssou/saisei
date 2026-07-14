"""Regression: the LLM-as-judge must read llm_api_key through the secret seam.

Same class of bug as the feasibility critic (!6): faithfulness._llm_configured
checked settings.llm_api_key directly and _judge_llm sent it verbatim as the
Bearer token. With a referenced key (@env:NAME) that (a) read as configured on
the literal reference and (b) 401'd, silently demoting every claim to the weaker
lexical proxy — quietly weakening the faithfulness guarantee on seam-configured
deployments. This pins that _llm_configured resolves the reference. Offline.
"""

from __future__ import annotations

from typing import cast

import app.backend.analysis.faithfulness as faithfulness
from app.backend.analysis.faithfulness import _llm_configured
from app.shared.settings import Settings


class _Cfg:
    def __init__(self, llm_api_key: str, llm_model: str = "gpt-x") -> None:
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model


def test_unresolvable_reference_is_not_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(faithfulness, "resolve_secret", lambda v: "")
    assert _llm_configured(cast("Settings", _Cfg("@env:MISSING"))) is False


def test_resolved_reference_is_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(faithfulness, "resolve_secret", lambda v: "sk-real" if v == "@env:K" else v)
    assert _llm_configured(cast("Settings", _Cfg("@env:K"))) is True


def test_no_model_is_not_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(faithfulness, "resolve_secret", lambda v: "sk-real")
    assert _llm_configured(cast("Settings", _Cfg("sk-real", llm_model=""))) is False


def test_plain_literal_still_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(faithfulness, "resolve_secret", lambda v: v)
    assert _llm_configured(cast("Settings", _Cfg("sk-literal"))) is True
