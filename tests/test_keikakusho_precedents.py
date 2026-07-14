"""Tests for the Feature 4 (partial) advisory precedent-citations appendix.

The feasibility critic already grounds its advisory note in retrieved precedents
(past plans / benchmarks / FSA passages) and records the per-claim provenance.
This section surfaces those precedents as CITATIONS in the regulated Keikakusho
so a banker / examiner can see what each feasibility opinion rests on.

Locked contracts for ``render_keikakusho``'s precedent appendix:

1. **Byte-identical when omitted / ungrounded.** Without ``feasibility_notes``
   (or when no provenance claim is ``grounded`` — the offline / no-LLM case) the
   draft is exactly the prior output, so existing render / polish / graph-flow
   expectations are unaffected.
2. **Advisory only.** When present, the section is explicitly labelled advisory
   and states it is NOT used for the classification or any figure — it must
   never be mistaken for the deterministic basis.
3. **Faithful + deterministic.** It lists each strategy's distinct grounded
   sources in first-seen order (de-duplicated), and only ``grounded`` claims are
   cited (``unverified`` claims are not).

Offline, deterministic; imports only from ``app.*``.
"""

from __future__ import annotations

import datetime as dt

from app.backend.nodes.kaizen_generation import render_keikakusho
from app.backend.state import Strategy
from app.shared.models.accounting import TrialBalance

_LATEST = TrialBalance(
    period=dt.date(2025, 6, 30),
    uriage=100_000_000,
    uriage_genka=78_000_000,
    hanbaihi=18_000_000,
    eigai_shueki=0,
    eigai_hiyo=0,
)
_STRATEGY = Strategy(
    title="\u4fa1\u683c\u8ee2\u5ac1\u306e\u5b9f\u884c",  # 価格転嫁の実行
    rationale="Recover margin via price pass-through.",
    expected_keijo_uplift=36_000_000,
)


def _render(**kwargs: object) -> str:
    return render_keikakusho(
        company_name="\u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e",
        hojin_bango="1234567890123",
        fsa_kanji="\u8981\u6ce8\u610f\u5148",
        latest=_LATEST,
        strategy=_STRATEGY,
        working_capital_gap=-5_000_000,
        **kwargs,  # type: ignore[arg-type]
    )


def _grounded_notes() -> list[dict[str, object]]:
    """Two feasibility notes: one grounded (2 distinct sources + a dup), one not."""
    return [
        {
            "strategy_title": "\u4fa1\u683c\u8ee2\u5ac1\u306e\u5b9f\u884c",
            "achievability": "medium",
            "achievability_score": 58.0,
            "advisory": (
                "\u904e\u53bb\u4e8b\u4f8b\u3067\u306f\u4fa1\u683c\u8ee2"
                "\u5ac1\u304c\u6709\u52b9\u3067\u3057\u305f\u3002"
            ),
            "advisory_grounded": True,
            "advisory_provenance": [
                {
                    "text": (
                        "\u904e\u53bb\u4e8b\u4f8b\u3067\u306f\u4fa1\u683c\u8ee2"
                        "\u5ac1\u304c\u6709\u52b9\u3067\u3057\u305f\u3002"
                    ),
                    "status": "grounded",
                    "citations": ["past_keikakusho", "benchmark"],
                },
                {
                    # A second grounded claim re-citing one source (dedupe check).
                    "text": "\u696d\u754c\u5e73\u5747\u3068\u6574\u5408\u3057\u307e\u3059\u3002",
                    "status": "grounded",
                    "citations": ["benchmark"],
                },
                {
                    # An unverified claim must NOT contribute a citation.
                    "text": "\u305d\u306e\u4ed6\u306e\u4e3b\u5f35\u3002",
                    "status": "unverified",
                    "citations": ["fsa_manual"],
                },
            ],
        },
        {
            "strategy_title": "\u539f\u4fa1\u4f4e\u6e1b",  # 原価低減 (no grounded provenance)
            "achievability": "low",
            "achievability_score": 30.0,
            "advisory": "",
            "advisory_grounded": False,
            "advisory_provenance": [],
        },
    ]


def test_byte_identical_when_notes_omitted() -> None:
    """No feasibility_notes -> output identical to a bare render (no section 6)."""
    bare = _render()
    assert "\u53c2\u8003\u4e8b\u4f8b" not in bare  # 参考事例
    assert _render(feasibility_notes=None) == bare
    assert _render(feasibility_notes=[]) == bare


def test_byte_identical_when_no_claim_is_grounded() -> None:
    """Offline / no-LLM case: only unverified claims -> no section, byte-identical."""
    bare = _render()
    ungrounded = [
        {
            "strategy_title": "strategy-a",
            "advisory_provenance": [
                {"text": "x", "status": "unverified", "citations": ["benchmark"]},
            ],
        }
    ]
    assert _render(feasibility_notes=ungrounded) == bare


def test_section_present_and_advisory_labelled_when_grounded() -> None:
    """A grounded citation appends section 6, explicitly labelled advisory."""
    out = _render(feasibility_notes=_grounded_notes())
    assert "## 6. \u53c2\u8003\u4e8b\u4f8b" in out  # ## 6. 参考事例
    # Advisory disclaimer present (bilingual): never feeds classification/figures.
    assert "advisory" in out.lower()
    assert "\u53c2\u8003\u60c5\u5831" in out  # 参考情報
    # The grounded strategy + its distinct sources are cited.
    assert "\u4fa1\u683c\u8ee2\u5ac1\u306e\u5b9f\u884c" in out
    assert "past_keikakusho" in out
    assert "benchmark" in out


def test_only_grounded_claims_are_cited() -> None:
    """An unverified claim's source must NOT appear as a citation."""
    out = _render(feasibility_notes=_grounded_notes())
    # fsa_manual was only on an unverified claim -> excluded.
    assert "fsa_manual" not in out


def test_sources_are_deduplicated_in_first_seen_order() -> None:
    """Each source is listed once, in first-seen order (deterministic)."""
    out = _render(feasibility_notes=_grounded_notes())
    # benchmark appears on two grounded claims but must be listed exactly once.
    assert out.count("- benchmark") == 1
    # First-seen order: past_keikakusho before benchmark.
    assert out.index("past_keikakusho") < out.index("- benchmark")


def test_ungrounded_strategy_is_skipped() -> None:
    """A strategy with no grounded provenance gets no citation subsection."""
    out = _render(feasibility_notes=_grounded_notes())
    # The second strategy (原価低減) had no grounded provenance -> no subheading.
    assert "### \u539f\u4fa1\u4f4e\u6e1b" not in out


def test_section_comes_after_the_deterministic_sections() -> None:
    """The advisory appendix sits after the hosho (deterministic) section."""
    out = _render(
        hosho_score=72.5,
        hosho_eligible=True,
        hosho_conditions={
            "bunri_met": True,
            "bunri_score": 40.0,
            "zaimu_met": False,
            "zaimu_score": 17.5,
            "kaiji_met": False,
            "kaiji_score": 15.0,
            "ordered_directives": [],
        },
        feasibility_notes=_grounded_notes(),
    )
    assert out.index("## 5. \u7d4c\u55b6\u8005\u4fdd\u8a3c\u89e3\u9664") < out.index(
        "## 6. \u53c2\u8003\u4e8b\u4f8b"
    )
