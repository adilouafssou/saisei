"""Offline tests for the deterministic bias / fairness review (Feature 7).

The fairness review is an advisory, after-the-fact analysis of classification
outcomes: does the adverse-outcome rate vary systematically by industry or
region beyond a tolerance, on enough borrowers to matter? These tests pin the
pure analysis contract and the report rendering. Fully offline (stdlib +
pydantic + the analysis/export modules); no network, no DB.
"""

from __future__ import annotations

from app.backend.analysis.fairness_review import (
    ClassificationOutcome,
    FairnessAxis,
    analyse_fairness,
    fairness_outcomes_from_audit,
)
from app.backend.export.fairness_report import (
    build_fairness_report,
    fairness_report_filename,
)


def _outcome(industry: str, region: str, fsa: str, special: bool = False) -> ClassificationOutcome:
    return ClassificationOutcome(
        industry=industry, region=region, fsa_class=fsa, special_attention=special
    )


def _book() -> list[ClassificationOutcome]:
    """A book where one industry (製造業) is far more distressed than the rest."""
    book: list[ClassificationOutcome] = []
    # 製造業: 5 borrowers, 5 distressed (rate 100%).
    for _ in range(5):
        book.append(_outcome("製造業", "愛知県", "hatan_kenensaki"))
    # サービス業: 6 borrowers, 0 distressed (rate 0%).
    for _ in range(6):
        book.append(_outcome("サービス業", "東京都", "seijosaki"))
    return book


# ---------------------------------------------------------------------------
# is_distressed contract
# ---------------------------------------------------------------------------


class TestDistressedContract:
    def test_seijosaki_is_not_distressed(self) -> None:
        assert not _outcome("x", "y", "seijosaki").is_distressed()

    def test_yochuisaki_is_distressed(self) -> None:
        assert _outcome("x", "y", "yochuisaki").is_distressed()

    def test_special_attention_seijosaki_is_distressed(self) -> None:
        # The 要管理先 flag makes an outcome adverse even if the base string is
        # not in the distressed set.
        assert _outcome("x", "y", "seijosaki", special=True).is_distressed()


# ---------------------------------------------------------------------------
# analyse_fairness
# ---------------------------------------------------------------------------


class TestAnalyseFairness:
    def test_overall_rate_and_classified_count(self) -> None:
        report = analyse_fairness(_book(), FairnessAxis.INDUSTRY)
        assert report.classified_outcomes == 11
        # 5 distressed of 11 = 0.4545.
        assert report.overall_adverse_rate == 0.4545

    def test_disparate_industry_is_flagged(self) -> None:
        report = analyse_fairness(_book(), FairnessAxis.INDUSTRY)
        assert "製造業" in report.flagged_groups
        # サービス業 deviates downward by the same magnitude; with 6 >= min size
        # and |disparity| >= 0.20 it is also flagged (deviation is signed both ways).
        assert "サービス業" in report.flagged_groups

    def test_worst_group_sorts_first(self) -> None:
        report = analyse_fairness(_book(), FairnessAxis.INDUSTRY)
        assert report.per_group[0].group == "製造業"
        assert report.per_group[0].adverse_rate == 1.0
        assert report.per_group[0].disparity > 0

    def test_small_group_is_not_flagged(self) -> None:
        # A tiny but 100%-distressed group must NOT be flagged (below min size).
        book = [
            _outcome("零細製造", "x", "hatansaki"),  # 1 borrower, 100% distressed
            *[_outcome("大規模", "x", "seijosaki") for _ in range(10)],
        ]
        report = analyse_fairness(book, FairnessAxis.INDUSTRY, min_group_size=5)
        assert "零細製造" not in report.flagged_groups

    def test_region_axis_groups_by_prefecture(self) -> None:
        report = analyse_fairness(_book(), FairnessAxis.REGION)
        groups = {s.group for s in report.per_group}
        assert groups == {"愛知県", "東京都"}

    def test_empty_group_values_excluded_from_denominator(self) -> None:
        book = [
            _outcome("", "x", "hatansaki"),  # no industry -> excluded from analysis
            *[_outcome("製造業", "x", "seijosaki") for _ in range(5)],
        ]
        report = analyse_fairness(book, FairnessAxis.INDUSTRY)
        assert report.total_outcomes == 6
        assert report.classified_outcomes == 5

    def test_accepts_dicts(self) -> None:
        report = analyse_fairness(
            [{"industry": "製造業", "region": "愛知県", "fsa_class": "yochuisaki"}],
            FairnessAxis.INDUSTRY,
        )
        assert report.classified_outcomes == 1
        assert report.overall_adverse_rate == 1.0

    def test_no_disparity_when_uniform(self) -> None:
        book = [_outcome(f"ind{i % 3}", "x", "seijosaki") for i in range(15)]
        report = analyse_fairness(book, FairnessAxis.INDUSTRY)
        assert report.flagged_groups == []
        assert "No" in report.rationale or "no" in report.rationale

    def test_is_deterministic(self) -> None:
        a = analyse_fairness(_book(), FairnessAxis.INDUSTRY)
        b = analyse_fairness(_book(), FairnessAxis.INDUSTRY)
        assert a == b

    def test_empty_corpus_is_safe(self) -> None:
        report = analyse_fairness([], FairnessAxis.INDUSTRY)
        assert report.classified_outcomes == 0
        assert report.flagged_groups == []
        assert report.per_group == []


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


