"""Keieisha Hosho (経営者保証) release + Jigyou Shoukei (事業承継) viability node.

Assesses whether a borrower can be RELEASED from the personal guarantee and
whether it is succession-ready, aligned with the FSA 'Keieisha Hosho ni Kansuru
Guideline' (経営者保証に関するガイドライン).

Runs UPSTREAM in the assessment phase for ALL borrowers (not only distressed ones).

All scoring is DETERMINISTIC — no LLM involvement. The three guideline conditions
are evaluated from data the engine already parses:

1. 法人個人分離 (Houjin-Kojin Bunri — separation of corporate/personal assets):
   FSA intent: BOTH abnormally high non-operating EXPENSE (owner-loan interest
   paid to the owner) AND abnormally high non-operating INCOME (owner renting
   personal property to the company, or company lending to the owner) are red
   flags for poor corporate/personal separation.

   Proxy (bidirectional): measure total non-operating scale relative to sales.
     non_op_total  = |avg_eigai_shueki| + |avg_eigai_hiyo|
     non_op_ratio  = non_op_total / max(avg_uriage, 1)
   A clean firm has small non-op items in BOTH directions → low ratio → high score.
   Large items in EITHER direction increase the ratio → penalized.

   Scoring:
     non_op_ratio <= BUNRI_CLEAN_THRESHOLD (2%)  → full 40 pts, bunri_met=True
     non_op_ratio >= BUNRI_DIRTY_THRESHOLD (10%) → 0 pts, bunri_met=False
     Between thresholds → linear interpolation, bunri_met=False

2. 財務基盤の強化 (Zaimu Kiban no Kyouka — financial-base strength):
   Reuses the existing EWS score and working-capital gap thresholds.
   - EWS < 40 AND gap >= 0 → full 35 pts.
   - EWS < 40 OR gap >= 0 → partial 17.5 pts.
   - Otherwise → 0 pts.

3. 適時適切な情報開示 (Tekiji Tekisetsu na Jouhou Kaiji — timely disclosure):
   Data-completeness / process check over available records.
   - 12 months of Shisanhyo → 10 pts.
   - TDB score present → 10 pts.
   - No errors in state → 5 pts.
   Total: 0-25 pts.

Hosho Kaijo Score = sum of three components (0-100, deterministic).
Succession readiness: EWS < 50 AND TDB score >= 55 AND no errors.

NOTE: ``from __future__ import annotations`` is intentionally NOT used here so
LangGraph can resolve the ``config`` node parameter type at ``add_node`` time
without emitting a spurious UserWarning about its annotation.
"""

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.backend.audit.audit_log import AuditEventType
from app.backend.audit.record import record_event
from app.backend.state import HoshoKaijoConditions, SaiseiState
from app.shared.constants import (
    EWS_SUBSTANDARD as _EWS_STRONG_THRESHOLD,
)
from app.shared.constants import (
    HOSHO_ELIGIBLE_SCORE as _ELIGIBLE_SCORE,
)
from app.shared.constants import (
    HOSHO_SUCCESSION_EWS_MAX as _SUCCESSION_EWS_MAX,
)
from app.shared.constants import (
    HOSHO_SUCCESSION_TDB_MIN as _SUCCESSION_TDB_MIN,
)
from app.shared.constants import (
    HOSHO_WEIGHT_BUNRI as _WEIGHT_BUNRI,
)
from app.shared.constants import (
    HOSHO_WEIGHT_ZAIMU as _WEIGHT_ZAIMU,
)
from app.shared.logging import get_logger

__all__ = ["keieisha_hosho_node", "assess_hosho_kaijo"]

_log = get_logger(__name__)


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    """Extract the run thread_id from a LangGraph RunnableConfig (or '').

    The thread_id lives in the run config (``configurable.thread_id``), not in
    ``SaiseiState``, so audit call sites read it here to key the hash chain
    (same helper as ``ews_scoring._thread_id_from_config``).
    """
    if not config:
        return ""
    configurable = config.get("configurable") or {}
    return str(configurable.get("thread_id", "") or "")


# ---------------------------------------------------------------------------
# Scoring weights and thresholds.
#
# Single source of truth: app.shared.constants. These module-level aliases keep
# the local references readable while ensuring there is no drift between this
# node and the shared constants (previously these were redefined locally).
# ---------------------------------------------------------------------------

# Thresholds local to this node (no shared equivalent).
#
# Bunri (法人個人分離) bidirectional non-op scale thresholds.
# non_op_ratio = (|eigai_shueki| + |eigai_hiyo|) / max(uriage, 1)
#   <= BUNRI_CLEAN_THRESHOLD → full score (clean separation)
#   >= BUNRI_DIRTY_THRESHOLD → zero score (poor separation)
#   between → linear interpolation
_BUNRI_CLEAN_THRESHOLD: float = 0.02  # 2% of sales → clean
_BUNRI_DIRTY_THRESHOLD: float = 0.10  # 10% of sales → fully penalized
_FULL_SHISANHYO_MONTHS: int = 12  # 12 months = full disclosure

