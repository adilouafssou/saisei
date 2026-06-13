"""Core Banking API tool.

Returns monthly Shisanhyo (J-GAAP trial balances) for a company keyed by its
13-digit Hojin Bango. Backed by deterministic JSON fixtures.

This module is the canonical location under ``app.backend.tools.core_banking``.
The legacy path ``mocks.core_banking`` re-exports from here.

Fixtures are resolved from the bundled ``app/backend/tools/fixtures/`` directory
so the application does not depend on the legacy top-level ``mocks/fixtures/``
directory at runtime.  The old path is kept as a fallback for local development
environments that still have the legacy directory.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from app.backend.tools.fixtures import FIXTURES_DIR
from app.shared.models.accounting import TrialBalance

__all__ = ["CoreBankingMockClient"]

# Canonical bundled fixtures directory (ships inside the app package/wheel).
_FIXTURES: Path = FIXTURES_DIR


class CoreBankingMockClient:
    """Deterministic Core Banking client backed by JSON fixtures."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self._fixtures_dir = fixtures_dir or _FIXTURES

    def get_monthly_shisanhyo(self, hojin_bango: str) -> list[TrialBalance]:
        """Return the ordered monthly trial balances for a company.

        Args:
            hojin_bango: 13-digit Corporate Number.

        Returns:
            Trial balances sorted ascending by period.

        Raises:
            KeyError: If no fixture exists for the company.
        """
        stem = _FIXTURE_INDEX[hojin_bango]
        path = self._fixtures_dir / f"{stem}.json"
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        balances = [
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
        return sorted(balances, key=lambda tb: tb.period)


#: Maps 13-digit Hojin Bango to fixture file stems.
_FIXTURE_INDEX: dict[str, str] = {
    "1234567890123": "aichi_manufacturer",
}
