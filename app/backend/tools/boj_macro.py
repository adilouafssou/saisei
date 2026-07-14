"""BOJ macro / settlement tool.

Returns a deterministic BOJ policy-rate curve and settlement liquidity metrics
(T+1 / T+2) used to assess the working-capital gap (Shikin Kuri / 資金繰り).

This module is the canonical location under ``app.backend.tools.boj_macro``.
The legacy path ``mocks.edinet_macro`` re-exports from here.
"""

from __future__ import annotations

import datetime as dt

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

_log = get_logger(__name__)

__all__ = ["RatePoint", "SettlementMetrics", "EdinetMacroMockClient", "BojRateClient"]


class RatePoint(BaseModel):
    """A single point on the BOJ policy-rate curve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: dt.date = Field(description="Observation date.")
    policy_rate_bps: int = Field(description="BOJ policy rate in basis points.")


class SettlementMetrics(BaseModel):
    """Settlement-cycle liquidity metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    t_plus_1_liquidity_ratio: float = Field(
        description="Liquidity coverage for T+1 settlement obligations."
    )
    t_plus_2_liquidity_ratio: float = Field(
        description="Liquidity coverage for T+2 settlement obligations."
    )
    receivable_days: int = Field(ge=0, description="Days sales outstanding / 売掛回収日数.")
    payable_days: int = Field(ge=0, description="Days payable outstanding / 買掛支払日数.")


class EdinetMacroMockClient:
    """Deterministic macro client.

    The rate curve reflects the recent BOJ exit from negative rates: a series of
    hikes that widen working-capital gaps for cost-pressured SMEs.
    """

    def get_rate_curve(self) -> list[RatePoint]:
        """Return the deterministic BOJ policy-rate curve (rising)."""
        base = dt.date(2025, 4, 30)
        bps = [10, 10, 25, 25, 25, 40, 40, 50, 50, 50, 60, 60]
        return [
            RatePoint(
                as_of=_month_end(base, i),
                policy_rate_bps=value,
            )
            for i, value in enumerate(bps)
        ]

    def get_settlement_metrics(self) -> SettlementMetrics:
        """Return deterministic settlement / liquidity metrics under rate stress."""
        return SettlementMetrics(
            t_plus_1_liquidity_ratio=0.82,
            t_plus_2_liquidity_ratio=0.74,
            receivable_days=95,
            payable_days=45,
        )


def _month_end(start: dt.date, months_ahead: int) -> dt.date:
    """Return the month-end date ``months_ahead`` months after ``start``."""
    month_index = start.month - 1 + months_ahead
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, month + 1, 1) - dt.timedelta(days=1)


class BojRateClient:
    """BOJ policy-rate client with a deterministic offline fallback.

    Fetches the live policy / uncollateralized overnight call-rate curve from
    the public BOJ / e-Stat time-series API when configured
    (``Settings.boj_api_base_url`` and ``boj_api_series_id`` set); otherwise, or
    on any HTTP/parse error, returns the deterministic mock curve from
    :class:`EdinetMacroMockClient`. Mirrors the polish_keikakusho offline
    fallback contract so `make verify` runs with no network.

    Settlement metrics (T+1/T+2 liquidity, DSO/DPO) remain mock-only because
    they are bank-internal, not published by the BOJ.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._mock = EdinetMacroMockClient()

    @property
    def live_enabled(self) -> bool:
        """True when both base URL and series id are configured."""
        return bool(self._settings.boj_api_base_url and self._settings.boj_api_series_id)

    def get_rate_curve(self) -> list[RatePoint]:
        """Return the BOJ rate curve (live when configured, else mock)."""
        if not self.live_enabled:
            return self._mock.get_rate_curve()
        try:
            curve = self._fetch_curve()
        except (httpx.HTTPError, ValueError, KeyError) as exc:  # pragma: no cover
            _log.warning("boj.rate_fetch_failed", error=str(exc))
            return self._mock.get_rate_curve()
        return curve or self._mock.get_rate_curve()

    def get_settlement_metrics(self) -> SettlementMetrics:
        """Settlement metrics are bank-internal; always the deterministic mock."""
        return self._mock.get_settlement_metrics()

    def _fetch_curve(self) -> list[RatePoint]:  # pragma: no cover
        """Fetch and parse the live BOJ rate series.

        VERIFY: endpoint path, query params, and JSON response shape follow the
        BOJ / e-Stat time-series API and must be confirmed against the live
        service. This network branch is excluded from coverage and guarded by
        the try/except in ``get_rate_curve``; the offline mock is the tested
        default path.
        """
        s = self._settings
        url = f"{s.boj_api_base_url.rstrip('/')}/series/{s.boj_api_series_id}"
        with httpx.Client(timeout=s.boj_api_timeout_seconds) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        points: list[RatePoint] = []
        for row in payload.get("observations", []):
            as_of = dt.date.fromisoformat(row["date"])
            # API publishes percent; store basis points (int yen-style integer).
            bps = int(round(float(row["value"]) * 100))
            points.append(RatePoint(as_of=as_of, policy_rate_bps=bps))
        return sorted(points, key=lambda p: p.as_of)
