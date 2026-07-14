"""Core Banking Shisanhyo client with a deterministic offline fallback.

Feature 2 (NEXT_STEPS.md), follow-on client after TdbClient. Fetches monthly
J-GAAP trial balances (Shisanhyo) from the bank-internal Core Banking API when
configured, behind the SAME ``MockDataProvider`` seam -- no graph changes.

Mirrors the established contract (BojRateClient / TdbClient):

* Deterministic :class:`~app.backend.tools.core_banking.CoreBankingMockClient`
  is the DEFAULT and the fallback. With no base URL configured the client is a
  pure pass-through to the mock and never touches the network.
* The LIVE LOOKUP runs ONLY when ``Settings.core_banking_base_url`` is set,
  wrapped in retry-with-backoff + a circuit breaker (Feature 2 slice 2). On any
  error or a failed boundary guard it degrades to the mock and never raises.
* A deterministic **boundary guard** (:func:`guard_shisanhyo`) enforces the
  J-GAAP identity invariant on every returned row (gross profit = sales - COGS,
  via ``TrialBalance.uriage_sourieki``) and rejects an empty series, so upstream
  data drift fails loudly into the fallback instead of corrupting a figure.

The live HTTP/parse branch is config-gated, excluded from coverage, and marked
``VERIFY``: the Core Banking API is bank-internal, so its endpoint and JSON
shape must be confirmed against the real service before this path is relied on.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.backend.secrets import resolve_secret
from app.backend.tools.core_banking import CoreBankingMockClient
from app.backend.tools.resilience import CircuitBreaker, retry_with_backoff
from app.shared.logging import get_logger
from app.shared.models.accounting import TrialBalance
from app.shared.settings import Settings, get_settings

__all__ = ["CoreBankingBoundaryError", "guard_shisanhyo", "CoreBankingClient"]

_log = get_logger(__name__)


class CoreBankingBoundaryError(ValueError):
    """Raised when a live Shisanhyo payload fails the deterministic boundary guard."""


def guard_shisanhyo(rows: list[TrialBalance]) -> list[TrialBalance]:
    """Validate live trial balances at the boundary; return them unchanged.

    The Pydantic model enforces per-field shape and strict integer yen, and it
    COMPUTES ``uriage_sourieki`` (= sales - COGS), so the gross-profit identity
    can never be violated by a model instance. This guard therefore adds the
    cross-row invariants a per-field shape check cannot express, so upstream
    data drift fails loudly here instead of silently corrupting a figure:

    * the series is non-empty;
    * no two rows share the same ``period``. Duplicate months are a real and
      undetectable-by-the-model drift: the downstream EWS window treats each
      row as a distinct month (and uses ``shisanhyo[0]`` / ``[-1]`` as the
      first/last endpoints), so a duplicated month would double-count it and
      distort the loss-ratio and trend signals.

    Args:
        rows: Parsed live trial balances.

    Returns:
        The same ``rows`` when every invariant holds.

    Raises:
        CoreBankingBoundaryError: If the series is empty or contains duplicate
            periods.
    """
    if not rows:
        raise CoreBankingBoundaryError("empty Shisanhyo series")
    seen_periods: set[dt.date] = set()
    for row in rows:
        if row.period in seen_periods:
            raise CoreBankingBoundaryError(f"duplicate Shisanhyo period: {row.period.isoformat()}")
        seen_periods.add(row.period)
    return rows


class CoreBankingClient:
    """Core Banking Shisanhyo client: live when configured, mock otherwise.

    Drop-in replacement for
    :class:`~app.backend.tools.core_banking.CoreBankingMockClient` behind the
    :class:`~app.backend.tools.provider.MockDataProvider` seam: exposes the same
    ``get_monthly_shisanhyo`` method, so no graph changes are required.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._mock = CoreBankingMockClient(fixtures_dir)
        self._breaker = CircuitBreaker(self._settings.core_banking_circuit_breaker_threshold)

    @property
    def live_enabled(self) -> bool:
        """True when a Core Banking base URL is configured."""
        return bool(self._settings.core_banking_base_url)

    def get_monthly_shisanhyo(self, hojin_bango: str) -> list[TrialBalance]:
        """Return monthly trial balances (live when configured, else the mock).

        Never raises on a live-path failure: any transport, HTTP, parse, or
        boundary-guard error trips the breaker and the deterministic mock series
        is returned, preserving the offline-fallback contract.
        """
        if not self.live_enabled:
            return self._mock.get_monthly_shisanhyo(hojin_bango)
        if not self._breaker.allow():
            _log.warning("core_banking.circuit_open", hojin_bango=hojin_bango)
            return self._mock.get_monthly_shisanhyo(hojin_bango)
        s = self._settings
        try:
            rows = retry_with_backoff(
                lambda: self._fetch(hojin_bango),
                max_retries=s.core_banking_max_retries,
                base_seconds=s.core_banking_backoff_base_seconds,
                retry_on=(httpx.HTTPError,),
            )
            guarded = guard_shisanhyo(rows)
        except (
            httpx.HTTPError,
            ValidationError,
            CoreBankingBoundaryError,
            ValueError,
            KeyError,
        ) as exc:  # pragma: no cover
            self._breaker.record_failure()
            _log.warning("core_banking.fetch_failed", hojin_bango=hojin_bango, error=str(exc))
            return self._mock.get_monthly_shisanhyo(hojin_bango)
        self._breaker.record_success()
        return sorted(guarded, key=lambda tb: tb.period)

    def _fetch(self, hojin_bango: str) -> list[TrialBalance]:  # pragma: no cover
        """Fetch and parse the live monthly Shisanhyo series.

        VERIFY: endpoint path, auth header, and JSON response shape follow the
        bank-internal Core Banking API and must be confirmed against the real
        service. Excluded from coverage; guarded by the try/except in
        :meth:`get_monthly_shisanhyo`; the offline mock is the tested default.
        """
        s = self._settings
        url = f"{s.core_banking_base_url.rstrip('/')}/companies/{hojin_bango}/shisanhyo"
        headers = {"Authorization": f"Bearer {resolve_secret(s.core_banking_api_key)}"}
        with httpx.Client(timeout=s.core_banking_timeout_seconds) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
        return [
            TrialBalance(
                period=dt.date.fromisoformat(row["period"]),
                uriage=row["uriage"],
                uriage_genka=row["uriage_genka"],
                hanbaihi=row["hanbaihi"],
                eigai_shueki=row.get("eigai_shueki", 0),
                eigai_hiyo=row.get("eigai_hiyo", 0),
            )
            for row in payload["shisanhyo"]
        ]
