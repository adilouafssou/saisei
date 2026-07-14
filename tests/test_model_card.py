"""Verifier for the deterministic engine model card + change log (Feature 7).

The production “model” is the deterministic rule-based spine, so its model card
must be a FAITHFUL, NON-DRIFTING description of that logic and the exact
thresholds that govern it. The load-bearing invariants pinned here:

1. **Determinism.** Same constants in -> byte-identical card/log out.
2. **No drift from the code.** Every governing constant's LIVE value appears in
   the card, and the five FSA kanji labels + the cascade thresholds are rendered
   from the live ``constants`` / ``FsaClass`` (so a value change is reflected and
   a dropped constant is caught).
3. **Faithful change log.** First-issuance (no baseline), no-change, and
   changed/added/removed diffs each render the right old -> new values.

All tests are offline, deterministic, and import only from ``app.*``.
"""

from __future__ import annotations

from app.backend.export.model_card import (
    MODEL_CARD_VERSION,
    build_constants_changelog,
    build_model_card,
    governing_constants,
    model_card_filename,
)
from app.shared import constants as C
from app.shared.models.classification import FsaClass


def _fmt(value: object) -> str:
    """Mirror the module's compact int/float formatting for assertions."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def test_card_is_deterministic_byte_identical() -> None:
    """Same engine config in -> byte-identical card out (no clock/LLM/network)."""
    assert build_model_card() == build_model_card()


def test_card_states_it_is_rule_based_not_trained() -> None:
    """The card must declare there is NO trained model in the decision path."""
    card = build_model_card()
    assert "NO trained model in the decision path" in card
    assert MODEL_CARD_VERSION in card


def test_card_renders_every_governing_constant_live_value() -> None:
    """Every governing constant's live name AND value appears in the card.

    This is the anti-drift guarantee: the card is generated from
    ``governing_constants()``, so adding a constant there surfaces it here, and a
    value change is reflected verbatim.
    """
    card = build_model_card()
    for name, value in governing_constants().items():
        assert f"`{name}`" in card, f"missing constant name: {name}"
        assert _fmt(value) in card, f"missing value for {name}: {value}"


def test_card_renders_all_five_fsa_kanji_labels() -> None:
    """The cascade names all five FSA categories by their live kanji labels."""
    card = build_model_card()
    for member in FsaClass:
        assert member.kanji in card, f"missing FSA label: {member.kanji}"


def test_card_cascade_inlines_live_band_thresholds() -> None:
    """The cascade text inlines the live EWS / TDB thresholds (not hard-coded)."""
    card = build_model_card()
    assert _fmt(C.EWS_DANGER) in card
    assert _fmt(C.EWS_DOUBTFUL) in card
    assert _fmt(C.EWS_SUBSTANDARD) in card
    assert _fmt(C.TDB_NORMAL_FLOOR) in card


def test_card_ends_with_single_trailing_newline() -> None:
    """Stable diffs / archiving: exactly one trailing newline."""
    card = build_model_card()
    assert card.endswith("\n")
    assert not card.endswith("\n\n")


def test_governing_constants_match_the_module() -> None:
    """The reported constants equal the live module values (single source)."""
    gc = governing_constants()
    assert gc["EWS_SUBSTANDARD"] == C.EWS_SUBSTANDARD
    assert gc["HOSHO_ELIGIBLE_SCORE"] == C.HOSHO_ELIGIBLE_SCORE
    assert gc["RECONCILIATION_BAND_DISTANCE"] == C.RECONCILIATION_BAND_DISTANCE
    # The Hosho pillar weights must still sum to 100 (engine invariant).
    assert gc["HOSHO_WEIGHT_BUNRI"] + gc["HOSHO_WEIGHT_ZAIMU"] + gc["HOSHO_WEIGHT_KAIJI"] == 100.0


# --- Change log -----------------------------------------------------------


def test_changelog_first_issuance_logs_current_values() -> None:
    """No baseline -> first-issuance log listing the current values."""
    log = build_constants_changelog(previous=None)
    assert "first issuance" in log.lower()
    # A representative current value is present.
    assert _fmt(C.EWS_SUBSTANDARD) in log
    assert build_constants_changelog(previous={}) == log  # empty == None


def test_changelog_no_changes_when_identical() -> None:
    """Identical baseline -> an explicit 'no changes' log (no diff tables)."""
    baseline = governing_constants()
    log = build_constants_changelog(current=baseline, previous=baseline)
    assert "No changes" in log
    assert "Changed" not in log
    assert "Added" not in log
    assert "Removed" not in log


def test_changelog_reports_changed_value_old_to_new() -> None:
    """A changed threshold is rendered as old -> new in the Changed table."""
    baseline = dict(governing_constants())
    baseline["EWS_SUBSTANDARD"] = 35.0  # pretend the floor was previously 35
    log = build_constants_changelog(previous=baseline)
    assert "Changed" in log
    assert "`EWS_SUBSTANDARD`" in log
    # Old (35) and new (live 40) both present in the changed row.
    assert "35" in log
    assert _fmt(C.EWS_SUBSTANDARD) in log


def test_changelog_reports_added_and_removed() -> None:
    """Constants present in only one side appear under Added / Removed."""
    baseline = dict(governing_constants())
    # Simulate: baseline had an extra (now-removed) constant; current has a new
    # one the baseline lacked.
    baseline["OLD_RETIRED_THRESHOLD"] = 99
    current = dict(governing_constants())
    current["NEW_THRESHOLD"] = 7
    log = build_constants_changelog(current=current, previous=baseline)
    assert "Added" in log
    assert "`NEW_THRESHOLD`" in log
    assert "Removed" in log
    assert "`OLD_RETIRED_THRESHOLD`" in log


def test_changelog_is_deterministic() -> None:
    """Same inputs -> byte-identical change log."""
    baseline = dict(governing_constants())
    baseline["EWS_DANGER"] = 80.0
    assert build_constants_changelog(previous=baseline) == build_constants_changelog(
        previous=baseline
    )


def test_model_card_filename_is_safe() -> None:
    """The filename mirrors the shared cross-platform download contract."""
    assert model_card_filename() == "model_card_saisei_engine.md"
    assert model_card_filename("bad:name?") == "model_card_bad_name.md"
    assert model_card_filename("") == "model_card_engine.md"
