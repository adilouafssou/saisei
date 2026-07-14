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
from typing import Any

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

    def __getitem__(self, key: str) -> Any:
        """Support dict-style read access (e.g. ``profile["hojin_bango"]``).

        A snapshot's ``company_profile`` may be consumed either as a live
        Pydantic object (``profile.hojin_bango``) or, after a checkpoint
        round-trip / in callers that read it uniformly, via subscript
        (``profile["hojin_bango"]``). This thin accessor keeps the model frozen
        and validated while supporting both styles, mirroring
        :meth:`app.backend.state.Strategy.__getitem__`.

        Args:
            key: A model field name.

        Returns:
            The value of the named field.

        Raises:
            KeyError: If ``key`` is not a field of the model.
        """
        if key not in type(self).model_fields:
            raise KeyError(key)
        return getattr(self, key)


class TdbCreditReport(BaseModel):
    """TDB credit assessment for a company."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tdb_code: str = Field(description="7-digit TDB Kigyo code.")
    profile: CompanyProfile
    tdb_score: int = Field(ge=1, le=100, description="TDB credit score (1-100, higher=better).")
    anti_social_check: AntiSocialCheck = Field(description="Anti-social-forces screening result.")
    lender_stakes: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Optional outstanding loan balances (JPY) per lender role "
            "(e.g. {'main_bank': ..., 'sub_bank': ...}). When present, the "
            "sub-bank critic runs the ACCURATE stake-based pro-rata fairness "
            "check instead of the weak uplift heuristic. Empty when the source "
            "carries no stake data (the critic then falls back to the proxy)."
        ),
    )


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
        tdb: dict[str, Any] = data["tdb"]  # type: ignore[assignment]
        profile = CompanyProfile.model_validate(data["profile"])
        # Optional stake data: present only on fixtures that model a syndicate;
        # absent -> {} so the sub-bank critic keeps its heuristic fallback.
        raw_stakes = tdb.get("lender_stakes") or {}
        lender_stakes = {str(k): int(v) for k, v in raw_stakes.items()}
        return TdbCreditReport(
            tdb_code=tdb_code,
            profile=profile,
            tdb_score=tdb["tdb_score"],
            anti_social_check=AntiSocialCheck(tdb["anti_social_check"]),
            lender_stakes=lender_stakes,
        )

    def _load(self, tdb_code: str) -> dict[str, object]:
        path = self._fixtures_dir / f"{_FIXTURE_INDEX[tdb_code]}.json"
        with path.open(encoding="utf-8") as fh:
            payload: dict[str, object] = json.load(fh)
        return payload


#: Maps known TDB codes to fixture file stems.
_FIXTURE_INDEX: dict[str, str] = {
    "1234567": "aichi_manufacturer",
    "2000001": "normal_service_co",
    "3000001": "needs_attention_mfg",
    "4000001": "osaka_distressed_mfg",
    "5000001": "kyoto_wc_deficit_co",
    "6000001": "thin_margin_trading_co",
}
