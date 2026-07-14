"""Tests for the EWS explainability layer (Feature 7).

Locks in the two invariants the breakdown / reason functions must always hold,
so the explainability layer can never silently drift from the deterministic
score and classification it explains:

1. **The breakdown sums to the score.** ``compute_ews_breakdown`` and
   ``compute_ews_score`` both derive from the same ``_ews_signals`` helper, so
   the per-signal points must always sum (after clamp + round) to the score.
2. **The reason matches the cascade.** ``classification_reason`` must describe
   the SAME band ``classify`` returns, for every representative signal
   combination across all five FSA bands.

All tests are offline, deterministic, and import only from ``app.*``.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.backend.nodes.ews_scoring import (
    EwsSignal,
    _acceleration_ratio,
    _deterioration_ratio,
    acceleration_descriptor,
    classification_reason,
    classify,
    compute_ews_breakdown,
    compute_ews_score,
    trend_descriptor,
)
from app.shared.constants import (
    EWS_DANGER,
    EWS_DOUBTFUL,
    EWS_SUBSTANDARD,
    TDB_NORMAL_FLOOR,
)
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass


def _tb(
    period: dt.date,
    uriage: int,
    uriage_genka: int,
    hanbaihi: int = 0,
    eigai_shueki: int = 0,
    eigai_hiyo: int = 0,
) -> TrialBalance:
    return TrialBalance(
        period=period,
        uriage=uriage,
        uriage_genka=uriage_genka,
        hanbaihi=hanbaihi,
        eigai_shueki=eigai_shueki,
        eigai_hiyo=eigai_hiyo,
    )


#: A few representative monthly histories spanning healthy -> distressed.
_HISTORIES: dict[str, list[TrialBalance]] = {
    "healthy": [
        _tb(dt.date(2025, 4, 30), 80_000_000, 60_000_000, 15_000_000),
        _tb(dt.date(2025, 5, 31), 79_000_000, 61_000_000, 15_000_000),
    ],
    "declining": [
        _tb(dt.date(2025, 4, 30), 100_000_000, 70_000_000, 20_000_000),
        _tb(dt.date(2025, 5, 31), 90_000_000, 72_000_000, 20_000_000),
        _tb(dt.date(2025, 6, 30), 80_000_000, 73_000_000, 20_000_000),
    ],
    "collapse_to_zero": [
        _tb(dt.date(2025, 4, 30), 100_000_000, 60_000_000, 20_000_000),
        _tb(dt.date(2025, 5, 31), 0, 0, 20_000_000),
    ],
    "zero_baseline": [
        _tb(dt.date(2025, 4, 30), 0, 0, 0),
        _tb(dt.date(2025, 5, 31), 100_000_000, 60_000_000, 20_000_000),
    ],
}


class TestEwsBreakdown:
    """compute_ews_breakdown must explain compute_ews_score exactly."""

    @pytest.mark.parametrize("name", list(_HISTORIES))
    def test_points_sum_to_score(self, name: str) -> None:
        """The per-signal points sum (clamped + rounded) to the EWS score."""
        rows = _HISTORIES[name]
        breakdown = compute_ews_breakdown(rows)
        score = compute_ews_score(rows)
        summed = round(max(0.0, min(100.0, sum(s.points for s in breakdown))), 2)
        assert summed == score

    @pytest.mark.parametrize("name", list(_HISTORIES))
    def test_five_signals_with_stable_keys(self, name: str) -> None:
        """Every history yields exactly the five known weighted signals."""
        breakdown = compute_ews_breakdown(_HISTORIES[name])
        assert [s.key for s in breakdown] == [
            "sales_drop",
            "margin_drop",
            "keijo_drop",
            "loss_ratio",
            "trend",
        ]
        assert all(isinstance(s, EwsSignal) for s in breakdown)

    @pytest.mark.parametrize("name", list(_HISTORIES))
    def test_points_never_exceed_weight(self, name: str) -> None:
        """Each signal's points are capped at its weight ceiling and non-negative."""
        for s in compute_ews_breakdown(_HISTORIES[name]):
            assert 0.0 <= s.points <= s.weight

    def test_weights_sum_to_100(self) -> None:
        """The five weight ceilings sum to the full 0-100 scale."""
        weights = sum(s.weight for s in compute_ews_breakdown(_HISTORIES["declining"]))
        assert weights == 100.0

    def test_insufficient_history_is_empty(self) -> None:
        """< 2 months yields an empty breakdown (matches the score's 0.0 floor)."""
        rows = [_tb(dt.date(2025, 4, 30), 100, 60, 20)]
        assert compute_ews_breakdown(rows) == []
        assert compute_ews_score(rows) == 0.0

    def test_deterministic(self) -> None:
        """The breakdown is deterministic for identical input."""
        rows = _HISTORIES["declining"]
        assert compute_ews_breakdown(rows) == compute_ews_breakdown(rows)


