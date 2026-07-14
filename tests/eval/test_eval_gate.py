"""Merge-blocking classification accuracy gate (Feature 1, LangSmith eval).

CI-gated, deterministic, offline. Runs every case in the versioned golden
dataset (``tests/eval/golden_dataset.py``) through the SAME pure rule functions
the graph nodes use -- ``compute_ews_score`` / ``estimate_working_capital_gap``
/ ``classify`` over the ``MockDataProvider`` -- and asserts:

* 100% FSA classification accuracy, and
* 100% special_attention (要管理先) recall.

This is the regression gate Feature 1 calls for: a prompt, model, or rule change
that moves any borrower across an FSA band (or drops the 要管理先 sub-tier) fails
the build. It runs under the existing named eval step (``pytest tests/eval``) in
``.github/workflows/ci.yml``, so it is automatically merge-blocking with no CI
config change.

The gate asserts CLASSES, not exact yen, so it stays robust to harmless numeric
drift while still catching any band regression -- mirroring the class-only
rationale documented in ``test_golden_spine.py``.
"""

from __future__ import annotations

import pytest
from app.backend.nodes.ews_scoring import classify, compute_ews_score
from app.backend.nodes.financial_extraction import estimate_working_capital_gap
from app.backend.tools.provider import MockDataProvider
from app.shared.models.classification import FsaClass

from tests.eval.golden_dataset import GOLDEN_DATASET, GoldenCase


def _classify(tdb_code: str) -> tuple[FsaClass, bool]:
    """Run the deterministic classification spine for one fixture.

    Composes the same pure rule functions the graph nodes use, with no graph
    execution and no HITL, returning ``(FsaClass, special_attention)``.
    """
    provider = MockDataProvider()
    report = provider.credit_report(tdb_code)
    shisanhyo = provider.shisanhyo(report.profile.hojin_bango)
    rate_curve = provider.rate_curve()
    metrics = provider.settlement_metrics()

    ews = compute_ews_score(shisanhyo)
    latest = shisanhyo[-1]
    gap = estimate_working_capital_gap(
        monthly_sales=int(latest.uriage),
        monthly_cogs=int(latest.uriage_genka),
        metrics=metrics,
        rate_curve=rate_curve,
        monthly_operating_profit=latest.eigyo_rieki,
    )
    return classify(ews_score=ews, working_capital_gap=gap, tdb_score=report.tdb_score)


@pytest.mark.parametrize("case", GOLDEN_DATASET, ids=lambda c: c.label)
def test_golden_case_classification(case: GoldenCase) -> None:
    """Each golden case must classify to its expected FSA band + sub-tier."""
    fsa, special = _classify(case.tdb_code)
    assert fsa is case.expected_fsa, (
        f"{case.label} ({case.tdb_code}): expected {case.expected_fsa.name}, got {fsa.name}"
    )
    assert special is case.expected_special_attention, (
        f"{case.label} ({case.tdb_code}): expected special_attention="
        f"{case.expected_special_attention}, got {special}"
    )


def test_classification_accuracy_is_perfect() -> None:
    """Aggregate gate: FSA accuracy and special-attention recall must be 100%.

    A single aggregate assertion (in addition to the per-case parametrization)
    so a regression is reported as a clear accuracy/recall number, which is the
    metric Feature 1 specifies the gate should block on.
    """
    total = len(GOLDEN_DATASET)
    fsa_correct = 0
    special_positives = 0
    special_recalled = 0

    for case in GOLDEN_DATASET:
        fsa, special = _classify(case.tdb_code)
        if fsa is case.expected_fsa:
            fsa_correct += 1
        if case.expected_special_attention:
            special_positives += 1
            if special is True:
                special_recalled += 1

    accuracy = fsa_correct / total
    recall = special_recalled / special_positives if special_positives else 1.0

    assert accuracy == 1.0, f"FSA classification accuracy regressed: {accuracy:.2%}"
    assert recall == 1.0, f"special_attention recall regressed: {recall:.2%}"


def test_classification_is_deterministic() -> None:
    """Every golden case must classify identically across repeated runs."""
    for case in GOLDEN_DATASET:
        assert _classify(case.tdb_code) == _classify(case.tdb_code)
