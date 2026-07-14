"""Governance gate: the live decision thresholds must match the reviewed baseline.

This is the load-bearing compliance control for the deterministic engine. A
regulated decision threshold (an EWS band, the guarantee-release eligibility
floor, the FSA cascade cut-offs, ...) changing SILENTLY is the most dangerous
failure mode for this product: it would alter credit decisions with no review and
no record.

This test makes that structurally impossible. The governing constants are
committed as a reviewed baseline
(``app/backend/export/governing_constants_baseline.json``); the live constants
must equal it. Changing a threshold therefore REQUIRES updating the baseline in
the SAME merge request — where the diff is visible, reviewed, and recorded (the
change log renders the exact old → new). The guard runs inside ``make verify``
(pytest), so no separate CI wiring is needed.

When this test fails, it is doing its job: a decision threshold changed. The fix
is NOT to weaken the test — it is to (1) confirm the change is intended and
reviewed, then (2) regenerate the baseline to match the new constants and commit
it alongside the change, so the change is on the record.

All offline, deterministic; imports only from ``app.*``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.backend.export.model_card import (
    CONSTANTS_BASELINE_PATH,
    build_constants_changelog,
    detect_constants_drift,
    governing_constants,
    load_constants_baseline,
)


def _format_drift(drift: dict[str, object]) -> str:
    """Build an actionable failure message describing the detected drift."""
    parts: list[str] = []
    changed = drift.get("changed") or {}
    assert isinstance(changed, dict)
    for name, ov in changed.items():
        parts.append(f"  CHANGED {name}: {ov['old']} -> {ov['new']}")
    added = drift.get("added") or []
    assert isinstance(added, list)
    for name in added:
        parts.append(f"  ADDED   {name}")
    removed = drift.get("removed") or []
    assert isinstance(removed, list)
    for name in removed:
        parts.append(f"  REMOVED {name}")
    body = "\n".join(parts)
    return (
        "Governing decision thresholds drifted from the reviewed baseline:\n"
        f"{body}\n\n"
        "If this change is intended and reviewed, regenerate the baseline\n"
        f"({CONSTANTS_BASELINE_PATH}) to match the new constants and commit it\n"
        "in the SAME merge request so the change is on the record."
    )


def test_baseline_file_exists_and_is_valid_json() -> None:
    """The committed baseline must exist and be a valid JSON object."""
    assert CONSTANTS_BASELINE_PATH.exists(), (
        f"Missing governing-constants baseline at {CONSTANTS_BASELINE_PATH}"
    )
    baseline = load_constants_baseline()
    assert isinstance(baseline, dict) and baseline


def test_live_constants_match_the_reviewed_baseline() -> None:
    """THE GATE: live decision thresholds must equal the reviewed baseline.

    A failure here means a regulated threshold changed. Do not weaken this test;
    regenerate and commit the baseline alongside the intended change.
    """
    drift = detect_constants_drift()
    assert not drift["drifted"], _format_drift(drift)


def test_baseline_covers_exactly_the_governing_constants() -> None:
    """The baseline's key set is exactly the live governing-constants key set.

    Guards both directions: a new constant added to the engine without a baseline
    entry, and a stale baseline key for a constant that was removed.
    """
    assert set(load_constants_baseline()) == set(governing_constants())


def test_changelog_against_baseline_reports_no_changes_when_in_sync() -> None:
    """With the engine in sync, the change log against the baseline is clean."""
    log = build_constants_changelog(previous=load_constants_baseline())
    assert "No changes" in log


def test_drift_detector_flags_a_changed_threshold() -> None:
    """Sanity: the detector actually catches a changed value (old -> new)."""
    baseline = dict(governing_constants())
    baseline["EWS_SUBSTANDARD"] = baseline["EWS_SUBSTANDARD"] + 1  # pretend baseline differed
    drift = detect_constants_drift(baseline=baseline)
    assert drift["drifted"] is True
    assert "EWS_SUBSTANDARD" in drift["changed"]
    assert drift["changed"]["EWS_SUBSTANDARD"]["new"] == governing_constants()["EWS_SUBSTANDARD"]


def test_drift_detector_flags_added_and_removed() -> None:
    """The detector reports keys present on only one side."""
    baseline = dict(governing_constants())
    baseline.pop("EWS_DANGER")  # current has it, baseline doesn't -> added
    baseline["RETIRED_THRESHOLD"] = 1  # baseline has it, current doesn't -> removed
    drift = detect_constants_drift(baseline=baseline)
    assert "EWS_DANGER" in drift["added"]
    assert "RETIRED_THRESHOLD" in drift["removed"]
    assert drift["drifted"] is True


def test_corrupt_baseline_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt baseline file raises (never silently disables the guard)."""
    import app.backend.export.model_card as mc

    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(mc, "CONSTANTS_BASELINE_PATH", bad)
    with pytest.raises(ValueError):
        mc.load_constants_baseline()


def test_baseline_json_round_trips_to_the_live_values() -> None:
    """The on-disk JSON parses to values equal to the live constants (typed)."""
    on_disk = json.loads(CONSTANTS_BASELINE_PATH.read_text(encoding="utf-8"))
    assert on_disk == governing_constants()