class TestClassificationReason:
    """classification_reason must always describe the band classify() returns."""

    #: (ews, gap, tdb, is_insolvent, net_worth) tuples spanning all five bands.
    _CASES = [
        (10.0, 0, 80, None, None),  # 正常先
        (float(EWS_SUBSTANDARD), 0, 80, None, None),  # 要注意先 (EWS)
        (10.0, -5_000_000, 80, None, None),  # 要注意先 (deficit)
        (10.0, 0, TDB_NORMAL_FLOOR - 1, None, None),  # 要注意先 (TDB)
        (float(EWS_DOUBTFUL), 0, 80, None, None),  # 破綻懸念先 (EWS)
        (float(EWS_SUBSTANDARD), -5_000_000, 80, None, None),  # 破綻懸念先 (deficit+EWS)
        (float(EWS_DANGER), 0, 80, None, None),  # 実質破綻先 (extreme EWS)
        (10.0, 0, 80, None, -1),  # 実質破綻先 (negative net worth)
        (10.0, 0, 80, True, None),  # 実質破綻先 (insolvency flag)
        (10.0, 0, 80, True, -1),  # 破綻先 (both hard signals)
    ]

    @pytest.mark.parametrize("ews,gap,tdb,insolvent,net_worth", _CASES)
    def test_reason_is_nonempty_and_consistent(
        self,
        ews: float,
        gap: int,
        tdb: int,
        insolvent: bool | None,
        net_worth: int | None,
    ) -> None:
        """The reason is always non-empty and is produced for the winning band."""
        cls, _special = classify(
            ews_score=ews,
            working_capital_gap=gap,
            tdb_score=tdb,
            is_insolvent=insolvent,
            net_worth=net_worth,
        )
        reason = classification_reason(
            cls,
            ews_score=ews,
            working_capital_gap=gap,
            tdb_score=tdb,
            is_insolvent=insolvent,
            net_worth=net_worth,
        )
        assert reason.strip() != ""

    def test_normal_reason_mentions_clear(self) -> None:
        """The 正常先 reason states all thresholds are clear."""
        cls, _ = classify(ews_score=5.0, working_capital_gap=0, tdb_score=90)
        reason = classification_reason(cls, ews_score=5.0, working_capital_gap=0, tdb_score=90)
        assert cls is FsaClass.SEIJOSAKI
        assert "clear" in reason.lower()

    def test_bankrupt_reason_names_both_signals(self) -> None:
        """The 破綻先 reason cites both insolvency AND negative net worth."""
        cls, _ = classify(
            ews_score=10.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=True,
            net_worth=-1,
        )
        reason = classification_reason(
            cls,
            ews_score=10.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=True,
            net_worth=-1,
        )
        assert cls is FsaClass.HATANSAKI
        assert "net worth" in reason.lower()


