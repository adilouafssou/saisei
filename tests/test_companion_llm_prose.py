"""Verifier for the companion's optional LLM prose pass (B1, opt-in, fail-safe).

The deterministic, cited answer is always the source of truth; an LLM may only
*rephrase* it for readability, and only when configured. These tests pin the
safety contract that makes that acceptable in regulated lending:

- **Offline / unconfigured -> byte-identical deterministic answer** (so make
  verify / CI stay offline and replayable).
- **A rephrase that preserves grounding is accepted.**
- **A rephrase that REGRESSES grounding is rejected** — the answer falls back to
  the deterministic text. This is the numeric-preservation stance generalised to
  claims: the LLM improves prose, it can never weaken attribution.
- **Any LLM error falls back** to the deterministic answer.

The LLM call is patched (no network); the grounding-regression logic — the part
that actually protects the banker — is exercised directly.
"""

from __future__ import annotations

from typing import Any

import app.backend.agents.saisei_chat as chat
import pytest
from app.backend.agents.saisei_chat import answer_question
from app.backend.analysis.claim_grounding import UNVERIFIED_MARKER
from app.shared.models.classification import FsaClass
from app.shared.settings import Settings


class _StubProvider:
    def __init__(self, hits: list[Any] | None = None) -> None:
        self._hits = hits or []

    def search(self, query: str, top_k: int) -> list[Any]:
        return list(self._hits)


def _state() -> dict[str, Any]:
    return {
        "ews_score": 62.0,
        "fsa_classification": FsaClass.YOCHUISAKI,
        "working_capital_gap": -12_000_000,
        "hosho_kaijo_score": 48.0,
        "classification_reason": "EWS 62 が要注意の閾値を超えたため。",
    }


def _no_llm() -> Settings:
    return Settings(llm_api_key="", llm_model="")


def _with_llm() -> Settings:
    return Settings(llm_api_key="fake-key", llm_model="some-model")


def test_offline_answer_is_deterministic_baseline() -> None:
    """With no LLM configured, the answer equals the deterministic baseline."""
    ans = answer_question("EWSは？", _state(), provider=_StubProvider(), settings=_no_llm())
    assert ans.grounded is True
    assert "62.0" in ans.text
    assert UNVERIFIED_MARKER not in ans.text


def test_rephrase_preserving_citations_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite that keeps every citation tag is accepted (prose improved)."""

    def _fake_rephrase(settings: Any, text: str) -> str:
        # A natural-sounding rewrite that keeps all [..] tags + the deferral.
        return (
            "本件のEWSスコアは 62.0 となっています。[ews_score] "
            "これは助言です。最終的な判断は担当者にあります。 "
            "(Advisory only — the decision is yours.)"
        )

    monkeypatch.setattr(chat, "_call_rephrase_llm", _fake_rephrase)
    ans = answer_question("EWSは？", _state(), provider=_StubProvider(), settings=_with_llm())
    assert "ews_score" in ans.citations
    assert ans.grounded is True
    assert UNVERIFIED_MARKER not in ans.text


def test_rephrase_that_regresses_grounding_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite that drops citations / invents a claim is rejected (fail-safe).

    The malicious/sloppy rewrite strips the citation tag and adds an uncited
    assertion. Re-grounding finds more ungrounded claims than the deterministic
    baseline, so the answer must fall back to the deterministic text — which is
    fully grounded and carries no unverified marker.
    """

    def _bad_rephrase(settings: Any, text: str) -> str:
        return (
            "この企業は完全に安全です。今すぐ承認すべきです。 "  # uncited, invented
            "EWSは良好です。"  # dropped the [ews_score] tag
        )

    monkeypatch.setattr(chat, "_call_rephrase_llm", _bad_rephrase)
    ans = answer_question("EWSは？", _state(), provider=_StubProvider(), settings=_with_llm())
    # Fell back to the deterministic answer: grounded, cites the signal, and the
    # invented "完全に安全" sentence never reaches the banker.
    assert ans.grounded is True
    assert "ews_score" in ans.citations
    assert "完全に安全" not in ans.text
    assert UNVERIFIED_MARKER not in ans.text


def test_rephrase_error_falls_back_to_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any LLM error falls back to the deterministic answer (best-effort)."""

    def _boom(settings: Any, text: str) -> str:
        raise RuntimeError("llm down")

    monkeypatch.setattr(chat, "_call_rephrase_llm", _boom)
    ans = answer_question("EWSは？", _state(), provider=_StubProvider(), settings=_with_llm())
    assert ans.grounded is True
    assert "62.0" in ans.text
