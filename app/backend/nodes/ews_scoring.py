"""EWS scoring and FSA classification node.

Merges the EWS agent (compute_ews_score, ews_node) and the classifier
(classify, classifier_node) into a single blueprint file.

Public functions preserved for test compatibility:
- ``compute_ews_score``: pure function, testable in isolation.
- ``ews_node``: load Shisanhyo and compute EWS score.
- ``classify``: pure function, testable in isolation.
- ``classifier_node``: classify the debtor from assessed signals.
"""

from __future__ import annotations

from typing import Any

from app.backend.state import SaiseiState
from app.backend.tools.provider import MockDataProvider
from app.shared.constants import EWS_DOUBTFUL, EWS_SUBSTANDARD, TDB_NORMAL_FLOOR
from app.shared.logging import get_logger
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass

__all__ = [
    "compute_ews_score",
    "ews_node",
    "classify",
    "classifier_node",
]

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# EWS scoring
# ---------------------------------------------------------------------------


def _gross_margin(tb: TrialBalance) -> float:
    """Return the gross margin ratio for a trial balance (0 if no sales)."""
    sales = int(tb.uriage)
    if sales == 0:
        return 0.0
    return tb.uriage_sourieki / sales


def compute_ews_score(shisanhyo: list[TrialBalance]) -> float:
    """Compute a 0-100 EWS score from ordered monthly trial balances.

    Higher is worse. Returns 0.0 when there is insufficient history.

    Args:
        shisanhyo: Trial balances ordered ascending by period.

    Returns:
        The EWS score clamped to the inclusive range [0, 100].
    """
    if len(shisanhyo) < 2:
        return 0.0

    first, last = shisanhyo[0], shisanhyo[-1]

    # Signal 1: sales decline (failed kakaku tenka).
    sales_first = int(first.uriage) or 1
    sales_drop = max(0.0, (sales_first - int(last.uriage)) / sales_first)

    # Signal 2: gross-margin compression (genka koutou).
    margin_drop = max(0.0, _gross_margin(first) - _gross_margin(last))

    # Signal 3: ordinary-profit deterioration (Keijo Rieki trend).
    keijo_first = first.keijo_rieki
    keijo_last = last.keijo_rieki
    if keijo_first > 0:
        keijo_drop = max(0.0, (keijo_first - keijo_last) / keijo_first)
    else:
        keijo_drop = 1.0 if keijo_last < keijo_first else 0.0

    # Signal 4: share of loss-making months (negative Keijo Rieki).
    loss_months = sum(1 for tb in shisanhyo if tb.keijo_rieki < 0)
    loss_ratio = loss_months / len(shisanhyo)

    score = (
        25.0 * min(1.0, sales_drop * 3.0)
        + 30.0 * min(1.0, margin_drop * 10.0)
        + 30.0 * min(1.0, keijo_drop)
        + 15.0 * loss_ratio
    )
    return round(max(0.0, min(100.0, score)), 2)


def ews_node(state: SaiseiState, provider: MockDataProvider | None = None) -> dict[str, Any]:
    """Load the Shisanhyo and compute the EWS score.

    Args:
        state: Current graph state (requires ``hojin_bango``).
        provider: Data provider; defaults to the mock engine.

    Returns:
        Partial state update with ``shisanhyo`` and ``ews_score``.
    """
    provider = provider or MockDataProvider()
    try:
        shisanhyo = provider.shisanhyo(state.hojin_bango)
    except KeyError:
        _log.warning("ews.no_shisanhyo", hojin_bango=state.hojin_bango)
        return {"errors": [*state.errors, f"No Shisanhyo for Hojin Bango: {state.hojin_bango}"]}

    score = compute_ews_score(shisanhyo)
    _log.info("ews.scored", hojin_bango=state.hojin_bango, ews_score=score, months=len(shisanhyo))
    return {"shisanhyo": shisanhyo, "ews_score": score}


# ---------------------------------------------------------------------------
# FSA classification
# ---------------------------------------------------------------------------


def classify(
    ews_score: float | None,
    working_capital_gap: int | None,
    tdb_score: int | None,
) -> FsaClass:
    """Return the FSA classification for the given signals.

    Rules (most severe wins):
        * Yukyo Guchi (Doubtful): EWS >= 70, or a working-capital deficit
          combined with EWS >= 40.
        * Yoi Kanri (Substandard): EWS >= 40, or TDB score below the Normal
          floor, or any working-capital deficit.
        * Joyo (Normal): otherwise.

    Args:
        ews_score: Early Warning Signal score (0-100), or None.
        working_capital_gap: Shikin Kuri gap in yen (negative = deficit), or None.
        tdb_score: TDB credit score (1-100), or None.

    Returns:
        The FSA classification.
    """
    ews = ews_score or 0.0
    deficit = working_capital_gap is not None and working_capital_gap < 0

    if ews >= EWS_DOUBTFUL or (deficit and ews >= EWS_SUBSTANDARD):
        return FsaClass.YUKYO_GUCHI

    if (
        ews >= EWS_SUBSTANDARD
        or deficit
        or (tdb_score is not None and tdb_score < TDB_NORMAL_FLOOR)
    ):
        return FsaClass.YOI_KANRI

    return FsaClass.JOYO


def classifier_node(state: SaiseiState) -> dict[str, Any]:
    """Classify the debtor from the assessed signals.

    Args:
        state: Current graph state (uses EWS score, gap, TDB score).

    Returns:
        Partial state update with ``fsa_classification``.
    """
    classification = classify(
        ews_score=state.ews_score,
        working_capital_gap=state.working_capital_gap,
        tdb_score=state.tdb_score,
    )
    _log.info(
        "classifier.classified",
        fsa_classification=classification.value,
        kanji=classification.kanji,
        ews_score=state.ews_score,
        working_capital_gap=state.working_capital_gap,
        tdb_score=state.tdb_score,
    )
    return {"fsa_classification": classification}