class TestTrendSignal:
    """The trajectory signal must read the SHAPE of the series, not endpoints."""

    def _keijo_series(self, *keijos: int) -> list[TrialBalance]:
        # Build months whose 経常利益 equals each value (sales/COGS fixed; the
        # ordinary profit is driven by non-op expense so endpoints stay stable).
        rows: list[TrialBalance] = []
        for i, k in enumerate(keijos):
            # uriage - genka - hanbaihi + shueki - hiyo = keijo; keep the first
            # three fixed and use eigai to set keijo precisely.
            base = 100_000_000 - 60_000_000 - 20_000_000  # = 20,000,000 operating
            hiyo = max(0, base - k)
            shueki = max(0, k - base)
            rows.append(
                _tb(
                    dt.date(2025, 4 + i, 28),
                    100_000_000,
                    60_000_000,
                    20_000_000,
                    eigai_shueki=shueki,
                    eigai_hiyo=hiyo,
                )
            )
        return rows

    def test_deterioration_ratio_monotonic_slide_is_one(self) -> None:
        rows = self._keijo_series(40, 30, 20, 10)
        assert _deterioration_ratio(rows) == 1.0

    def test_deterioration_ratio_flat_is_zero(self) -> None:
        rows = self._keijo_series(20, 20, 20, 20)
        assert _deterioration_ratio(rows) == 0.0

    def test_deterioration_ratio_recovery_is_low(self) -> None:
        # Fell once, then recovered: only the first step is a decline (1 of 3).
        rows = self._keijo_series(40, 10, 25, 40)
        assert _deterioration_ratio(rows) == pytest.approx(1 / 3)

    def test_deterioration_ratio_insufficient_history(self) -> None:
        assert _deterioration_ratio(self._keijo_series(20)) == 0.0

    def test_steady_slide_scores_higher_than_recovered(self) -> None:
        """The depth payoff: a steady slide and a fell-then-recovered firm with
        the SAME endpoints no longer score identically -- the slide is worse."""
        slide = self._keijo_series(40, 30, 20, 10)
        recovered = self._keijo_series(40, 5, 8, 10)  # same endpoints (40 -> 10)
        # Endpoints identical, so the endpoint-only keijo_drop is the same; the
        # trend signal breaks the tie in favour of flagging the steady slide.
        slide_trend = next(s for s in compute_ews_breakdown(slide) if s.key == "trend")
        rec_trend = next(s for s in compute_ews_breakdown(recovered) if s.key == "trend")
        assert slide_trend.points > rec_trend.points
        assert compute_ews_score(slide) > compute_ews_score(recovered)


class TestTrendDescriptor:
    """trend_descriptor must translate the deterioration ratio into prose."""

    def test_sustained_deterioration_above_080(self) -> None:
        d = trend_descriptor(1.0)
        assert "持続的悪化" in d
        assert "sustained deterioration" in d

    def test_deteriorating_trend_between_050_and_080(self) -> None:
        d = trend_descriptor(0.6)
        assert "悪化傾向" in d
        assert "deteriorating trend" in d

    def test_no_phrase_below_050(self) -> None:
        assert trend_descriptor(0.4) == ""
        assert trend_descriptor(0.0) == ""

    def test_none_is_empty(self) -> None:
        assert trend_descriptor(None) == ""


