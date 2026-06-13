"""Shared constants for the Saisei app package."""

from __future__ import annotations

#: EWS thresholds (higher score = worse health).
EWS_DOUBTFUL: float = 70.0
EWS_SUBSTANDARD: float = 40.0

#: TDB score floor below which a debtor cannot be Normal.
TDB_NORMAL_FLOOR: int = 60

#: Annualisation factor (monthly -> yearly).
MONTHS_PER_YEAR: int = 12

#: Maximum revision cycles before the graph forces escalation.
MAX_REVISION_CYCLES: int = 3

#: Pro-rata deviation tolerance for sub-bank critic (fraction, e.g. 0.20 = 20%).
#: This is the single source of truth; sub_bank.py and lead_arranger.py import it.
PRO_RATA_TOLERANCE: float = 0.20

#: Minimum recovery horizon (years) required by guarantor critic.
MIN_RECOVERY_HORIZON_YEARS: int = 5

#: Keieisha Hosho scoring weights (must sum to 100).
HOSHO_WEIGHT_BUNRI: float = 40.0       # 法人個人分離
HOSHO_WEIGHT_ZAIMU: float = 35.0       # 財務基盤の強化
HOSHO_WEIGHT_KAIJI: float = 25.0       # 適時適切な情報開示

#: Thresholds for Hosho Kaijo eligibility.
HOSHO_ELIGIBLE_SCORE: float = 70.0     # Score >= this → eligible for release
HOSHO_SUCCESSION_EWS_MAX: float = 50.0  # EWS must be below this for succession readiness
HOSHO_SUCCESSION_TDB_MIN: int = 55      # TDB score must be >= this for succession readiness
