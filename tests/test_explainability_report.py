"""Verifier for the deterministic explainability report (Feature 7).

The interactive UI already shows *why* a borrower landed in its FSA band; this
report assembles those SAME already-computed deterministic figures into one
examiner-facing Markdown artifact. The load-bearing invariants this pins are:

1. **Determinism.** Same state in -> byte-identical report out (no LLM, no
   network, no clock). An archived classification basis must be reproducible.
2. **Numeric / verbatim preservation.** Every figure (EWS score, per-signal
   points / weights, Hosho pillar scores) and the classification reason appear
   exactly as the spine produced them — the report formats, never re-derives.
3. **Rehydration safety.** It renders identically off a live ``SaiseiState`` and
   off a checkpointer-rehydrated ``dict`` (the breakdown is stored as dicts on
   real state), so an examiner reading a snapshot sees the same story.

All tests are offline, deterministic, and import only from ``app.*``.
"""

from __future__ import annotations

from app.backend.export.explainability_report import (
    build_explainability_report,
    explainability_filename,
)
from app.backend.nodes.ews_scoring import compute_ews_breakdown, compute_ews_score
from app.backend.nodes.keieisha_hosho import assess_hosho_kaijo
from app.backend.state import SaiseiState
from app.backend.tools.tdb_api import CompanyProfile
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass


def _profile() -> CompanyProfile:
    return CompanyProfile(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        name="\u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e",  # テスト製造株式会社
        prefecture="\u611b\u77e5\u770c",
        industry="\u88fd\u9020\u696d",
        established_year=1990,
        employees=42,
    )


def _declining_history() -> list[TrialBalance]:
    import datetime as dt

    rows: list[TrialBalance] = []
    for i in range(6):
        sales = 100_000_000 - i * 8_000_000
        cogs = int(sales * (0.72 + i * 0.01))
        rows.append(
            TrialBalance(
                period=dt.date(2025, 1, 1) + dt.timedelta(days=30 * i),
                uriage=sales,
                uriage_genka=cogs,
                hanbaihi=20_000_000,
            )
        )
    return rows


def _yochuisaki_state() -> SaiseiState:
    """A fully-assessed 要注意先 borrower (with EWS breakdown + Hosho pillars)."""
    history = _declining_history()
    conditions = assess_hosho_kaijo(
        shisanhyo_count=len(history),
        avg_eigai_shueki=0.0,
        avg_eigai_hiyo=0.0,
        avg_uriage=90_000_000.0,
        ews_score=compute_ews_score(history),
        working_capital_gap=-5_000_000,
        tdb_score=58,
        error_count=0,
    )
    return SaiseiState(
        tdb_code="1234567",
        company_profile=_profile(),
        tdb_score=58,
        shisanhyo=history,
        working_capital_gap=-5_000_000,
        ews_score=compute_ews_score(history),
        ews_breakdown=[s.__dict__ for s in compute_ews_breakdown(history)],
        fsa_classification=FsaClass.YOCHUISAKI,
        special_attention=True,
        classification_reason=(
            "\u8cc7\u91d1\u7e70\u308a\u4e0d\u8db3 "
            "(working-capital deficit \u2192 \u8981\u7ba1\u7406\u5148)"
        ),
        hosho_kaijo_score=round(
            conditions.bunri_score + conditions.zaimu_score + conditions.kaiji_score, 2
        ),
        hosho_kaijo_conditions=conditions,
    )


def test_report_is_deterministic_byte_identical() -> None:
    """Same state in -> byte-identical Markdown out (no LLM, clock, or network)."""
    state = _yochuisaki_state()
    assert build_explainability_report(state) == build_explainability_report(state)


def test_report_renders_company_and_classification() -> None:
    """The borrower name, FSA class kanji, and threshold reason all appear."""
    report = build_explainability_report(_yochuisaki_state())
    assert "\u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e" in report  # company name
    assert FsaClass.YOCHUISAKI.kanji in report  # 要注意先
    assert "\u8981\u7ba1\u7406\u5148" in report  # special-attention marker (要管理先)
    assert "working-capital deficit" in report  # the classification reason verbatim


def test_report_renders_ews_score_and_signal_points_verbatim() -> None:
    """The EWS score and every per-signal point/weight render exactly as computed."""
    history = _declining_history()
    state = _yochuisaki_state()
    report = build_explainability_report(state)

    score = compute_ews_score(history)
    # The score appears (whole numbers render without a trailing .0).
    score_txt = str(int(score)) if score == int(score) else f"{score:.2f}"
    assert score_txt in report

    breakdown = compute_ews_breakdown(history)
    assert breakdown  # guard: the fixture actually has a breakdown
    for sig in breakdown:
        assert sig.label_ja in report
        pts = str(int(sig.points)) if sig.points == int(sig.points) else f"{sig.points:.2f}"
        assert pts in report