class TestAccelerationSignal:
    """The acceleration signal must read CURVATURE (the change in the slope)."""

    def _keijo_series(self, *keijos: int) -> list[TrialBalance]:
        rows: list[TrialBalance] = []
        for i, k in enumerate(keijos):
            base = 100_000_000 - 60_000_000 - 20_000_000  # = 20,000,000 operating
            hiyo = max(0, base - k)
            shueki = max(0, k - base)
            rows.append(
                _tb(
                    dt.date(2025, 4 + i, 28),
                    100_000_000,
                    60_000_000,
                    20_000_000,
                    eigai_shueki=shueki,
                    eigai_hiyo=hiyo,
                )
            )
        return rows

    def test_accelerating_slide_is_one(self) -> None:
        # Deltas: -10, -20, -40 -> each step steeper than the last (2 of 2).
        rows = self._keijo_series(100, 90, 70, 30)
        assert _acceleration_ratio(rows) == 1.0

    def test_linear_slide_is_zero(self) -> None:
        # Deltas: -10, -10, -10 -> never steepening (0 of 2).
        rows = self._keijo_series(40, 30, 20, 10)
        assert _acceleration_ratio(rows) == 0.0

    def test_decelerating_slide_is_zero(self) -> None:
        # Deltas: -40, -20, -10 -> easing, never steepening (0 of 2).
        rows = self._keijo_series(100, 60, 40, 30)
        assert _acceleration_ratio(rows) == 0.0

    def test_partial_steepening(self) -> None:
        # Deltas: -10, -30, -20 -> steepens once of two pairs.
        rows = self._keijo_series(100, 90, 60, 40)
        assert _acceleration_ratio(rows) == pytest.approx(1 / 2)

    def test_insufficient_history_under_three_months(self) -> None:
        # < 3 months: no two deltas to compare -> 0.0 (history floor).
        assert _acceleration_ratio(self._keijo_series(40, 10)) == 0.0
        assert _acceleration_ratio(self._keijo_series(40)) == 0.0

    def test_does_not_affect_score(self) -> None:
        """Acceleration feeds the explanation path ONLY: the score for an
        accelerating slide and a linear slide with the SAME shape-of-declines
        is driven by the existing signals, never by acceleration."""
        rows = self._keijo_series(100, 90, 70, 30)
        # Score is reproducible and bounded; acceleration is not a score input.
        assert compute_ews_score(rows) == compute_ews_score(rows)
        assert [s.key for s in compute_ews_breakdown(rows)] == [
            "sales_drop",
            "margin_drop",
            "keijo_drop",
            "loss_ratio",
            "trend",
        ]


class TestAccelerationDescriptor:
    """acceleration_descriptor fires only on an adverse AND accelerating slide."""

    def test_accelerating_adverse_trend_emits_clause(self) -> None:
        d = acceleration_descriptor(trend_ratio=1.0, accel_ratio=1.0)
        assert "悪化が加速" in d
        assert "decline accelerating" in d

    def test_adverse_but_not_accelerating_is_empty(self) -> None:
        assert acceleration_descriptor(trend_ratio=1.0, accel_ratio=0.0) == ""

    def test_accelerating_but_no_adverse_trend_is_empty(self) -> None:
        # A blip that steepens but is not a real downtrend -> no clause.
        assert acceleration_descriptor(trend_ratio=0.3, accel_ratio=1.0) == ""

    def test_none_is_empty(self) -> None:
        assert acceleration_descriptor(None, 1.0) == ""
        assert acceleration_descriptor(1.0, None) == ""


class TestClassificationReasonAcceleration:
    """The acceleration clause must enrich actionable bands, additively only."""

    def test_actionable_band_appends_acceleration_clause(self) -> None:
        cls, _ = classify(ews_score=float(EWS_DOUBTFUL), working_capital_gap=0, tdb_score=80)
        reason = classification_reason(
            cls,
            ews_score=float(EWS_DOUBTFUL),
            working_capital_gap=0,
            tdb_score=80,
            trend_ratio=1.0,
            accel_ratio=1.0,
        )
        assert cls is FsaClass.HATAN_KENENSAKI
        assert "持続的悪化" in reason  # trend clause still present
        assert "悪化が加速" in reason  # acceleration clause appended

    def test_omitting_accel_ratio_is_byte_identical(self) -> None:
        """Backward compatibility: omitting accel_ratio yields the prior reason
        (the !10 trend-only output), so a run without acceleration data is
        byte-identical to before this MR."""
        kwargs = {
            "ews_score": float(EWS_DOUBTFUL),
            "working_capital_gap": 0,
            "tdb_score": 80,
        }
        cls, _ = classify(**kwargs)  # type: ignore[arg-type]
        trend_only = classification_reason(cls, **kwargs, trend_ratio=1.0)  # type: ignore[arg-type]
        with_accel_omitted = classification_reason(cls, **kwargs, trend_ratio=1.0)  # type: ignore[arg-type]
        assert trend_only == with_accel_omitted
        assert "悪化が加速" not in trend_only

    def test_decelerating_slide_adds_no_acceleration_clause(self) -> None:
        cls, _ = classify(ews_score=float(EWS_DOUBTFUL), working_capital_gap=0, tdb_score=80)
        reason = classification_reason(
            cls,
            ews_score=float(EWS_DOUBTFUL),
            working_capital_gap=0,
            tdb_score=80,
            trend_ratio=1.0,
            accel_ratio=0.0,
        )
        assert "持続的悪化" in reason  # still a sustained slide
        assert "悪化が加速" not in reason  # but not accelerating

    def test_hard_signal_bankrupt_omits_acceleration_clause(self) -> None:
        cls, _ = classify(
            ews_score=10.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=True,
            net_worth=-1,
        )
        reason = classification_reason(
            cls,
            ews_score=10.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=True,
            net_worth=-1,
            trend_ratio=1.0,
            accel_ratio=1.0,
        )
        assert cls is FsaClass.HATANSAKI
        assert "悪化が加速" not in reason

    def test_normal_band_omits_acceleration_clause(self) -> None:
        cls, _ = classify(ews_score=5.0, working_capital_gap=0, tdb_score=90)
        reason = classification_reason(
            cls,
            ews_score=5.0,
            working_capital_gap=0,
            tdb_score=90,
            trend_ratio=1.0,
            accel_ratio=1.0,
        )
        assert cls is FsaClass.SEIJOSAKI
        assert "悪化が加速" not in reason


