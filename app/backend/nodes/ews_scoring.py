"""EWS scoring and FSA classification node.

Merges the EWS agent (compute_ews_score, ews_node) and the classifier
(classify, classifier_node) into a single blueprint file.

Public functions preserved for test compatibility:
- ``compute_ews_score``: pure function, testable in isolation.
- ``ews_node``: load Shisanhyo and compute EWS score.
- ``classify``: pure function, testable in isolation.
- ``classifier_node``: classify the debtor from assessed signals.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here.
LangGraph introspects node ``config`` parameter types at ``add_node`` time;
stringized annotations make it emit a spurious UserWarning. Keeping runtime
annotations lets it resolve ``RunnableConfig | None`` cleanly.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.record import record_event
from app.backend.portfolio.recorder import record_snapshot
from app.backend.state import SaiseiState
from app.backend.tools.provider import MockDataProvider
from app.shared.constants import (
    EWS_DANGER,
    EWS_DOUBTFUL,
    EWS_SUBSTANDARD,
    TDB_NORMAL_FLOOR,
)
from app.shared.logging import get_logger
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass

__all__ = [
    "compute_ews_score",
    "EwsSignal",
    "compute_ews_breakdown",
    "classification_reason",
    "trend_descriptor",
    "acceleration_descriptor",
    "ews_node",
    "classify",
    "classifier_node",
]

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# EWS scoring
# ---------------------------------------------------------------------------


def _gross_margin(tb: TrialBalance) -> float | None:
    """Return the gross-margin ratio for a trial balance, or None if no sales.

    A zero-sales month has NO meaningful gross margin (the ratio is undefined,
    not 0%). Returning ``None`` lets the margin-compression signal SKIP that
    endpoint instead of treating "sold nothing" as a real 0% margin — which
    previously distorted the EWS score. The genuine distress of a zero-sales
    month is still captured by the loss-ratio and ordinary-profit signals.
    """
    sales = int(tb.uriage)
    if sales == 0:
        return None
    return tb.uriage_sourieki / sales


def _safe_sales_drop(first: TrialBalance, last: TrialBalance) -> float:
    """Return the sales-decline fraction from ``first`` to ``last`` in [0, 1].

    When the baseline month has zero sales the decline is UNDEFINED (a firm
    cannot decline from nothing), so this returns 0.0 rather than fabricating a
    ¥1 baseline — the old ``sales_first or 1`` made a zero-sales baseline produce
    a meaningless, near-maximal sales-drop signal. A zero-sales baseline's
    distress is captured by the loss-ratio / ordinary-profit signals instead.
    """
    sales_first = int(first.uriage)
    if sales_first <= 0:
        return 0.0
    return max(0.0, (sales_first - int(last.uriage)) / sales_first)


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
    return round(max(0.0, min(100.0, sum(s.points for s in _ews_signals(shisanhyo)))), 2)


# ---------------------------------------------------------------------------
# EWS explainability (Feature 7): per-signal breakdown of the score.
#
# The score is a weighted blend of four deterministic signals. Showing only the
# final number is opaque; this exposes each signal's raw measure, its capped
# contribution (points), and the weight ceiling, so a banker (or an examiner)
# can see EXACTLY which deterioration drove the score. The breakdown reuses the
# SAME arithmetic as compute_ews_score (single source of truth, _ews_signals),
# so the parts always sum to the whole and can never drift from the score.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EwsSignal:
    """One weighted EWS signal's contribution to the score.

    Attributes:
        key: Stable signal id ('sales_drop' | 'margin_drop' | 'keijo_drop'
            | 'loss_ratio').
        label_ja: Banker-facing Japanese label.
        raw: The underlying measure in [0, 1] (e.g. 0.18 = an 18% decline).
        points: The signal's capped contribution to the 0-100 score.
        weight: The signal's maximum possible points (its weight ceiling).
    """

    key: str
    label_ja: str
    raw: float
    points: float
    weight: float


def _ews_signals(shisanhyo: list[TrialBalance]) -> list[EwsSignal]:
    """Compute the four weighted EWS signals (the score's single source of truth).

    Both :func:`compute_ews_score` and :func:`compute_ews_breakdown` use this so
    the per-signal points always sum to the score. Returns ``[]`` when there is
    insufficient history (< 2 months), matching the score's 0.0 floor.
    """
    if len(shisanhyo) < 2:
        return []

    first, last = shisanhyo[0], shisanhyo[-1]

    # Signal 1: sales decline (failed kakaku tenka).
    sales_drop = _safe_sales_drop(first, last)

    # Signal 2: gross-margin compression (genka koutou).
    margin_first = _gross_margin(first)
    margin_last = _gross_margin(last)
    if margin_first is None or margin_last is None:
        margin_drop = 0.0
    else:
        margin_drop = max(0.0, margin_first - margin_last)

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

    # Signal 5: trajectory / trend velocity over the FULL monthly series.
    # The other four signals compare only endpoints, so a firm that fell then
    # recovered scores identically to one in steady free-fall. This signal reads
    # the SHAPE between the endpoints: the fraction of month-over-month 経常利益
    # steps that are declines (a sustained-deterioration ratio). A monotonic
    # slide reads ~1.0 (worst); a recovering / stable trajectory reads low. This
    # is what makes the EWS genuinely "early" -- it fires on the slope before the
    # endpoint gap is severe. Computed from keijo_rieki (the same series the
    # loss-ratio signal uses), so no new data is required.
    trend_ratio = _deterioration_ratio(shisanhyo)

    return [
        EwsSignal(
            key="sales_drop",
            label_ja="売上減少（Sales decline）",
            raw=round(sales_drop, 4),
            points=round(22.0 * min(1.0, sales_drop * 3.0), 2),
            weight=22.0,
        ),
        EwsSignal(
            key="margin_drop",
            label_ja="粗利率低下（Margin compression）",
            raw=round(margin_drop, 4),
            points=round(26.0 * min(1.0, margin_drop * 10.0), 2),
            weight=26.0,
        ),
        EwsSignal(
            key="keijo_drop",
            label_ja="経常利益悪化（Ordinary-profit deterioration）",
            raw=round(keijo_drop, 4),
            points=round(27.0 * min(1.0, keijo_drop), 2),
            weight=27.0,
        ),
        EwsSignal(
            key="loss_ratio",
            label_ja="赤字月比率（Loss-making months）",
            raw=round(loss_ratio, 4),
            points=round(13.0 * loss_ratio, 2),
            weight=13.0,
        ),
        EwsSignal(
            key="trend",
            label_ja="悪化トレンド（Sustained deterioration）",
            raw=round(trend_ratio, 4),
            points=round(12.0 * trend_ratio, 2),
            weight=12.0,
        ),
    ]


def _deterioration_ratio(shisanhyo: list[TrialBalance]) -> float:
    """Return the fraction of month-over-month 経常利益 steps that decline, [0, 1].

    The trajectory signal's raw measure: walk the ordered monthly ordinary-profit
    series and count the consecutive steps where profit fell versus the prior
    month, over the total number of steps. 1.0 = every month worse than the last
    (a monotonic slide); 0.0 = never worse (flat or improving). This reads the
    SHAPE of the series, not just its endpoints, so a steady decline is caught
    early and a fell-then-recovered firm is not penalised as if still falling.

    Returns 0.0 when there are fewer than two months (no step to measure),
    matching the score's insufficient-history floor.
    """
    if len(shisanhyo) < 2:
        return 0.0
    declines = 0
    steps = 0
    prev = shisanhyo[0].keijo_rieki
    for tb in shisanhyo[1:]:
        steps += 1
        if tb.keijo_rieki < prev:
            declines += 1
        prev = tb.keijo_rieki
    return declines / steps if steps else 0.0


def _acceleration_ratio(shisanhyo: list[TrialBalance]) -> float:
    """Return the fraction of consecutive 経常利益 steps that steepen, [0, 1].

    A SECOND-ORDER trajectory measure that reads whether the decline is
    *speeding up*. Where :func:`_deterioration_ratio` counts how often profit
    fell month-over-month (the slope), this walks the series of month-over-month
    deltas and counts how often each delta is MORE NEGATIVE than the previous one
    (the change in the slope -- the curvature). 1.0 = every step steeper than the
    last (an accelerating free-fall); 0.0 = never steepening (a linear or easing
    decline). This catches a firm whose slide is steepening even before the
    magnitude is alarming -- the natural analytical deepening of the trend signal.

    Computed from the SAME keijo_rieki series the trend signal uses, so no new
    data is required. Returns 0.0 when there are fewer than three months (you need
    at least two deltas to compare one against the next), matching the
    insufficient-history floor used elsewhere. Like the trend signal it feeds the
    explanation path ONLY -- never the score, a gate, or a route.
    """
    if len(shisanhyo) < 3:
        return 0.0
    deltas = [
        shisanhyo[i].keijo_rieki - shisanhyo[i - 1].keijo_rieki for i in range(1, len(shisanhyo))
    ]
    steepenings = 0
    pairs = 0
    for i in range(1, len(deltas)):
        pairs += 1
        if deltas[i] < deltas[i - 1]:
            steepenings += 1
    return steepenings / pairs if pairs else 0.0


def compute_ews_breakdown(shisanhyo: list[TrialBalance]) -> list[EwsSignal]:
    """Return the per-signal EWS breakdown (display/audit metadata).

    Pure and deterministic; the signal points sum to ``compute_ews_score`` (both
    derive from :func:`_ews_signals`). Empty when there is insufficient history.

    Args:
        shisanhyo: Trial balances ordered ascending by period.

    Returns:
        The four :class:`EwsSignal` contributions (or ``[]`` when < 2 months).
    """
    return _ews_signals(shisanhyo)


def ews_node(state: SaiseiState, provider: MockDataProvider | None = None) -> dict[str, Any]:
    """Load the Shisanhyo and compute the EWS score.

    Args:
        state: Current graph state (requires ``hojin_bango``).
        provider: Data provider; defaults to the mock engine.

    Returns:
        Partial state update with ``shisanhyo`` and ``ews_score``.
    """
    provider = provider or MockDataProvider()
    # Honor a Shisanhyo already present on the state (e.g. confirmed Excel/CSV
    # upload injected as the initial payload, Feature 8 Part 6) instead of
    # overwriting it with the provider fixture. Normal runs start with an empty
    # shisanhyo, so they still fetch from the provider — behaviour unchanged.
    if state.shisanhyo:
        shisanhyo = state.shisanhyo
    else:
        try:
            shisanhyo = provider.shisanhyo(state.hojin_bango)
        except KeyError:
            _log.warning("ews.no_shisanhyo", hojin_bango=state.hojin_bango)
            return {
                "errors": [
                    *state.errors,
                    f"No Shisanhyo for Hojin Bango: {state.hojin_bango}",
                ]
            }

    score = compute_ews_score(shisanhyo)
    breakdown = [s.__dict__ for s in compute_ews_breakdown(shisanhyo)]
    _log.info("ews.scored", hojin_bango=state.hojin_bango, ews_score=score, months=len(shisanhyo))
    return {"shisanhyo": shisanhyo, "ews_score": score, "ews_breakdown": breakdown}


# ---------------------------------------------------------------------------
# FSA classification
# ---------------------------------------------------------------------------


def trend_descriptor(trend_ratio: float | None) -> str:
    """Return a bilingual trajectory descriptor for a deterioration ratio.

    Translates the deterministic trend signal (the fraction of month-over-month
    経常利益 steps that declined, see :func:`_deterioration_ratio`) into a short,
    banker-facing phrase that says WHY the slope matters. The bands mirror how a
    relationship manager reads a trajectory:

        >= 0.80  → 持続的悪化 (sustained deterioration) -- a near-monotonic slide.
        >= 0.50  → 悪化傾向 (deteriorating trend) -- more down-months than up.
        <  0.50  → "" (no adverse-trajectory phrase; trend is mixed/improving).

    Pure display derivation from an already-computed figure; it names a
    trajectory, never a verdict, and feeds no gate, route, or score. Returns ""
    when ``trend_ratio`` is None (no trajectory available) or below the floor.

    Args:
        trend_ratio: The deterioration ratio in [0, 1], or None.

    Returns:
        A bilingual descriptor phrase, or "" when no adverse trajectory applies.
    """
    if trend_ratio is None:
        return ""
    if trend_ratio >= 0.80:
        return (
            f"持続的悪化（sustained deterioration; 経常利益が月次で "
            f"{trend_ratio * 100:.0f}% の期間低下）"
        )
    if trend_ratio >= 0.50:
        return (
            f"悪化傾向（deteriorating trend; 経常利益が月次で {trend_ratio * 100:.0f}% の期間低下）"
        )
    return ""


def acceleration_descriptor(trend_ratio: float | None, accel_ratio: float | None) -> str:
    """Return a bilingual 'decline is accelerating' clause, or "" when none applies.

    Translates the deterministic acceleration signal (the fraction of consecutive
    経常利益 steps that steepen, see :func:`_acceleration_ratio`) into a short
    banker-facing phrase that says the slide is SPEEDING UP -- the second-order
    read on top of the slope. To avoid alarming on a decline that is steady or
    easing, the clause fires ONLY when the borrower both has an adverse
    trajectory (``trend_ratio`` >= 0.50, i.e. a real downtrend) AND is
    accelerating (``accel_ratio`` >= 0.50, i.e. most steps are steepening). A
    firm that is declining but decelerating (stabilising) gets no clause.

    Pure display derivation from already-computed figures; it names a trajectory
    shape, never a verdict, and feeds no gate, route, or score. Returns "" when
    either ratio is None or below its floor.

    Args:
        trend_ratio: The deterioration ratio in [0, 1], or None.
        accel_ratio: The acceleration ratio in [0, 1], or None.

    Returns:
        A bilingual descriptor phrase, or "" when no acceleration applies.
    """
    if trend_ratio is None or accel_ratio is None:
        return ""
    if trend_ratio < 0.50 or accel_ratio < 0.50:
        return ""
    return (
        f"悪化が加速（decline accelerating; 経常利益の悪化幅が "
        f"{accel_ratio * 100:.0f}% の局面で拡大）"
    )


def classify(
    ews_score: float | None,
    working_capital_gap: int | None,
    tdb_score: int | None,
    *,
    is_insolvent: bool | None = None,
    net_worth: int | None = None,
) -> tuple[FsaClass, bool]:
    """Return the FSA classification and 要管理先 sub-flag for the given signals.

    Implements the five-category FSA Financial Inspection Manual (金融検査マニュアル)
    classification. Rules apply in most-severe-wins order (top = most severe):

    **破綻先 (Bankrupt)**
        ``is_insolvent=True`` AND ``net_worth < 0`` (both hard signals present).
        The borrower has formally failed and has negative net worth.

    **実質破綻先 (De facto Bankrupt)**
        ``is_insolvent=True`` OR ``net_worth < 0`` (either hard signal present),
        OR EWS >= EWS_DANGER (85) — financial deterioration so severe that
        de-facto bankruptcy is indicated even without an explicit insolvency flag.

    **破綻懸念先 (In Danger of Bankruptcy)**
        EWS >= EWS_DOUBTFUL (70), OR a working-capital deficit combined with
        EWS >= EWS_SUBSTANDARD (40).

    **要注意先 (Needs Attention)**
        EWS >= EWS_SUBSTANDARD (40), OR any working-capital deficit, OR TDB
        score below TDB_NORMAL_FLOOR.
        Sub-flag: ``special_attention=True`` (要管理先) when 要注意先 AND a
        working-capital deficit exists (要管理債権 indicator).

    **正常先 (Normal)**
        All other cases.

    Args:
        ews_score: Early Warning Signal score (0-100), or None.
        working_capital_gap: Shikin Kuri gap in yen (negative = deficit), or None.
        tdb_score: TDB credit score (1-100), or None.
        is_insolvent: Explicit insolvency flag (True = insolvent), or None.
        net_worth: Borrower net worth in JPY (may be negative), or None.

    Returns:
        A tuple of (FsaClass, special_attention) where ``special_attention``
        is True when the borrower is 要注意先 with a working-capital deficit
        (i.e. a 要管理先 sub-tier borrower).
    """
    ews = ews_score or 0.0
    deficit = working_capital_gap is not None and working_capital_gap < 0
    insolvent_flag = is_insolvent is True
    negative_net_worth = net_worth is not None and net_worth < 0

    # --- Band 1: 破綻先 (Bankrupt) — both hard signals ---
    if insolvent_flag and negative_net_worth:
        return FsaClass.HATANSAKI, False

    # --- Band 2: 実質破綻先 (De facto Bankrupt) — either hard signal or extreme EWS ---
    if insolvent_flag or negative_net_worth or ews >= EWS_DANGER:
        return FsaClass.JISSHITSU_HATANSAKI, False

    # --- Band 3: 破綻懸念先 (In Danger of Bankruptcy) ---
    if ews >= EWS_DOUBTFUL or (deficit and ews >= EWS_SUBSTANDARD):
        return FsaClass.HATAN_KENENSAKI, False

    # --- Band 4: 要注意先 (Needs Attention) ---
    if (
        ews >= EWS_SUBSTANDARD
        or deficit
        or (tdb_score is not None and tdb_score < TDB_NORMAL_FLOOR)
    ):
        # 要管理先 sub-flag: 要注意先 with a working-capital deficit.
        special_attention = deficit
        return FsaClass.YOCHUISAKI, special_attention

    # --- Band 5: 正常先 (Normal) ---
    return FsaClass.SEIJOSAKI, False


def classification_reason(
    classification: FsaClass,
    ews_score: float | None,
    working_capital_gap: int | None,
    tdb_score: int | None,
    *,
    is_insolvent: bool | None = None,
    net_worth: int | None = None,
    trend_ratio: float | None = None,
    accel_ratio: float | None = None,
) -> str:
    """Return the deterministic threshold reason for a classification.

    Feature 7 explainability: states WHICH signal crossed WHICH threshold to put
    the borrower in its band, mirroring the exact cascade in :func:`classify`
    (same order, same constants). Display/audit metadata only — it never decides
    the class (``classify`` does) and never feeds a gate, route, or figure.

    When ``trend_ratio`` is provided and the borrower is in an ACTIONABLE band
    (要注意先 / 破綻懸念先, or 実質破綻先 reached via extreme EWS rather than a hard
    insolvency signal), a deterministic trajectory clause is appended via
    :func:`trend_descriptor` so the banker reads WHY the slope matters
    (e.g. a sustained slide vs a one-off dip). The clause is purely additive
    explanatory prose; it changes no threshold and no decision, and is omitted
    for the hard-signal bankrupt bands (where the trajectory is moot) and the
    正常先 band.

    Args:
        classification: The class :func:`classify` returned (the cascade winner).
        ews_score: Early Warning Signal score (0-100), or None.
        working_capital_gap: Shikin Kuri gap in yen (negative = deficit), or None.
        tdb_score: TDB credit score (1-100), or None.
        is_insolvent: Explicit insolvency flag, or None.
        net_worth: Borrower net worth in JPY (may be negative), or None.
        trend_ratio: The deterioration ratio in [0, 1] (see
            :func:`_deterioration_ratio`), or None to omit the trajectory clause.

    Returns:
        A short bilingual reason string naming the decisive threshold(s), with an
        optional trajectory clause for actionable bands.
    """
    ews = ews_score or 0.0
    deficit = working_capital_gap is not None and working_capital_gap < 0
    insolvent_flag = is_insolvent is True
    negative_net_worth = net_worth is not None and net_worth < 0

    if classification is FsaClass.HATANSAKI:
        return "破綻認定かつ純資産マイナス (insolvency flag AND negative net worth)"
    if classification is FsaClass.JISSHITSU_HATANSAKI:
        if insolvent_flag:
            return "破綻認定 (insolvency flag set)"
        if negative_net_worth:
            return "純資産マイナス (negative net worth)"
        # Reached via extreme EWS (not a hard signal) -> the trajectory is
        # informative, so the clause is appended below.
        base = f"EWS {ews:.0f} ≥ {int(EWS_DANGER)} (危険水準 / danger threshold)"
        return _with_trend(base, trend_ratio, accel_ratio)
    if classification is FsaClass.HATAN_KENENSAKI:
        if ews >= EWS_DOUBTFUL:
            base = f"EWS {ews:.0f} ≥ {int(EWS_DOUBTFUL)} (破綻懸念水準 / doubtful threshold)"
        else:
            base = (
                f"資金繰り不足かつ EWS {ews:.0f} ≥ {int(EWS_SUBSTANDARD)} "
                "(working-capital deficit + substandard EWS)"
            )
        return _with_trend(base, trend_ratio, accel_ratio)
    if classification is FsaClass.YOCHUISAKI:
        reasons: list[str] = []
        if ews >= EWS_SUBSTANDARD:
            reasons.append(f"EWS {ews:.0f} ≥ {int(EWS_SUBSTANDARD)} (要注意水準)")
        if deficit:
            reasons.append("資金繰り不足 (working-capital deficit → 要管理先)")
        if tdb_score is not None and tdb_score < TDB_NORMAL_FLOOR:
            reasons.append(f"TDBスコア {tdb_score} < {TDB_NORMAL_FLOOR} (below normal floor)")
        base = " / ".join(reasons) if reasons else "要注意水準に該当"
        return _with_trend(base, trend_ratio, accel_ratio)
    return f"全閾値を下回る (all thresholds clear; EWS {ews:.0f} < {int(EWS_SUBSTANDARD)})"


def _with_trend(base: str, trend_ratio: float | None, accel_ratio: float | None = None) -> str:
    """Append the trajectory (and acceleration) descriptors when they apply.

    The trend descriptor names WHY the slope matters (sustained slide vs one-off
    dip); the acceleration descriptor adds whether that slide is SPEEDING UP.
    Both are purely additive explanatory clauses joined with ' ・ '. Returns
    ``base`` unchanged when neither descriptor yields a phrase, so a run without
    trajectory data -- or with ``accel_ratio`` omitted -- is byte-identical to
    before (the acceleration clause never appears without the trend clause it
    refines, since both share the 0.50 trajectory floor).
    """
    clauses = [
        c
        for c in (
            trend_descriptor(trend_ratio),
            acceleration_descriptor(trend_ratio, accel_ratio),
        )
        if c
    ]
    return f"{base} ・ {' ・ '.join(clauses)}" if clauses else base


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    """Extract the run thread_id from a LangGraph RunnableConfig (or '').

    The thread_id lives in the run config (``configurable.thread_id``), not in
    ``SaiseiState``, so audit call sites read it here to key the hash chain.
    """
    if not config:
        return ""
    configurable = config.get("configurable") or {}
    return str(configurable.get("thread_id", "") or "")


def classifier_node(state: SaiseiState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Classify the debtor from the assessed signals.

    Applies the five-category FSA classification (金融検査マニュアル) and sets
    the 要管理先 ``special_attention`` sub-flag deterministically.

    Args:
        state: Current graph state (uses EWS score, gap, TDB score,
               is_insolvent, net_worth).
        config: LangGraph run config (injected); used only to read the thread_id
               for the best-effort audit event.

    Returns:
        Partial state update with ``fsa_classification`` and
        ``special_attention``.
    """
    classification, special_attention = classify(
        ews_score=state.ews_score,
        working_capital_gap=state.working_capital_gap,
        tdb_score=state.tdb_score,
        is_insolvent=state.is_insolvent,
        net_worth=state.net_worth,
    )
    # Derive the trajectory ratio from the SAME monthly series the EWS trend
    # signal uses, so the classification reason can name WHY the slope matters
    # (sustained slide vs one-off dip) for actionable bands. Deterministic; it
    # changes no threshold and no decision -- only the explanatory prose.
    trend_ratio = _deterioration_ratio(state.shisanhyo) if state.shisanhyo else None
    # Second-order read (step 3): is that decline steepening? Same series,
    # explanation-path only -- changes no threshold and no decision.
    accel_ratio = _acceleration_ratio(state.shisanhyo) if state.shisanhyo else None
    reason = classification_reason(
        classification,
        ews_score=state.ews_score,
        working_capital_gap=state.working_capital_gap,
        tdb_score=state.tdb_score,
        is_insolvent=state.is_insolvent,
        net_worth=state.net_worth,
        trend_ratio=trend_ratio,
        accel_ratio=accel_ratio,
    )
    _log.info(
        "classifier.classified",
        fsa_classification=classification.value,
        kanji=classification.kanji,
        ews_score=state.ews_score,
        working_capital_gap=state.working_capital_gap,
        tdb_score=state.tdb_score,
        is_insolvent=state.is_insolvent,
        net_worth=state.net_worth,
        special_attention=special_attention,
    )
    # Feature 7: best-effort immutable audit record of the classification. Never
    # fatal, mutates no graph state, offline no-op when no audit backend is set.
    profile = state.company_profile
    record_event(
        AuditEventType.CLASSIFICATION,
        state=state,
        thread_id=_thread_id_from_config(config),
        payload={
            "fsa_classification": classification.value,
            "special_attention": special_attention,
            "ews_score": state.ews_score,
            "classification_reason": reason,
            "ews_breakdown": [s.__dict__ for s in compute_ews_breakdown(state.shisanhyo)],
            "working_capital_gap": state.working_capital_gap,
            "tdb_score": state.tdb_score,
            "is_insolvent": state.is_insolvent,
            "net_worth": state.net_worth,
            # Feature 7 (fairness review): record the borrower's industry +
            # region so the after-the-fact bias / fairness analysis can build its
            # corpus straight from the ledger (the source of truth for what was
            # classified). Additive payload keys; advisory-use only.
            "industry": getattr(profile, "industry", "") if profile else "",
            "prefecture": getattr(profile, "prefecture", "") if profile else "",
        },
    )

    # Feature 8.1: best-effort opt-in Portfolio watchlist snapshot. Like the
    # audit record above it is write-only, never fatal, mutates no graph state,
    # and is an offline no-op when no portfolio backend is configured
    # (NullPortfolioStore). Recorded here because ews_score + fsa_classification
    # are now final, so the snapshot reflects the borrower's latest assessment.
    record_snapshot(state=state)

    return {
        "fsa_classification": classification,
        "special_attention": special_attention,
        "classification_reason": reason,
    }
