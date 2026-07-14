"""Deterministic resilience primitives for the live data clients (Feature 2).

Pure, dependency-free building blocks so the live-API clients gain production
resilience (retry-with-backoff + a circuit breaker) WITHOUT pulling in a new
dependency and WITHOUT a network requirement in the tests. Both primitives are
fully unit-testable offline:

* :func:`retry_with_backoff` drives a caller-supplied zero-arg function,
  retrying on a configured exception tuple with exponential backoff. The sleep
  function is injected (defaults to :func:`time.sleep`) so tests run instantly
  with a no-op sleep.
* :class:`CircuitBreaker` is an in-memory consecutive-failure breaker. After
  ``threshold`` consecutive failures it is OPEN (``allow()`` returns False) so
  the caller can short-circuit to its fallback without a network call; a single
  success resets it.

Neither primitive performs I/O itself; the client composes them around its live
HTTP call and keeps its existing offline-fallback contract.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.shared.logging import get_logger

__all__ = ["retry_with_backoff", "CircuitBreaker"]

_log = get_logger(__name__)


def retry_with_backoff[T](
    func: Callable[[], T],
    *,
    max_retries: int,
    base_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``func`` with exponential-backoff retries on the given exceptions.

    Args:
        func: Zero-arg callable to execute (the live request).
        max_retries: Additional attempts after the first (>= 0). Total attempts
            are ``max_retries + 1``.
        base_seconds: Base backoff delay; attempt ``n`` (0-indexed) sleeps
            ``base_seconds * 2**n`` before the next try. 0 disables the delay.
        retry_on: Exception types that trigger a retry. Any other exception
            propagates immediately.
        sleep: Sleep function (injected for tests; defaults to time.sleep).

    Returns:
        The value returned by ``func`` on the first successful attempt.

    Raises:
        BaseException: The last caught exception if every attempt fails, or any
            exception not in ``retry_on`` immediately.
    """
    attempts = max_retries + 1
    last_exc: BaseException | None = None
    for n in range(attempts):
        try:
            return func()
        except retry_on as exc:
            last_exc = exc
            if n < attempts - 1:
                delay = base_seconds * (2**n)
                _log.info("resilience.retry", attempt=n + 1, of=attempts, delay=delay)
                if delay > 0:
                    sleep(delay)
    # Exhausted all attempts; re-raise the last error for the caller's fallback.
    assert last_exc is not None  # noqa: S101 - loop ran at least once
    raise last_exc


class CircuitBreaker:
    """In-memory consecutive-failure circuit breaker.

    Starts CLOSED (calls allowed). Each :meth:`record_failure` increments a
    counter; once it reaches ``threshold`` the breaker is OPEN and
    :meth:`allow` returns ``False`` so the caller short-circuits to its
    fallback. Any :meth:`record_success` resets the counter and closes it.

    Deterministic and side-effect-free aside from its own counter, so it is
    fully testable without time or network.
    """

    def __init__(self, threshold: int) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self._threshold = threshold
        self._consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        """True when the breaker is open (too many consecutive failures)."""
        return self._consecutive_failures >= self._threshold

    def allow(self) -> bool:
        """Return True when a live call should be attempted (breaker closed)."""
        return not self.is_open

    def record_success(self) -> None:
        """Reset the failure counter (closes the breaker)."""
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """Increment the consecutive-failure counter."""
        self._consecutive_failures += 1
