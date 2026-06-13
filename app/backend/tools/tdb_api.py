"""Teikoku Databank (TDB) API tool.

Returns deterministic corporate profiles, credit scores, and anti-social-forces
(Hanshateki Seiryoku / 反社会的勢力) check results keyed by 7-digit TDB Kigyo
code (企業コード). Profiles also carry the 13-digit Hojin Bango (法人番号).

This module is the canonical location under ``app.backend.tools.tdb_api``.
The legacy path ``mocks.tdb`` re-exports from here.

Fixtures are resolved from the bundled ``app/backend/tools/fixtures/`` directory
so the application does not depend on the legacy top-level ``mocks/fixtures/``
directory at runtime.  The old path is kept as a fallback for local development
environments that still have the legacy directory.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.backend.tools.fixtures import FIXTURES_DIR

__all__ = [
    "AntiSocialCheck",
    "CompanyProfile",
    "TdbCreditReport",
    "TdbMockClient",
]

# Canonical bundled fixtures directory (ships inside the app package/wheel).
_FIXTURES: Path = FIXTURES_DIR


class AntiSocialCheck(StrEnum):
    """Anti-social-forces screening result (反社会的勢力チェック)."""

    CLEAR = "clear"
    FLAGGED = "flagged"
    PENDING = "pending"


class CompanyProfile(BaseModel):
    """SME corporate profile as returned by TDB."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tdb_code: str = Field(description="7-digit TDB Kigyo code / 企業コード.")
    hojin_bango: str = Field(description="13-digit Corporate Number / 法人番号.")
    name: str = Field(description="Company name.")
    prefecture: str = Field(description="Prefecture / 都道府県.")
    industry: str = Field(description="Industry / 業種.")
    established_year: int = Field(description="Year established.")
    employees: int = Field(ge=0, description="Employee headcount.")

    @field_validator("tdb_code")
    @classmethod
    def _check_tdb_code(cls, value: str) -> str:
        if not (value.isdigit() and len(value) == 7):
            raise ValueError("tdb_code must be exactly 7 digits")
        return value

    @field_validator("hojin_bango")
    @classmethod
    def _check_hojin_bango(cls, value: str) -> str:
        if not (value.isdigit() and len(value) == 13):
            raise ValueError("hojin_bango must be exactly 13 digits")
        return value


class TdbCreditReport(BaseModel):
    """TDB credit assessment for a company."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tdb_code: str = Field(description="7-digit TDB Kigyo code.")
    profile: CompanyProfile
    tdb_score: int = Field(ge=1, le=100, description="TDB credit score (1-100, higher=better).")
    anti_social_check: AntiSocialCheck = Field(description="Anti-social-forces screening result.")


class TdbMockClient:
    """Deterministic TDB client backed by JSON fixtures."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self._fixtures_dir = fixtures_dir or _FIXTURES

    def get_credit_report(self, tdb_code: str) -> TdbCreditReport:
        """Return the credit report for a 7-digit TDB code.

        Args:
            tdb_code: 7-digit TDB Kigyo code.

        Returns:
            The deterministic :class:`TdbCreditReport` for the company.

        Raises:
            KeyError: If no fixture exists for the given code.
        """
        data = self._load(tdb_code)
        tdb = data["tdb"]
        profile = CompanyProfile.model_validate(data["profile"])
        return TdbCreditReport(
            tdb_code=tdb_code,
            profile=profile,
            tdb_score=tdb["tdb_score"],
            anti_social_check=AntiSocialCheck(tdb["anti_social_check"]),
        )

    def _load(self, tdb_code: str) -> dict[str, object]:
        path = self._fixtures_dir / f"{_FIXTURE_INDEX[tdb_code]}.json"
        with path.open(encoding="utf-8") as fh:
            payload: dict[str, object] = json.load(fh)
        return payload


#: Maps known TDB codes to fixture file stems.
_FIXTURE_INDEX: dict[str, str] = {
    "1234567": "aichi_manufacturer",
}