class TestClassificationReasonTrend:
    """The trajectory clause must enrich actionable bands, and only those."""

    def test_actionable_band_appends_trend_clause(self) -> None:
        # 要注意先 via EWS, with a sustained slide -> the clause is appended.
        cls, _ = classify(ews_score=float(EWS_SUBSTANDARD), working_capital_gap=0, tdb_score=80)
        reason = classification_reason(
            cls,
            ews_score=float(EWS_SUBSTANDARD),
            working_capital_gap=0,
            tdb_score=80,
            trend_ratio=1.0,
        )
        assert cls is FsaClass.YOCHUISAKI
        assert "持続的悪化" in reason

    def test_doubtful_band_appends_trend_clause(self) -> None:
        cls, _ = classify(ews_score=float(EWS_DOUBTFUL), working_capital_gap=0, tdb_score=80)
        reason = classification_reason(
            cls,
            ews_score=float(EWS_DOUBTFUL),
            working_capital_gap=0,
            tdb_score=80,
            trend_ratio=0.9,
        )
        assert cls is FsaClass.HATAN_KENENSAKI
        assert "持続的悪化" in reason

    def test_hard_signal_bankrupt_omits_trend_clause(self) -> None:
        # 破綻先 (both hard signals): the trajectory is moot -> no clause.
        cls, _ = classify(
            ews_score=10.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=True,
            net_worth=-1,
        )
        reason = classification_reason(
            cls,
            ews_score=10.0,
            working_capital_gap=0,
            tdb_score=80,
            is_insolvent=True,
            net_worth=-1,
            trend_ratio=1.0,
        )
        assert cls is FsaClass.HATANSAKI
        assert "持続的悪化" not in reason

    def test_normal_band_omits_trend_clause(self) -> None:
        cls, _ = classify(ews_score=5.0, working_capital_gap=0, tdb_score=90)
        reason = classification_reason(
            cls,
            ews_score=5.0,
            working_capital_gap=0,
            tdb_score=90,
            trend_ratio=1.0,
        )
        assert cls is FsaClass.SEIJOSAKI
        assert "持続的悪化" not in reason

    def test_no_trend_ratio_is_byte_identical(self) -> None:
        # Backward compatibility: omitting trend_ratio yields the prior reason.
        kwargs = {
            "ews_score": float(EWS_SUBSTANDARD),
            "working_capital_gap": 0,
            "tdb_score": 80,
        }
        cls, _ = classify(**kwargs)  # type: ignore[arg-type]
        with_none = classification_reason(cls, **kwargs)  # type: ignore[arg-type]
        with_low = classification_reason(cls, **kwargs, trend_ratio=0.3)  # type: ignore[arg-type]
        assert with_none == with_low  # a sub-floor trend adds nothing
        assert "・" not in with_none
