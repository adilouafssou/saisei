"""Tests for the optional LLM polish pass."""

from __future__ import annotations

import httpx
import pytest

import app.backend.nodes.kaizen_generation as polish_mod
from app.backend.nodes.kaizen_generation import polish_keikakusho
from app.shared.settings import Settings

_DRAFT = "# 経営改善計画書\n\n- 売上: ¥100,000,000\n"


def test_polish_noop_without_llm() -> None:
    settings = Settings(llm_api_key="", llm_model="")
    assert polish_keikakusho(_DRAFT, settings) == _DRAFT


def test_polish_falls_back_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(llm_api_key="fake-key", llm_model="some-model")

    def _boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(polish_mod.httpx, "post", _boom)
    assert polish_keikakusho(_DRAFT, settings) == _DRAFT


def test_polish_applies_when_llm_responds(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(llm_api_key="fake-key", llm_model="some-model")
    polished_text = _DRAFT + "\n\n_(refined)_\n"

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": polished_text}}]}

    def _ok(*args: object, **kwargs: object) -> _Resp:
        return _Resp()

    monkeypatch.setattr(polish_mod.httpx, "post", _ok)
    assert polish_keikakusho(_DRAFT, settings) == polished_text
