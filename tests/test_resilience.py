"""Offline tests for the deterministic resilience primitives (Feature 2 slice 2).

Pure-logic tests: retry_with_backoff and CircuitBreaker perform no I/O, so they
are fully covered offline with an injected no-op sleep and a counting stub.
"""

from __future__ import annotations

import pytest
from app.backend.tools.resilience import CircuitBreaker, retry_with_backoff


class _Boom(Exception):
    """Retryable test exception."""


class _Other(Exception):
    """Non-retryable test exception."""


def _noop_sleep(_seconds: float) -> None:
    """No-op sleep so retry tests run instantly."""


# ---------------------------------------------------------------------------
# retry_with_backoff
# ---------------------------------------------------------------------------


def test_retry_returns_first_success_without_retry() -> None:
    calls = {"n": 0}

    def func() -> str:
        calls["n"] += 1
        return "ok"

    result = retry_with_backoff(
        func, max_retries=3, base_seconds=0.0, retry_on=(_Boom,), sleep=_noop_sleep
    )
    assert result == "ok"
    assert calls["n"] == 1


def test_retry_recovers_after_transient_failures() -> None:
    calls = {"n": 0}

    def func() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Boom
        return "recovered"

    result = retry_with_backoff(
        func, max_retries=3, base_seconds=0.0, retry_on=(_Boom,), sleep=_noop_sleep
    )
    assert result == "recovered"
    assert calls["n"] == 3


def test_retry_exhausts_and_reraises_last() -> None:
    calls = {"n": 0}

    def func() -> str:
        calls["n"] += 1
        raise _Boom

    with pytest.raises(_Boom):
        retry_with_backoff(
            func, max_retries=2, base_seconds=0.0, retry_on=(_Boom,), sleep=_noop_sleep
        )
    assert calls["n"] == 3  # first try + 2 retries


def test_retry_does_not_swallow_non_retryable() -> None:
    calls = {"n": 0}

    def func() -> str:
        calls["n"] += 1
        raise _Other

    with pytest.raises(_Other):
        retry_with_backoff(
            func, max_retries=5, base_seconds=0.0, retry_on=(_Boom,), sleep=_noop_sleep
        )
    assert calls["n"] == 1  # no retry on a non-retryable error


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


def test_breaker_starts_closed() -> None:
    cb = CircuitBreaker(threshold=3)
    assert cb.allow() is True
    assert cb.is_open is False


def test_breaker_opens_after_threshold_failures() -> None:
    cb = CircuitBreaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow() is True  # 2 < 3
    cb.record_failure()
    assert cb.is_open is True
    assert cb.allow() is False


def test_breaker_resets_on_success() -> None:
    cb = CircuitBreaker(threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open is True
    cb.record_success()
    assert cb.is_open is False
    assert cb.allow() is True


def test_breaker_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        CircuitBreaker(threshold=0)