# Disclosure (kaiji) sub-component point values. Their sum is the kaiji pillar's
# achievable maximum and is the single source of truth for BOTH the score and
# the kaiji_met verdict (see below). It must equal HOSHO_WEIGHT_KAIJI; pinning
# kaiji_met to this local sum — rather than to the external HOSHO_WEIGHT_KAIJI —
# keeps the "complete disclosure" verdict and the score from drifting apart if
# the shared weight is ever retuned (the other two pillars already derive their
# full score from their weight constant; kaiji's sub-points are hardcoded).
_KAIJI_POINTS_SHISANHYO: float = 10.0
_KAIJI_POINTS_TDB: float = 10.0
_KAIJI_POINTS_NO_ERRORS: float = 5.0
_KAIJI_MAX: float = _KAIJI_POINTS_SHISANHYO + _KAIJI_POINTS_TDB + _KAIJI_POINTS_NO_ERRORS


def assess_hosho_kaijo(
    shisanhyo_count: int,
    avg_eigai_shueki: float,
    avg_eigai_hiyo: float,
    avg_uriage: float,
    ews_score: float | None,
    working_capital_gap: int | None,
    tdb_score: int | None,
    error_count: int,
) -> HoshoKaijoConditions:
    """Compute the Hosho Kaijo conditions deterministically.

    All inputs are derived from data the engine already parses.
    No LLM involvement; every figure is rule-based.

    Args:
        shisanhyo_count: Number of monthly trial balances available.
        avg_eigai_shueki: Average monthly non-operating income (JPY).
        avg_eigai_hiyo: Average monthly non-operating expenses (JPY).
        avg_uriage: Average monthly sales / 売上 (JPY), used as operating-scale
            reference for the bunri bidirectional non-op penalty.
        ews_score: EWS score (0-100), or None.
        working_capital_gap: Shikin Kuri gap in yen (negative = deficit), or None.
        tdb_score: TDB credit score (1-100), or None.
        error_count: Number of errors accumulated in state.

    Returns:
        Structured :class:`HoshoKaijoConditions` with scores and directives.
    """
    # ------------------------------------------------------------------
    # Condition 1: 法人個人分離 (Houjin-Kojin Bunri)
    #
    # FSA intent: BOTH abnormally high non-operating EXPENSE (owner-loan
    # interest) AND abnormally high non-operating INCOME (owner renting
    # property to the company) are red flags for poor separation.
    #
    # Bidirectional proxy: measure total non-op scale relative to sales.
    #   non_op_ratio = (|shueki| + |hiyo|) / max(uriage, 1)
    # Small ratio → clean separation → full score.
    # Large ratio in EITHER direction → penalized.
    # ------------------------------------------------------------------
    non_op_total = abs(avg_eigai_shueki) + abs(avg_eigai_hiyo)
    non_op_ratio = non_op_total / max(avg_uriage, 1.0)

    if non_op_ratio <= _BUNRI_CLEAN_THRESHOLD:
        # Clean: non-op items are negligible relative to sales.
        bunri_met = True
        bunri_score = _WEIGHT_BUNRI
        bunri_directive = "法人個人分離は適切です。現状を維持してください。"
    elif non_op_ratio >= _BUNRI_DIRTY_THRESHOLD:
        # Fully penalized: non-op items are abnormally large.
        bunri_met = False
        bunri_score = 0.0
        bunri_directive = (
            "営業外項目（Eigai Kamoku）が売上高に対して著しく大きく、"
            "法人個人分離が不十分である可能性があります。"
            "役員貸付金・役員借入金（Yakuin Kashitsuke / Kariire）を解消し、"
            "オーナーへの不動産賃貸・貸付取引を見直してください。"
            "法人口座と個人口座を完全に分離することが保証解除の条件です。"
        )
    else:
        # Partial: linear interpolation between clean and dirty thresholds.
        bunri_met = False
        # Score decreases linearly from _WEIGHT_BUNRI (at clean) to 0 (at dirty).
        span = _BUNRI_DIRTY_THRESHOLD - _BUNRI_CLEAN_THRESHOLD
        excess = non_op_ratio - _BUNRI_CLEAN_THRESHOLD
        bunri_score = round(_WEIGHT_BUNRI * (1.0 - excess / span), 2)
        bunri_directive = (
            "営業外収益または営業外費用（Eigai Shueki / Hiyo）が売上高に対して"
            f"相対的に大きい水準（{non_op_ratio:.1%}）です。"
            "役員貸付金・役員借入金（Yakuin Kashitsuke / Kariire）を解消し、"
            "オーナーとの取引を適正化してください。"
            "法人口座と個人口座の完全分離が保証解除の条件です。"
        )

    # ------------------------------------------------------------------
    # Condition 2: 財務基盤の強化 (Zaimu Kiban no Kyouka)
    # ------------------------------------------------------------------
    ews = ews_score or 0.0
    deficit = working_capital_gap is not None and working_capital_gap < 0
    ews_strong = ews < _EWS_STRONG_THRESHOLD
    gap_positive = not deficit

    if ews_strong and gap_positive:
        zaimu_met = True
        zaimu_score = _WEIGHT_ZAIMU
        zaimu_directive = "財務基盤は十分に強化されています。現状を維持してください。"
    elif ews_strong or gap_positive:
        zaimu_met = False
        zaimu_score = round(_WEIGHT_ZAIMU * 0.5, 2)
        if not ews_strong:
            zaimu_directive = (
                f"EWSスコア（{ews:.1f}）が閾値（{_EWS_STRONG_THRESHOLD}）を超えています。"
                "売上回復・原価低減・SG&A削減により経常利益を改善してください。"
            )
        else:
            zaimu_directive = (
                "資金繰りギャップ（Shikin Kuri Gap）が赤字です。"
                "売掛回収日数の短縮と買掛支払日数の延長により資金繰りを改善してください。"
            )
    else:
        zaimu_met = False
        zaimu_score = 0.0
        zaimu_directive = (
            f"EWSスコア（{ews:.1f}）が高く、かつ資金繰りが赤字です。"
            "抜本的な収益改善と資金繰り対策が必要です。"
            "EWSスコアを40未満に、資金繰りギャップをゼロ以上にすることが保証解除の条件です。"
        )

    # ------------------------------------------------------------------
    # Condition 3: 適時適切な情報開示 (Tekiji Tekisetsu na Jouhou Kaiji)
    # ------------------------------------------------------------------
    kaiji_score = 0.0
    kaiji_directives: list[str] = []

    # 12 months of Shisanhyo → 10 pts.
    if shisanhyo_count >= _FULL_SHISANHYO_MONTHS:
        kaiji_score += _KAIJI_POINTS_SHISANHYO
    else:
        kaiji_directives.append(
            f"試算表（Shisanhyo）が{shisanhyo_count}ヶ月分しかありません。"
            f"12ヶ月分の月次試算表を提出してください。"
        )

    # TDB score present → 10 pts.
    if tdb_score is not None:
        kaiji_score += _KAIJI_POINTS_TDB
    else:
        kaiji_directives.append("TDB信用スコアが取得できていません。TDB審査を受けてください。")

    # No errors in state → 5 pts.
    if error_count == 0:
        kaiji_score += _KAIJI_POINTS_NO_ERRORS
    else:
        kaiji_directives.append(
            f"審査プロセスに{error_count}件のエラーがあります。エラーを解消してください。"
        )

    # "Complete disclosure" = every sub-check satisfied, i.e. the kaiji pillar's
    # own achievable maximum. Comparing to _KAIJI_MAX (the sum of the sub-points
    # above) instead of the external HOSHO_WEIGHT_KAIJI keeps met and score from
    # desyncing if the shared weight is retuned.
    kaiji_met = kaiji_score >= _KAIJI_MAX
    if kaiji_met:
        kaiji_directive = "情報開示は適切です。現状を維持してください。"
    else:
        kaiji_directive = (
            " ".join(kaiji_directives) if kaiji_directives else "情報開示を改善してください。"
        )

    # ------------------------------------------------------------------
    # Ordered directives (priority: bunri > zaimu > kaiji)
    # ------------------------------------------------------------------
    ordered: list[str] = []
    if not bunri_met:
        ordered.append(f"[P1 法人個人分離] {bunri_directive}")
    if not zaimu_met:
        ordered.append(f"[P2 財務基盤] {zaimu_directive}")
    if not kaiji_met:
        ordered.append(f"[P3 情報開示] {kaiji_directive}")

    return HoshoKaijoConditions(
        bunri_met=bunri_met,
        bunri_score=bunri_score,
        bunri_directive=bunri_directive,
        zaimu_met=zaimu_met,
        zaimu_score=zaimu_score,
        zaimu_directive=zaimu_directive,
        kaiji_met=kaiji_met,
        kaiji_score=kaiji_score,
        kaiji_directive=kaiji_directive,
        ordered_directives=ordered,
    )