def test_report_renders_hosho_pillars_and_directives() -> None:
    """The guarantee-release section carries each pillar and its directives."""
    state = _yochuisaki_state()
    report = build_explainability_report(state)
    conditions = state.hosho_kaijo_conditions
    assert conditions is not None
    assert "\u4fdd\u8a3c\u89e3\u9664" in report or "Guarantee-release" in report
    # Pillar met/unmet glyphs are present.
    assert "\u2713" in report or "\u2717" in report
    # The ordered directives (what must change) are surfaced.
    for directive in conditions.ordered_directives:
        assert directive in report


def test_report_without_hosho_omits_that_section() -> None:
    """No Hosho conditions -> the guarantee-release section is absent (no crash)."""
    history = _declining_history()
    state = SaiseiState(
        tdb_code="1234567",
        company_profile=_profile(),
        shisanhyo=history,
        ews_score=compute_ews_score(history),
        ews_breakdown=[s.__dict__ for s in compute_ews_breakdown(history)],
        fsa_classification=FsaClass.SEIJOSAKI,
        classification_reason="all thresholds clear",
    )
    report = build_explainability_report(state)
    assert "Guarantee-release" not in report
    assert "\u4fdd\u8a3c\u89e3\u9664\u306e\u6839\u62e0" not in report
    # The core sections still render.
    assert FsaClass.SEIJOSAKI.kanji in report


def test_report_with_empty_breakdown_states_insufficient_history() -> None:
    """< 2 months -> empty breakdown -> explicit 'no breakdown' note, no table."""
    state = SaiseiState(
        tdb_code="1234567",
        company_profile=_profile(),
        ews_score=0.0,
        ews_breakdown=[],
        fsa_classification=FsaClass.SEIJOSAKI,
        classification_reason="all thresholds clear",
    )
    report = build_explainability_report(state)
    assert (
        "insufficient history" in report.lower()
        or "\u5185\u8a33\u306f\u3042\u308a\u307e\u305b\u3093" in report
    )
    # No signal table header row when there is no breakdown.
    assert "| --- | ---: | ---: | ---: |" not in report


def test_report_renders_off_a_rehydrated_dict() -> None:
    """A checkpointer-style dict renders identically to the live state object.

    Uses the ROMANIZED StrEnum value for ``fsa_classification`` (not the enum)
    because that is the real rehydration risk: a snapshot can carry the plain
    string, and the report must still resolve it to the kanji rather than print
    the romanized id.
    """
    state = _yochuisaki_state()
    from_obj = build_explainability_report(state)

    assert state.company_profile is not None
    assert state.fsa_classification is not None
    assert state.hosho_kaijo_conditions is not None
    rehydrated = {
        "tdb_code": state.tdb_code,
        "company_profile": {"name": state.company_profile.name},
        # Plain romanized string (e.g. "yochuisaki"), as a snapshot may carry it.
        "fsa_classification": state.fsa_classification.value,
        "classification_reason": state.classification_reason,
        "special_attention": state.special_attention,
        "ews_score": state.ews_score,
        "ews_breakdown": state.ews_breakdown,  # already dicts on real state
        "hosho_kaijo_score": state.hosho_kaijo_score,
        "hosho_kaijo_conditions": state.hosho_kaijo_conditions.model_dump(),
    }
    rendered = build_explainability_report(rehydrated)
    assert rendered == from_obj
    # And it resolved the kanji, not the romanized id.
    assert FsaClass.YOCHUISAKI.kanji in rendered
    assert "yochuisaki" not in rendered


def test_report_ends_with_single_trailing_newline() -> None:
    """The artifact ends with exactly one newline (stable diffs / archiving)."""
    report = build_explainability_report(_yochuisaki_state())
    assert report.endswith("\n")
    assert not report.endswith("\n\n")


def test_explainability_filename_is_safe() -> None:
    """The filename builder mirrors the DOCX/XLSX cross-platform contract."""
    assert (
        explainability_filename("\u30c6\u30b9\u30c8 \u88fd\u9020")
        == "explainability_\u30c6\u30b9\u30c8_\u88fd\u9020.md"
    )
    assert explainability_filename("bad:name?") == "explainability_bad_name.md"
    assert explainability_filename("") == "explainability_borrower.md"
