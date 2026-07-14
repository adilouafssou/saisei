"""Teikoku Databank (TDB) credit-report client with an offline fallback.

Feature 2 (NEXT_STEPS.md): the first live integration behind the existing
``MockDataProvider`` seam. It realises one live client WITHOUT any graph changes:
nodes keep calling ``provider.credit_report(tdb_code)``; only the client behind
the seam changes.

Design (mirrors :class:`~app.backend.tools.boj_macro.BojRateClient` and
:class:`~app.backend.tools.hojin_bango.HojinBangoClient`):

* The deterministic :class:`~app.backend.tools.tdb_api.TdbMockClient` is the
  DEFAULT and the fallback. With no API key configured the client is a pure
  pass-through to the mock and never touches the network, so ``make verify`` and
  CI run fully offline.
* The LIVE LOOKUP runs ONLY when ``Settings.tdb_api_key`` is set. On any error
  (transport, HTTP status, parse, or a failed boundary guard) the client logs
  and degrades to the mock report -- it never raises into the graph.
* A deterministic **boundary guard** (:func:`guard_credit_report`) validates the
  live payload's shape and J-GAAP-style identity invariants BEFORE the data is
  trusted, so upstream data drift fails loudly into the fallback instead of
  silently corrupting a downstream figure.

The live HTTP/parse branch is config-gated, excluded from coverage, and marked
``VERIFY``: the real TDB API is paid and contract-only, so its exact endpoint
and JSON shape must be confirmed against the live service before it is relied on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.backend.secrets import resolve_secret
from app.backend.tools.resilience import CircuitBreaker, retry_with_backoff
from app.backend.tools.tdb_api import (
    AntiSocialCheck,
    CompanyProfile,
    TdbCreditReport,
    TdbMockClient,
)
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = ["TdbBoundaryError", "guard_credit_report", "TdbClient"]

_log = get_logger(__name__)


class TdbBoundaryError(ValueError):
    """Raised when a live TDB payload fails the deterministic boundary guard."""


def guard_credit_report(tdb_code: str, report: TdbCreditReport) -> TdbCreditReport:
    """Validate a credit report at the live-data boundary; return it unchanged.

    The Pydantic model already enforces field shapes and ranges (7-digit code,
    13-digit Hojin Bango, score in 1-100). This guard adds the cross-field
    IDENTITY invariants that a shape check cannot express, so upstream data
    drift fails loudly here instead of silently corrupting a downstream figure:

    * the requested ``tdb_code`` matches the report and its embedded profile;
    * the anti-social-forces check is a recognised enum value.

    Args:
        tdb_code: The 7-digit TDB code that was requested.
        report: The parsed live credit report to validate.

    Returns:
        The same ``report`` when every invariant holds.

    Raises:
        TdbBoundaryError: If any cross-field invariant is violated.
    """
    if report.tdb_code != tdb_code:
        raise TdbBoundaryError(
            f"tdb_code mismatch: requested {tdb_code!r}, report has {report.tdb_code!r}"
        )
    if report.profile.tdb_code != tdb_code:
        raise TdbBoundaryError(
            f"profile tdb_code mismatch: requested {tdb_code!r}, "
            f"profile has {report.profile.tdb_code!r}"
        )
    if report.anti_social_check not in AntiSocialCheck:
        raise TdbBoundaryError(f"unknown anti_social_check: {report.anti_social_check!r}")
    return report


class TdbClient:
    """TDB credit-report client: live when configured, deterministic mock otherwise.

    Drop-in replacement for :class:`~app.backend.tools.tdb_api.TdbMockClient`
    behind the :class:`~app.backend.tools.provider.MockDataProvider` seam:
    exposes the same ``get_credit_report`` method, so no graph changes are
    required to adopt it.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._mock = TdbMockClient(fixtures_dir)
        self._breaker = CircuitBreaker(self._settings.tdb_circuit_breaker_threshold)

    @property
    def live_enabled(self) -> bool:
        """True when a TDB API key is configured for live lookup.

        The key is resolved through the secret seam, so a ``@env:`` / ``@file:``
        / ``@/path`` reference enables the live path exactly like a literal.
        """
        return bool(resolve_secret(self._settings.tdb_api_key))

    def get_credit_report(self, tdb_code: str) -> TdbCreditReport:
        """Return the credit report (live when configured, else the mock).

        Resilient live path: retries transient failures with exponential
        backoff and trips a circuit breaker after sustained failures so the
        request path short-circuits to the mock without a network call. Never
        raises on a live-path failure: any transport, HTTP, parse, or
        boundary-guard error is logged and the deterministic mock report is
        returned, preserving the offline-fallback contract.

        Args:
            tdb_code: 7-digit TDB Kigyo code.

        Returns:
            The :class:`TdbCreditReport` for the company.
        """
        if not self.live_enabled:
            return self._mock.get_credit_report(tdb_code)
        # Circuit breaker open -> skip the network entirely, use the fallback.
        if not self._breaker.allow():
            _log.warning("tdb.circuit_open", tdb_code=tdb_code)
            return self._mock.get_credit_report(tdb_code)
        s = self._settings
        try:
            report = retry_with_backoff(
                lambda: self._fetch_report(tdb_code),
                max_retries=s.tdb_api_max_retries,
                base_seconds=s.tdb_api_backoff_base_seconds,
                retry_on=(httpx.HTTPError,),
            )
            guarded = guard_credit_report(tdb_code, report)
        except (
            httpx.HTTPError,
            ValidationError,
            TdbBoundaryError,
            ValueError,
            KeyError,
        ) as exc:  # pragma: no cover
            self._breaker.record_failure()
            _log.warning("tdb.report_fetch_failed", tdb_code=tdb_code, error=str(exc))
            return self._mock.get_credit_report(tdb_code)
        self._breaker.record_success()
        return guarded

    def _fetch_report(self, tdb_code: str) -> TdbCreditReport:  # pragma: no cover
        """Fetch and parse the live TDB credit report.

        VERIFY: endpoint path, auth header, query params, and JSON response
        shape follow the TDB credit-report API contract and must be confirmed
        against the live (paid, contract-only) service before this path is
        relied on. Excluded from coverage and guarded by the try/except in
        :meth:`get_credit_report`; the offline mock is the tested default path.
        """
        s = self._settings
        url = f"{s.tdb_api_base_url.rstrip('/')}/companies/{tdb_code}/credit-report"
        headers = {"Authorization": f"Bearer {resolve_secret(s.tdb_api_key)}"}
        with httpx.Client(timeout=s.tdb_api_timeout_seconds) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
        profile = CompanyProfile.model_validate(payload["profile"])
        return TdbCreditReport(
            tdb_code=tdb_code,
            profile=profile,
            tdb_score=int(payload["tdb_score"]),
            anti_social_check=AntiSocialCheck(payload["anti_social_check"]),
        )