def _assess_succession_readiness(
    ews_score: float | None,
    tdb_score: int | None,
    error_count: int,
) -> bool:
    """Determine succession readiness deterministically.

    Succession-ready if:
    - EWS score < 50 (business is not severely distressed)
    - TDB score >= 55 (external creditworthiness acceptable)
    - No errors in state (clean record)

    Args:
        ews_score: EWS score (0-100), or None.
        tdb_score: TDB credit score (1-100), or None.
        error_count: Number of errors in state.

    Returns:
        True if succession-ready, False otherwise.
    """
    ews = ews_score or 0.0
    if ews >= _SUCCESSION_EWS_MAX:
        return False
    if tdb_score is None or tdb_score < _SUCCESSION_TDB_MIN:
        return False
    return error_count == 0


def keieisha_hosho_node(state: SaiseiState, config: RunnableConfig | None = None) -> dict[str, Any]:
    """Assess guarantee-release eligibility and succession readiness.

    Runs for ALL borrowers in the assessment path (after classifier, before
    the turnaround branch). Pure function: takes state, returns result.

    Args:
        state: Current graph state (uses Shisanhyo, EWS, gap, TDB score).
        config: LangGraph run config (injected); used only to read the thread_id
               for the best-effort guarantee_release audit event so it is
               chained under the SAME thread as the classification / decision
               events (the thread_id lives in the run config, not SaiseiState).

    Returns:
        Partial state update with ``hosho_kaijo_score``, ``hosho_kaijo_conditions``,
        and ``succession_ready``.
    """
    # Derive inputs from existing state data.
    shisanhyo_count = len(state.shisanhyo)

    if shisanhyo_count > 0:
        avg_eigai_shueki = sum(int(tb.eigai_shueki) for tb in state.shisanhyo) / shisanhyo_count
        avg_eigai_hiyo = sum(int(tb.eigai_hiyo) for tb in state.shisanhyo) / shisanhyo_count
        avg_uriage = sum(int(tb.uriage) for tb in state.shisanhyo) / shisanhyo_count
    else:
        avg_eigai_shueki = 0.0
        avg_eigai_hiyo = 0.0
        avg_uriage = 0.0

    conditions = assess_hosho_kaijo(
        shisanhyo_count=shisanhyo_count,
        avg_eigai_shueki=avg_eigai_shueki,
        avg_eigai_hiyo=avg_eigai_hiyo,
        avg_uriage=avg_uriage,
        ews_score=state.ews_score,
        working_capital_gap=state.working_capital_gap,
        tdb_score=state.tdb_score,
        error_count=len(state.errors),
    )

    hosho_kaijo_score = round(
        conditions.bunri_score + conditions.zaimu_score + conditions.kaiji_score, 2
    )

    succession_ready = _assess_succession_readiness(
        ews_score=state.ews_score,
        tdb_score=state.tdb_score,
        error_count=len(state.errors),
    )

    # Deterministic release-eligibility verdict from the shared threshold.
    hosho_kaijo_eligible = hosho_kaijo_score >= _ELIGIBLE_SCORE

    _log.info(
        "keieisha_hosho.assessed",
        hosho_kaijo_score=hosho_kaijo_score,
        hosho_kaijo_eligible=hosho_kaijo_eligible,
        bunri_met=conditions.bunri_met,
        zaimu_met=conditions.zaimu_met,
        kaiji_met=conditions.kaiji_met,
        succession_ready=succession_ready,
    )

    result: dict[str, Any] = {
        "hosho_kaijo_score": hosho_kaijo_score,
        "hosho_kaijo_conditions": conditions,
        "hosho_kaijo_eligible": hosho_kaijo_eligible,
        "succession_ready": succession_ready,
    }

    # Feature 7 (audit log, step 5): record an immutable `guarantee_release`
    # event. Side-record ONLY — emitted AFTER ``result`` is built and never
    # mutates it, so the returned graph state is byte-identical with or without
    # an audit sink (spine invariance). Best-effort and never fatal. Offline
    # (no audit_dsn) -> NullAuditSink -> no-op.
    try:
        record_event(
            AuditEventType.GUARANTEE_RELEASE,
            state=state,
            thread_id=_thread_id_from_config(config),
            payload={
                "hosho_kaijo_score": hosho_kaijo_score,
                "hosho_kaijo_eligible": hosho_kaijo_eligible,
                "bunri_met": conditions.bunri_met,
                "bunri_score": conditions.bunri_score,
                "zaimu_met": conditions.zaimu_met,
                "zaimu_score": conditions.zaimu_score,
                "kaiji_met": conditions.kaiji_met,
                "kaiji_score": conditions.kaiji_score,
                "succession_ready": succession_ready,
            },
        )
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never fatal
        _log.warning("keieisha_hosho.audit_record_failed", error=str(exc))

    return result
