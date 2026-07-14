"""MR4 — `make calibrate` CLI tests.

Covers the pure surface offline (no Postgres touched):
- collect_outcomes dedup / skip / order,
- load_outcomes_from_json shapes + errors,
- format_report,
- main() table output, --json, missing-file exit code, custom thresholds.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from app.backend.analysis.calibrate_cli import (
    collect_outcomes,
    format_report,
    load_outcomes_from_json,
    main,
)
from app.backend.analysis.threshold_calibration import (
    calibrate_reconciliation_threshold,
)


def _outcome(band_distance: int, verdict: str, title: str = "s") -> dict[str, Any]:
    return {
        "strategy_title": title,
        "deterministic_band": "high",
        "llm_band": "low",
        "band_distance": band_distance,
        "banker_decision": "approve",
        "banker_verdict": verdict,
    }


# ---------------------------------------------------------------------------
# collect_outcomes: dedup / skip / order.
# ---------------------------------------------------------------------------


def test_collect_keeps_longest_snapshot_per_run() -> None:
    run = [_outcome(2, "floor", "a")]
    grown = [_outcome(2, "floor", "a"), _outcome(1, "llm", "b")]
    # Two snapshots of the SAME run (append-only) must not double-count.
    corpus = collect_outcomes(
        [{"reconciliation_outcomes": run}, {"reconciliation_outcomes": grown}]
    )
    assert corpus == grown


def test_collect_concatenates_distinct_runs() -> None:
    run_a = [_outcome(2, "floor", "a")]
    run_b = [_outcome(1, "llm", "b")]
    corpus = collect_outcomes(
        [{"reconciliation_outcomes": run_a}, {"reconciliation_outcomes": run_b}]
    )
    assert len(corpus) == 2
    assert {o["strategy_title"] for o in corpus} == {"a", "b"}


def test_collect_skips_missing_empty_and_non_dict() -> None:
    corpus = collect_outcomes(
        [
            {},  # no key
            {"reconciliation_outcomes": []},  # empty
            {"reconciliation_outcomes": "nope"},  # wrong type
            "not-a-dict",  # type: ignore[list-item]
            {"reconciliation_outcomes": [_outcome(1, "floor")]},
        ]
    )
    assert len(corpus) == 1


def test_collect_filters_non_dict_outcomes() -> None:
    corpus = collect_outcomes([{"reconciliation_outcomes": [_outcome(1, "floor"), "junk", 42]}])
    assert corpus == [_outcome(1, "floor")]


def test_collect_is_deterministic_in_order() -> None:
    a = collect_outcomes(
        [
            {"reconciliation_outcomes": [_outcome(1, "floor", "a")]},
            {"reconciliation_outcomes": [_outcome(2, "llm", "b")]},
        ]
    )
    b = collect_outcomes(
        [
            {"reconciliation_outcomes": [_outcome(2, "llm", "b")]},
            {"reconciliation_outcomes": [_outcome(1, "floor", "a")]},
        ]
    )
    assert a == b  # sorted by run key, order-independent


def test_collect_tolerates_non_serialisable_values() -> None:
    # A datetime in the first outcome must not raise in _run_key.
    outcome = _outcome(1, "floor")
    outcome["captured_at"] = dt.datetime(2026, 1, 1, 12, 0, 0)
    corpus = collect_outcomes([{"reconciliation_outcomes": [outcome]}])
    assert len(corpus) == 1


# ---------------------------------------------------------------------------
# load_outcomes_from_json: shapes + errors.
# ---------------------------------------------------------------------------


def test_load_json_array(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text(json.dumps([_outcome(1, "floor")]), encoding="utf-8")
    assert load_outcomes_from_json(p) == [_outcome(1, "floor")]


def test_load_json_state_object(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"reconciliation_outcomes": [_outcome(2, "llm")]}), encoding="utf-8")
    assert load_outcomes_from_json(p) == [_outcome(2, "llm")]


def test_load_json_filters_non_dicts(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps([_outcome(1, "floor"), "junk", 7]), encoding="utf-8")
    assert load_outcomes_from_json(p) == [_outcome(1, "floor")]


def test_load_json_bad_shape_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps("a string"), encoding="utf-8")
    with pytest.raises(ValueError, match="array of outcomes"):
        load_outcomes_from_json(p)


def test_load_json_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_outcomes_from_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# format_report.
# ---------------------------------------------------------------------------


def test_format_report_contains_key_lines() -> None:
    report = calibrate_reconciliation_threshold([_outcome(2, "floor") for _ in range(10)])
    text = format_report(report)
    assert "calibration report" in text
    assert "recommendation:" in text
    assert "rationale:" in text
    # The em-dash appears for a distance with no labelled outcomes (distance 1).
    assert "\u2014" in text


# ---------------------------------------------------------------------------
# main(): table, --json, exit codes, thresholds.
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, outcomes: list[dict[str, Any]]) -> Path:
    p = tmp_path / "o.json"
    p.write_text(json.dumps(outcomes), encoding="utf-8")
    return p


def test_main_table_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write(tmp_path, [_outcome(2, "floor") for _ in range(10)])
    code = main(["--json-file", str(p)])
    assert code == 0
    out = capsys.readouterr().out
    assert "calibration report" in out
    assert "recommendation:" in out


def test_main_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write(tmp_path, [_outcome(2, "floor") for _ in range(10)])
    code = main(["--json-file", str(p), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_outcomes"] == 10
    assert "per_distance" in payload


def test_main_missing_file_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json-file", str(tmp_path / "nope.json")])
    assert code == 2
    assert "file not found" in capsys.readouterr().err


def test_main_bad_shape_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps("x"), encoding="utf-8")
    code = main(["--json-file", str(p)])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_main_custom_thresholds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 6/10 floor -> precision 0.6: fails default 0.7, passes target 0.55.
    outcomes = [_outcome(1, "floor") for _ in range(6)] + [_outcome(1, "neither") for _ in range(4)]
    p = _write(tmp_path, outcomes)
    code = main(
        ["--json-file", str(p), "--json", "--target-precision", "0.55", "--min-samples", "5"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["recommended_band_distance"] == 1
