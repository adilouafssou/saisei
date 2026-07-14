"""Verifier for the shared LLM config/auth chokepoint (app.backend.llm).

Pins that llm_configured and llm_auth_headers route the key through the secret
seam, so a @env:/@file:/@/path reference resolves before use and a literal
passes through. Fully offline (resolve_secret is monkeypatched).
"""

from __future__ import annotations

from typing import cast

import app.backend.llm as llm
import pytest
from app.backend.llm import llm_auth_headers, llm_configured, resolved_llm_key
from app.shared.settings import Settings


class _Cfg:
    def __init__(self, llm_api_key: str, llm_model: str = "gpt-x") -> None:
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model


def test_unresolvable_reference_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm, "resolve_secret", lambda v: "")
    assert llm_configured(cast("Settings", _Cfg("@env:MISSING"))) is False


def test_resolved_reference_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm, "resolve_secret", lambda v: "sk-real" if v == "@env:K" else v)
    cfg = cast("Settings", _Cfg("@env:K"))
    assert llm_configured(cfg) is True
    assert resolved_llm_key(cfg) == "sk-real"
    assert llm_auth_headers(cfg) == {"Authorization": "Bearer sk-real"}


def test_no_model_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "resolve_secret", lambda v: "sk-real")
    assert llm_configured(cast("Settings", _Cfg("sk-real", llm_model=""))) is False


def test_plain_literal_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "resolve_secret", lambda v: v)
    cfg = cast("Settings", _Cfg("sk-literal"))
    assert llm_configured(cfg) is True
    assert llm_auth_headers(cfg) == {"Authorization": "Bearer sk-literal"}
