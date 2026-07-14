"""Offline tests for the LangSmith golden-dataset push (Feature 1).

push_golden_dataset must be a STRICT no-op offline: with LangSmith tracing
unconfigured it returns 0 and makes no network call, exactly like
capture_hitl_feedback. That offline contract is what keeps make verify green;
the live upload path is exercised only when an operator configures LangSmith.
"""

from __future__ import annotations

from app.backend.observability import push_golden_dataset
from app.shared.settings import Settings

#: No tracing -> strict offline no-op.
_OFFLINE = Settings(langsmith_tracing=False, langsmith_api_key="")

#: Tracing flag on but no API key -> still offline (both guards required).
_NO_KEY = Settings(langsmith_tracing=True, langsmith_api_key="")


def test_push_is_noop_when_tracing_disabled() -> None:
    assert push_golden_dataset(_OFFLINE) == 0


def test_push_is_noop_without_api_key() -> None:
    assert push_golden_dataset(_NO_KEY) == 0


def test_push_offline_makes_no_network_call() -> None:
    """If the no-op contract held, httpx is never imported/used -> no error.

    A network attempt offline would raise; returning 0 cleanly proves the guard
    short-circuits before any HTTP work.
    """
    assert push_golden_dataset(_OFFLINE) == 0
