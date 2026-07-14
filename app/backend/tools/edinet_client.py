"""EDINET macro client with a deterministic offline fallback.

Feature 2 (NEXT_STEPS.md), follow-on client. Provides the BOJ rate curve and
settlement metrics behind the SAME ``MockDataProvider`` seam. Mirrors
:class:`~app.backend.tools.boj_macro.BojRateClient`:

* The deterministic :class:`~app.backend.tools.boj_macro.EdinetMacroMockClient`
  is the DEFAULT and fallback. With no base URL configured the client is a pure
  pass-through to the mock and never touches the network.
* The LIVE rate-curve lookup runs ONLY when ``Settings.edinet_base_url`` is
  set, wrapped in retry-with-backoff + a circuit breaker. On any error it
  degrades to the mock curve and never raises.
* Settlement metrics (T+1/T+2 liquidity, DSO/DPO) are bank-internal and remain
  mock-only, exactly as in BojRateClient.

The live HTTP/parse branch is config-gated, excluded from coverage, and marked
``VERIFY``.
"""

from __future__ import annotations

import datetime as dt

import httpx

from app.backend.tools.boj_macro import (
    EdinetMacroMockClient,
    RatePoint,
    SettlementMetrics,
)
from app.backend.tools.resilience import CircuitBreaker, retry_with_backoff
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = ["EdinetMacroClient"]

_log = get_logger(__name__)


class EdinetMacroClient:
    """EDINET macro client: live rate curve when configured, mock otherwise.

    Drop-in replacement for
    :class:`~app.backend.tools.boj_macro.EdinetMacroMockClient` behind the
    provider seam: exposes ``get_rate_curve`` and ``get_settlement_metrics``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._mock = EdinetMacroMockClient()
        self._breaker = CircuitBreaker(self._settings.edinet_circuit_breaker_threshold)

    @property
    def live_enabled(self) -> bool:
        """True when an EDINET base URL is configured."""
        return bool(self._settings.edinet_base_url)

    def get_rate_curve(self) -> list[RatePoint]:
        """Return the rate curve (live when configured, else the mock)."""
        if not self.live_enabled:
            return self._mock.get_rate_curve()
        if not self._breaker.allow():
            _log.warning("edinet.circuit_open")
            return self._mock.get_rate_curve()
        s = self._settings
        try:
            curve = retry_with_backoff(
                self._fetch_curve,
                max_retries=s.edinet_max_retries,
                base_seconds=s.edinet_backoff_base_seconds,
                retry_on=(httpx.HTTPError,),
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:  # pragma: no cover
            self._breaker.record_failure()
            _log.warning("edinet.rate_fetch_failed", error=str(exc))
            return self._mock.get_rate_curve()
        self._breaker.record_success()
        return curve or self._mock.get_rate_curve()

    def get_settlement_metrics(self) -> SettlementMetrics:
        """Settlement metrics are bank-internal; always the deterministic mock."""
        return self._mock.get_settlement_metrics()

    def _fetch_curve(self) -> list[RatePoint]:  # pragma: no cover
        """Fetch and parse the live EDINET / macro rate series.

        VERIFY: endpoint path, query params, and JSON response shape must be
        confirmed against the live EDINET / macro service. Excluded from
        coverage; guarded by the try/except in :meth:`get_rate_curve`.
        """
        s = self._settings
        url = f"{s.edinet_base_url.rstrip('/')}/macro/policy-rate"
        with httpx.Client(timeout=s.edinet_timeout_seconds) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        points: list[RatePoint] = []
        for row in payload.get("observations", []):
            as_of = dt.date.fromisoformat(row["date"])
            bps = int(round(float(row["value"]) * 100))
            points.append(RatePoint(as_of=as_of, policy_rate_bps=bps))
        return sorted(points, key=lambda p: p.as_of)
