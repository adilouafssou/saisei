"""MR3 — evidence-based reconciliation-threshold calibration tests.

Covers the pure analysis surface fully offline:
- empty / unlabelled corpora,
- precision math,
- both floors (precision, min-samples) blocking independently,
- smallest-qualifying preference,
- custom thresholds,
- malformed-input hardening (bool / non-int / out-of-range distances, missing
  fields), including honest total/skipped accounting in the report + rationale,
- model round-trip and byte-stable determinism.
"""

from __future__ import annotations

from typing import Any

from app.backend.analysis.threshold_calibration import (
    CalibrationReport,
    calibrate_reconciliation_threshold,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outcome(band_distance: Any, verdict: str = "") -> dict[str, Any]:
    return {
        "strategy_title": "s",
        "deterministic_band": "high",
        "llm_band": "low",
        "band_distance": band_distance,
        "banker_decision": "approve",
        "banker_verdict": verdict,
    }


def _many(
    band_distance: int, *, useful: int, neither: int, unlabelled: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    out += [_outcome(band_distance, "floor") for _ in range(useful)]
    out += [_outcome(band_distance, "neither") for _ in range(neither)]
    out += [_outcome(band_distance, "") for _ in range(unlabelled)]
    return out


def _stats(report: CalibrationReport, distance: int) -> Any:
    return next(s for s in report.per_distance if s.band_distance == distance)


# ---------------------------------------------------------------------------
# Empty / unlabelled corpora.
# ---------------------------------------------------------------------------


def test_empty_corpus_no_recommendation() -> None:
    report = calibrate_reconciliation_threshold([])
    assert report.total_outcomes == 0
    assert report.skipped_outcomes == 0
    assert report.recommended_band_distance is None
    assert "No outcomes" in report.rationale
    # Always reports both valid distances.
    assert [s.band_distance for s in report.per_distance] == [1, 2]


def test_all_unlabelled_no_recommendation() -> None:
    report = calibrate_reconciliation_threshold([_outcome(1), _outcome(2), _outcome(1)])
    assert report.total_outcomes == 3
    assert report.recommended_band_distance is None
    assert _stats(report, 1).precision is None  # labelled == 0
    assert _stats(report, 1).unlabelled == 2
    assert "unlabelled" in report.rationale


# ---------------------------------------------------------------------------
# Precision math.
# ---------------------------------------------------------------------------


def test_precision_excludes_unlabelled_counts_neither() -> None:
    # distance 2: 7 floor + 3 neither + 5 unlabelled -> precision 7/10 = 0.7.
    report = calibrate_reconciliation_threshold(
        _many(2, useful=7, neither=3, unlabelled=5), min_samples=10
    )
    s2 = _stats(report, 2)
    assert s2.total == 15
    assert s2.labelled == 10
    assert s2.useful == 7
    assert s2.unlabelled == 5
    assert s2.precision == 0.7


# ---------------------------------------------------------------------------
# Floors block independently.
# ---------------------------------------------------------------------------


def test_min_samples_floor_blocks_high_precision() -> None:
    # Perfect precision but only 3 labelled < min_samples (10) -> no rec.
    report = calibrate_reconciliation_threshold(_many(1, useful=3, neither=0, unlabelled=0))
    assert _stats(report, 1).precision == 1.0
    assert _stats(report, 1).meets_target is False
    assert report.recommended_band_distance is None


def test_precision_floor_blocks_large_sample() -> None:
    # 20 labelled (clears samples) but precision 0.5 < target (0.7) -> no rec.
    report = calibrate_reconciliation_threshold(_many(1, useful=10, neither=10, unlabelled=0))
    assert _stats(report, 1).precision == 0.5
    assert _stats(report, 1).meets_target is False
    assert report.recommended_band_distance is None
    assert "precision target" in report.rationale


# ---------------------------------------------------------------------------
# Smallest-qualifying preference.
# ---------------------------------------------------------------------------


def test_prefers_smallest_qualifying_distance() -> None:
    # Both distances qualify; the smaller (more sensitive) must win.
    corpus = (
        _many(1, useful=9, neither=1, unlabelled=0)  # 0.9 over 10
        + _many(2, useful=10, neither=0, unlabelled=0)  # 1.0 over 10
    )
    report = calibrate_reconciliation_threshold(corpus)
    assert _stats(report, 1).meets_target is True
    assert _stats(report, 2).meets_target is True
    assert report.recommended_band_distance == 1
    assert "smallest" in report.rationale.lower()


def test_recommends_distance_2_when_only_it_qualifies() -> None:
    corpus = (
        _many(1, useful=5, neither=5, unlabelled=0)  # 0.5 -> fails
        + _many(2, useful=9, neither=1, unlabelled=0)  # 0.9 -> qualifies
    )
    report = calibrate_reconciliation_threshold(corpus)
    assert report.recommended_band_distance == 2


# ---------------------------------------------------------------------------
# Custom thresholds.
# ---------------------------------------------------------------------------


def test_custom_thresholds_change_recommendation() -> None:
    corpus = _many(1, useful=6, neither=4, unlabelled=0)  # 0.6 over 10
    # Default target 0.7 -> no rec.
    assert calibrate_reconciliation_threshold(corpus).recommended_band_distance is None
    # Lower the bar -> qualifies.
    relaxed = calibrate_reconciliation_threshold(corpus, target_precision=0.55, min_samples=5)
    assert relaxed.recommended_band_distance == 1


# ---------------------------------------------------------------------------
# Malformed-input hardening + honest accounting.
# ---------------------------------------------------------------------------


def test_bool_band_distance_is_skipped() -> None:
    # True is an int subclass but must NOT be treated as distance 1.
    report = calibrate_reconciliation_threshold([_outcome(True, "floor")])
    assert report.skipped_outcomes == 1
    assert _stats(report, 1).total == 0


def test_non_int_and_out_of_range_distances_skipped() -> None:
    corpus = [
        _outcome(0, "floor"),  # out of range (agreement)
        _outcome(3, "floor"),  # out of range (impossible)
        _outcome("2", "floor"),  # wrong type
        _outcome(1.0, "floor"),  # float, wrong type
        _outcome(1, "floor"),  # valid
    ]
    report = calibrate_reconciliation_threshold(corpus)
    assert report.skipped_outcomes == 4
    assert report.total_outcomes == 5  # honest: includes skipped
    assert _stats(report, 1).total == 1


def test_missing_band_distance_field_skipped() -> None:
    report = calibrate_reconciliation_threshold([{"banker_verdict": "floor"}])
    assert report.skipped_outcomes == 1
    assert report.total_outcomes == 1


def test_rationale_names_out_of_range_cause_not_unlabelled() -> None:
    # All records are out-of-range; the rationale must NOT claim "all unlabelled".
    report = calibrate_reconciliation_threshold([_outcome(0, "floor"), _outcome(9, "llm")])
    assert report.recommended_band_distance is None
    assert "out-of-range" in report.rationale or "malformed" in report.rationale
    assert "all" not in report.rationale.lower() or "unlabelled" not in report.rationale.lower()


def test_missing_verdict_field_counts_unlabelled() -> None:
    # band_distance valid, no banker_verdict key -> unlabelled, not skipped.
    report = calibrate_reconciliation_threshold([{"band_distance": 2}])
    assert report.skipped_outcomes == 0
    assert _stats(report, 2).unlabelled == 1
    assert _stats(report, 2).labelled == 0


# ---------------------------------------------------------------------------
# Model round-trip + determinism.
# ---------------------------------------------------------------------------


def test_report_model_round_trip() -> None:
    report = calibrate_reconciliation_threshold(_many(2, useful=8, neither=2, unlabelled=1))
    assert CalibrationReport(**report.model_dump()) == report


def test_byte_stable_determinism() -> None:
    corpus = _many(1, useful=7, neither=3, unlabelled=2) + _many(
        2, useful=10, neither=0, unlabelled=0
    )
    a = calibrate_reconciliation_threshold(corpus)
    b = calibrate_reconciliation_threshold(corpus)
    assert a.model_dump_json() == b.model_dump_json()


# ---------------------------------------------------------------------------
# MR5: report_to_display_rows (UI display mapping).
# ---------------------------------------------------------------------------


def test_display_rows_shape_and_order() -> None:
    from app.backend.analysis.threshold_calibration import report_to_display_rows

    report = calibrate_reconciliation_threshold(_many(2, useful=8, neither=2, unlabelled=0))
    rows = report_to_display_rows(report)
    assert [r["band_distance"] for r in rows] == ["1", "2"]
    expected_keys = {
        "band_distance",
        "total",
        "labelled",
        "useful",
        "precision",
        "meets_target",
        "recommended",
    }
    for r in rows:
        assert set(r) == expected_keys


def test_display_rows_all_string_values() -> None:
    from app.backend.analysis.threshold_calibration import report_to_display_rows

    report = calibrate_reconciliation_threshold(_many(2, useful=8, neither=2, unlabelled=1))
    rows = report_to_display_rows(report)
    for r in rows:
        assert all(isinstance(v, str) for v in r.values())


def test_display_rows_precision_percent_and_em_dash() -> None:
    from app.backend.analysis.threshold_calibration import report_to_display_rows

    # distance 2: 8/10 labelled -> 80.0%; distance 1: no labelled -> em-dash.
    report = calibrate_reconciliation_threshold(_many(2, useful=8, neither=2, unlabelled=0))
    rows = {r["band_distance"]: r for r in report_to_display_rows(report)}
    assert rows["2"]["precision"] == "80.0%"
    assert rows["1"]["precision"] == "\u2014"


def test_display_rows_flags_recommended() -> None:
    from app.backend.analysis.threshold_calibration import report_to_display_rows

    report = calibrate_reconciliation_threshold(_many(1, useful=9, neither=1, unlabelled=0))
    assert report.recommended_band_distance == 1
    rows = {r["band_distance"]: r for r in report_to_display_rows(report)}
    assert rows["1"]["recommended"] == "yes"
    assert rows["2"]["recommended"] == "no"


def test_display_rows_no_recommendation_has_no_flagged_row() -> None:
    from app.backend.analysis.threshold_calibration import report_to_display_rows

    report = calibrate_reconciliation_threshold([])
    assert report.recommended_band_distance is None
    rows = report_to_display_rows(report)
    assert all(r["recommended"] == "no" for r in rows)
