"""Tests for FSA classification logic."""

from __future__ import annotations

from app.backend.nodes.ews_scoring import classify
from app.shared.models.classification import FsaClass


def test_fsa_class_has_exactly_three_members() -> None:
    assert set(FsaClass) == {FsaClass.JOYO, FsaClass.YOI_KANRI, FsaClass.YUKYO_GUCHI}


def test_joyo_does_not_require_turnaround() -> None:
    assert FsaClass.JOYO.requires_turnaround is False
    assert FsaClass.YOI_KANRI.requires_turnaround is True
    assert FsaClass.YUKYO_GUCHI.requires_turnaround is True


def test_classify_normal() -> None:
    assert classify(ews_score=10.0, working_capital_gap=5_000_000, tdb_score=80) == FsaClass.JOYO


def test_classify_substandard_on_deficit() -> None:
    assert (
        classify(ews_score=20.0, working_capital_gap=-1_000_000, tdb_score=80)
        == FsaClass.YOI_KANRI
    )


def test_classify_substandard_on_low_tdb() -> None:
    assert (
        classify(ews_score=10.0, working_capital_gap=1_000_000, tdb_score=50)
        == FsaClass.YOI_KANRI
    )


def test_classify_doubtful_on_high_ews() -> None:
    assert classify(ews_score=75.0, working_capital_gap=0, tdb_score=80) == FsaClass.YUKYO_GUCHI


def test_classify_doubtful_on_deficit_and_mid_ews() -> None:
    assert (
        classify(ews_score=45.0, working_capital_gap=-1, tdb_score=80) == FsaClass.YUKYO_GUCHI
    )