class TestFairnessReportRender:
    def test_markdown_contains_axis_and_groups(self) -> None:
        md = build_fairness_report(analyse_fairness(_book(), FairnessAxis.INDUSTRY))
        assert "# 公平性レビュー (Bias / fairness review)" in md
        assert "製造業" in md
        assert "advisory" in md.lower()

    def test_markdown_marks_flagged_groups(self) -> None:
        md = build_fairness_report(analyse_fairness(_book(), FairnessAxis.INDUSTRY))
        # The flag glyph appears for the disparate group.
        assert "⚠" in md

    def test_render_is_deterministic(self) -> None:
        report = analyse_fairness(_book(), FairnessAxis.INDUSTRY)
        assert build_fairness_report(report) == build_fairness_report(report)

    def test_filename_is_axis_scoped(self) -> None:
        assert fairness_report_filename(FairnessAxis.INDUSTRY) == "fairness_industry.md"
        assert fairness_report_filename(FairnessAxis.REGION) == "fairness_region.md"


# ---------------------------------------------------------------------------
# Audit-ledger adapter
# ---------------------------------------------------------------------------


class TestFromAudit:
    def _event(self, **payload: object) -> dict[str, object]:
        return {"event_type": "classification", "payload": payload}

    def test_maps_classification_events(self) -> None:
        events = [
            self._event(
                industry="製造業",
                prefecture="愛知県",
                fsa_classification="yochuisaki",
                special_attention=True,
            )
        ]
        outcomes = fairness_outcomes_from_audit(events)
        assert len(outcomes) == 1
        assert outcomes[0].industry == "製造業"
        assert outcomes[0].region == "愛知県"
        assert outcomes[0].is_distressed()

    def test_skips_non_classification_events(self) -> None:
        events = [
            {"event_type": "human_decision", "payload": {"decision": "approve"}},
            self._event(industry="x", prefecture="y", fsa_classification="seijosaki"),
        ]
        outcomes = fairness_outcomes_from_audit(events)
        assert len(outcomes) == 1
        assert not outcomes[0].is_distressed()

    def test_missing_keys_degrade_safely(self) -> None:
        outcomes = fairness_outcomes_from_audit([{"event_type": "classification"}])
        assert len(outcomes) == 1
        assert outcomes[0].industry == ""
        assert outcomes[0].fsa_class == ""

    def test_end_to_end_from_audit_to_report(self) -> None:
        # The adapter output flows straight into analyse_fairness.
        events = [
            self._event(industry="製造業", prefecture="愛知県", fsa_classification="hatansaki")
            for _ in range(5)
        ] + [
            self._event(industry="サービス業", prefecture="東京都", fsa_classification="seijosaki")
            for _ in range(6)
        ]
        report = analyse_fairness(fairness_outcomes_from_audit(events), FairnessAxis.INDUSTRY)
        assert "製造業" in report.flagged_groups
