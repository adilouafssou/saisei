"""Tests for the LangSmith opt-in tracing and HITL feedback capture module.

Verifies the offline-by-default contract:
- configure_tracing() must be a strict no-op (no env vars set, no network calls)
  when either guard is false.
- capture_hitl_feedback() must be a strict no-op (returns False, no network
  calls) when tracing is not configured.
"""

from __future__ import annotations

import os

import pytest
from app.backend.observability import capture_hitl_feedback, configure_tracing
from app.shared.settings import Settings


def _make_settings(**kwargs: object) -> Settings:
    """Build a Settings instance with the given overrides (no .env file)."""
    return Settings.model_construct(**kwargs)  # type: ignore[arg-type]


def test_configure_tracing_disabled_by_default() -> None:
    """configure_tracing() must return False when langsmith_tracing=False."""
    s = _make_settings(
        langsmith_tracing=False,
        langsmith_api_key="sk-test",
        langsmith_project="saisei",
        langsmith_endpoint="https://api.smith.langchain.com",
    )
    result = configure_tracing(s)
    assert result is False


def test_configure_tracing_disabled_when_no_api_key() -> None:
    """configure_tracing() must return False when langsmith_api_key is empty."""
    s = _make_settings(
        langsmith_tracing=True,
        langsmith_api_key="",
        langsmith_project="saisei",
        langsmith_endpoint="https://api.smith.langchain.com",
    )
    result = configure_tracing(s)
    assert result is False


def test_configure_tracing_sets_env_vars_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """configure_tracing() must set LANGCHAIN_* env vars when both guards are true."""
    for key in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGCHAIN_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)

    s = _make_settings(
        langsmith_tracing=True,
        langsmith_api_key="sk-test-key",
        langsmith_project="my-project",
        langsmith_endpoint="https://custom.smith.langchain.com",
    )
    result = configure_tracing(s)

    assert result is True
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "sk-test-key"
    assert os.environ["LANGCHAIN_PROJECT"] == "my-project"
    assert os.environ["LANGCHAIN_ENDPOINT"] == "https://custom.smith.langchain.com"


def test_configure_tracing_no_env_vars_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """configure_tracing() must NOT set any LANGCHAIN_* env vars when disabled."""
    for key in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_PROJECT",
        "LANGCHAIN_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)

    s = _make_settings(
        langsmith_tracing=False,
        langsmith_api_key="sk-test-key",
        langsmith_project="saisei",
        langsmith_endpoint="https://api.smith.langchain.com",
    )
    configure_tracing(s)

    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ
    assert "LANGCHAIN_PROJECT" not in os.environ
    assert "LANGCHAIN_ENDPOINT" not in os.environ


def test_settings_langsmith_defaults() -> None:
    """LangSmith settings must default to offline-safe values."""
    s = Settings()
    assert s.langsmith_tracing is False
    assert s.langsmith_api_key == ""
    assert s.langsmith_project == "saisei"
    assert s.langsmith_endpoint == "https://api.smith.langchain.com"


# ---------------------------------------------------------------------------
# MR #2: capture_hitl_feedback offline-by-default contract.
# ---------------------------------------------------------------------------


def test_capture_hitl_feedback_noop_when_tracing_disabled() -> None:
    """capture_hitl_feedback() must return False when tracing is disabled."""
    s = _make_settings(
        langsmith_tracing=False,
        langsmith_api_key="[REDACTED]-key",
        langsmith_project="saisei",
        langsmith_endpoint="https://api.smith.langchain.com",
    )
    result = capture_hitl_feedback(
        tdb_code="1234567",
        decision="approve",
        strategies=[{"title": "test", "rationale": "r", "expected_keijo_uplift": 1}],
        settings=s,
    )
    assert result is False


def test_capture_hitl_feedback_noop_when_no_api_key() -> None:
    """capture_hitl_feedback() must return False when langsmith_api_key is empty."""
    s = _make_settings(
        langsmith_tracing=True,
        langsmith_api_key="",
        langsmith_project="saisei",
        langsmith_endpoint="https://api.smith.langchain.com",
    )
    result = capture_hitl_feedback(
        tdb_code="1234567",
        decision="revise",
        strategies=[],
        settings=s,
    )
    assert result is False


def test_capture_hitl_feedback_noop_by_default() -> None:
    """capture_hitl_feedback() must return False with default settings (offline)."""
    result = capture_hitl_feedback(
        tdb_code="1234567",
        decision="reject",
        strategies=[],
        settings=Settings(),
    )
    assert result is False


def test_capture_hitl_feedback_accepts_all_optional_fields() -> None:
    """capture_hitl_feedback() must accept all optional fields without error."""
    s = _make_settings(
        langsmith_tracing=False,
        langsmith_api_key="",
        langsmith_project="saisei",
        langsmith_endpoint="https://api.smith.langchain.com",
    )
    # Should not raise even with all optional fields populated.
    result = capture_hitl_feedback(
        tdb_code="1234567",
        decision="approve",
        strategies=[{"title": "t", "rationale": "r", "expected_keijo_uplift": 1}],
        approved_strategy={"title": "t", "rationale": "r", "expected_keijo_uplift": 1},
        revision_note=None,
        reconciliation_required=True,
        reconciliation_details=[
            {
                "strategy_title": "t",
                "deterministic_band": "high",
                "deterministic_score": 80.0,
                "llm_band": "low",
                "llm_score": 10.0,
                "band_distance": 2,
            }
        ],
        feasibility_notes=[
            {
                "strategy_title": "t",
                "achievability": "high",
                "achievability_score": 80.0,
                "rationale": "r",
                "advisory": "",
                "advisory_grounded": False,
            }
        ],
        fsa_classification="要注意先",
        working_capital_gap=-5_000_000,
        settings=s,
    )
    assert result is False  # tracing disabled -> no-op
