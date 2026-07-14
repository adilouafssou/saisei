"""NTA Hojin Bango (法人番号 / Corporate Number) tool.

Validates and optionally enriches the 13-digit Corporate Number against Japan's
National Tax Agency (国税庁) Corporate Number Web-API.

Design (mirrors the polish_keikakusho offline-fallback contract):

* The 13-digit check-digit VALIDATION is pure, deterministic, and always runs
  offline -- it needs no network and is fully unit-tested.
* The LIVE LOOKUP (official name / prefecture) runs ONLY when an application id
  is configured (``Settings.hojin_bango_app_id``). On any error, timeout, or
  when unconfigured, the client degrades gracefully: it returns ``None`` for the
  enrichment and never raises into the graph.

This is the canonical location under ``app.backend.tools.hojin_bango``.

The Corporate Number check digit is defined by the NTA as:

    check_digit = 9 - ((sum over the 12 base digits of d_n * P_n) mod 9)

where the 12 base digits are numbered n = 1 (rightmost of the base) .. 12
(leftmost), and P_n = 1 if n is odd, 2 if n is even. The check digit is the
leading (1st) digit of the full 13-digit number.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.backend.secrets import resolve_secret
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "HojinBangoInfo",
    "HojinBangoClient",
    "is_valid_hojin_bango",
    "hojin_bango_check_digit",
]

_log = get_logger(__name__)


def hojin_bango_check_digit(base_12: str) -> int:
    """Return the NTA check digit for the 12 base digits of a Corporate Number.

    Args:
        base_12: The 12 base digits (the trailing 12 of the full 13-digit
            number), as a string of decimal digits.

    Returns:
        The check digit (0-9): ``9 - (weighted_sum mod 9)``.

    Raises:
        ValueError: If ``base_12`` is not exactly 12 digits.
    """
    if not (base_12.isdigit() and len(base_12) == 12):
        raise ValueError("base_12 must be exactly 12 digits")
    # n = 1 is the rightmost base digit; P_n = 1 (odd n) or 2 (even n).
    total = 0
    for i, ch in enumerate(reversed(base_12)):
        n = i + 1
        weight = 1 if n % 2 == 1 else 2
        total += int(ch) * weight
    return 9 - (total % 9)


def is_valid_hojin_bango(value: str) -> bool:
    """Return True if ``value`` is a structurally valid 13-digit Corporate Number.

    Checks length, all-digits, and the leading check digit per the NTA formula.
    """
    if not (value.isdigit() and len(value) == 13):
        return False
    check, base = value[0], value[1:]
    return int(check) == hojin_bango_check_digit(base)


class HojinBangoInfo(BaseModel):
    """Official corporate-registry record returned by the NTA Web-API."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hojin_bango: str = Field(description="13-digit Corporate Number / 法人番号.")
    name: str = Field(description="Official registered company name.")
    prefecture: str = Field(default="", description="Prefecture / 都道府県.")


class HojinBangoClient:
    """Validate + optionally enrich a Corporate Number via the NTA Web-API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def live_enabled(self) -> bool:
        """True when an application id is configured for live lookup.

        The app id is resolved through the secret seam, so a ``@env:`` /
        ``@file:`` / ``@/path`` reference enables the live path like a literal.
        """
        return bool(resolve_secret(self._settings.hojin_bango_app_id))

    def validate(self, hojin_bango: str) -> bool:
        """Deterministic, offline check-digit validation (never hits network)."""
        return is_valid_hojin_bango(hojin_bango)

    def lookup(self, hojin_bango: str) -> HojinBangoInfo | None:
        """Return the official registry record, or None on any failure.

        Always validates the check digit first (offline). Performs the live
        lookup only when configured; degrades to ``None`` (never raises) on
        invalid input, missing config, timeout, or HTTP/parse error.
        """
        if not self.validate(hojin_bango):
            _log.warning("hojin_bango.invalid_check_digit", hojin_bango=hojin_bango)
            return None
        if not self.live_enabled:
            return None
        try:
            return self._fetch(hojin_bango)
        except (httpx.HTTPError, ValueError, KeyError) as exc:  # pragma: no cover
            _log.warning("hojin_bango.lookup_failed", hojin_bango=hojin_bango, error=str(exc))
            return None

    def _fetch(self, hojin_bango: str) -> HojinBangoInfo | None:  # pragma: no cover
        """Perform the live NTA Web-API request.

        VERIFY: endpoint path, query params, and response shape are per the NTA
        Corporate Number Web-API v4 spec and must be confirmed against the live
        service before relying on this path. The offline contract (validate +
        graceful None) is fully tested; this network branch is excluded from
        coverage and guarded by the try/except in ``lookup``.
        """
        s = self._settings
        # NTA v4 returns CSV or XML; request CSV (type=12) for simplest parsing.
        url = f"{s.hojin_bango_base_url.rstrip('/')}/num"
        params: dict[str, Any] = {
            "id": resolve_secret(s.hojin_bango_app_id),
            "number": hojin_bango,
            "type": "12",  # CSV/Shift_JIS per NTA spec
            "history": "0",
        }
        with httpx.Client(timeout=s.hojin_bango_timeout_seconds) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return self._parse_csv(hojin_bango, resp.content)

    @staticmethod
    def _parse_csv(hojin_bango: str, content: bytes) -> HojinBangoInfo | None:  # pragma: no cover
        """Parse the first NTA CSV record into a HojinBangoInfo.

        VERIFY: column indices follow the NTA v4 CSV layout (col 1 = number,
        col 6 = name, col 9 = prefecture). Confirm against live output.
        """
        text = content.decode("shift_jis", errors="replace")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        if not first:
            return None
        cols = [c.strip('"') for c in first.split(",")]
        if len(cols) < 10:
            return None
        return HojinBangoInfo(
            hojin_bango=hojin_bango,
            name=cols[6],
            prefecture=cols[9],
        )
