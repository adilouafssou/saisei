"""Versioned golden dataset for the Saisei classification eval (Feature 1).

Single in-repo source of truth mapping each deterministic borrower fixture to
its EXPECTED FSA classification spine: the FSA debtor category and the 要管理先
(special_attention) sub-tier flag. The merge-blocking accuracy gate
(``tests/eval/test_eval_gate.py``) runs every case through the same pure rule
functions the graph nodes use and asserts 100% classification accuracy and
special-attention recall, so a prompt/model/rule change that regresses the
classification spine fails CI.

This is the local, version-controlled form of the "golden dataset" in
``docs/en/NEXT_STEPS.md`` Feature 1. Pushing/versioning the same cases in
LangSmith is the network-dependent follow-up; the cases live here so the gate
runs fully offline.

The expected values are the ones already asserted by the golden-spine harness
(``tests/eval/test_golden_spine.py``); keeping them in one declarative table
lets the gate iterate every case uniformly and makes adding a new labelled case
a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.shared.models.classification import FsaClass

__all__ = ["GoldenCase", "GOLDEN_DATASET"]


@dataclass(frozen=True)
class GoldenCase:
    """One labelled classification-eval case.

    Attributes:
        tdb_code: 7-digit TDB Kigyo code identifying the deterministic fixture.
        label: Human-readable fixture label (for test ids / failure messages).
        expected_fsa: The expected FSA debtor classification.
        expected_special_attention: Expected 要管理先 sub-tier flag.
    """

    tdb_code: str
    label: str
    expected_fsa: FsaClass
    expected_special_attention: bool


#: The versioned golden dataset. Each entry is a deterministic fixture with a
#: human-verified expected classification. Add a new labelled case by appending
#: one GoldenCase; the accuracy gate picks it up automatically.
GOLDEN_DATASET: tuple[GoldenCase, ...] = (
    GoldenCase(
        tdb_code="2000001",
        label="normal_service_co",
        expected_fsa=FsaClass.SEIJOSAKI,
        expected_special_attention=False,
    ),
    GoldenCase(
        tdb_code="3000001",
        label="needs_attention_mfg",
        expected_fsa=FsaClass.YOCHUISAKI,
        expected_special_attention=True,
    ),
    GoldenCase(
        tdb_code="4000001",
        label="osaka_distressed_mfg",
        expected_fsa=FsaClass.HATAN_KENENSAKI,
        expected_special_attention=False,
    ),
    GoldenCase(
        tdb_code="5000001",
        label="kyoto_wc_deficit_co",
        expected_fsa=FsaClass.YOCHUISAKI,
        expected_special_attention=True,
    ),
    GoldenCase(
        tdb_code="1234567",
        label="aichi_manufacturer",
        # Through the pure deterministic spine (EWS + working-capital gap + TDB
        # score) WITHOUT an explicit insolvency override, the Aichi fixture's
        # financials land in 破綻懸念先 (In Danger of Bankruptcy). It only reaches
        # 実質破綻先 (JISSHITSU_HATANSAKI) when a banker supplies net_worth < 0 /
        # is_insolvent=True (see test_golden_spine's insolvent fixture, which
        # passes that override). The eval gate exercises the financial spine, so
        # the expected band here is the no-override outcome.
        expected_fsa=FsaClass.HATAN_KENENSAKI,
        expected_special_attention=False,
    ),
)
